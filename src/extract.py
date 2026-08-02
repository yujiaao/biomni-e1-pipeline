#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract.py — Phase 2：AI 提取（核心，支持并发断点续传）

对 papers.jsonl 中每篇论文：
  1. PDF 解析（pymupdf）为纯文本；无 PDF 或库缺失时回退到摘要文本
  2. 分块（章节优先，超长论文定长 12000 字符 + 800 重叠）
  3. 逐块调用 LLM 提取 Task/Tool/Database/Software 四类实体（带 confidence）
  4. 论文内去重，输出到 data/extractions/<doi>.json
  5. 幻觉防护：confidence<0.5 标记 needs_review

用法：
  python src/extract.py                      # 并发处理全部论文（默认 4 线程）
  python src/extract.py --workers 8          # 8 线程
  python src/extract.py --limit 5            # 只处理前 5 篇（演示）
  python src/extract.py --no-verify          # 跳过 URL 可访问性校验（更快）
  python src/extract.py --force              # 强制重提取全部（忽略已有结果）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPERS_JSONL = ROOT / "data" / "papers.jsonl"
EXTRACT_DIR = ROOT / "data" / "extractions"
SYS_PROMPT = ROOT / "config" / "prompts" / "system_prompt.txt"
USER_PROMPT_TPL = ROOT / "config" / "prompts" / "user_prompt.txt"
PAPERS_PDF_DIR = ROOT / "data" / "papers"

sys.path.insert(0, str(ROOT / "src"))
from llm_client import LLMClient  # noqa: E402

_CHUNK_CHARS = 12000   # ~3000 tokens
_OVERLAP_CHARS = 800   # ~200 tokens

try:
    import fitz  # pymupdf
    HAVE_FITZ = True
except Exception:  # noqa: BLE001
    HAVE_FITZ = False


# ---- 全局限速（防 DeepSeek 429）：保证相邻 LLM 调用间隔 >= 60/rpm 秒 ----
import threading
_rate_lock = threading.Lock()
_rate_last = [0.0]


def _rate_limit(rpm: float) -> None:
    if not rpm or rpm <= 0:
        return
    interval = 60.0 / rpm
    with _rate_lock:
        now = time.time()
        wait = _rate_last[0] + interval - now
        if wait > 0:
            time.sleep(wait)
        _rate_last[0] = time.time()


def load_pdf_text(pdf_path: Path) -> str:
    if not HAVE_FITZ or not pdf_path.exists():
        return ""
    try:
        doc = fitz.open(str(pdf_path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[extract] PDF parse failed {pdf_path}: {e}\n")
        return ""


def chunk_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    # 章节分块：优先按常见标题切分
    section_pat = re.compile(r"\n\s*([0-9]+(?:\.[0-9]+)*[\.\s]+[A-Z][^\n]{2,60}|Abstract|Introduction|Methods|Results|Discussion|Conclusion)\s*\n", re.IGNORECASE)
    parts = section_pat.split(text)
    sections = []
    for i in range(1, len(parts), 2):
        header = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append(f"{header}\n{body}".strip())
    if len(sections) >= 3:
        chunks = sections
    else:
        chunks = []
    # 超长块再定长切分
    out = []
    for c in chunks:
        if len(c) <= _CHUNK_CHARS:
            out.append(c)
        else:
            for i in range(0, len(c), _CHUNK_CHARS - _OVERLAP_CHARS):
                out.append(c[i:i + _CHUNK_CHARS])
    if not out:
        for i in range(0, len(text), _CHUNK_CHARS - _OVERLAP_CHARS):
            out.append(text[i:i + _CHUNK_CHARS])
    return [c for c in out if c.strip()]


def verify_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    try:
        import requests
        r = requests.head(url, timeout=10, allow_redirects=True)
        return r.status_code < 400
    except Exception:  # noqa: BLE001
        return False


def process_paper(p: dict, client: LLMClient, system: str, user_tpl: str, args, ext_dir: Path) -> dict:
    """处理单篇论文。返回该篇的实体计数（失败则返回 {"error": True}）。线程安全：每篇写独立文件。"""
    doi = p.get("doi", "unknown")
    safe = doi.replace("/", "_").replace(":", "_")
    ext_path = ext_dir / f"{safe}.json"
    try:
        pdf_path = PAPERS_PDF_DIR / f"{safe}.pdf"
        text = load_pdf_text(pdf_path)
        if not text and p.get("abstract"):
            text = f"Title: {p.get('title')}\nAbstract: {p.get('abstract')}"
        if not text:
            text = f"Title: {p.get('title')}"
        chunks = chunk_text(text)
        if not chunks:
            chunks = [text]

        agg = {"tasks": [], "tools": [], "databases": [], "software": []}
        for idx, ch in enumerate(chunks, 1):
            user = (user_tpl
                    .replace("{title}", p.get("title", ""))
                    .replace("{category}", p.get("category", ""))
                    .replace("{chunk_index}", str(idx))
                    .replace("{total_chunks}", str(len(chunks)))
                    .replace("{chunk_text}", ch))
            _rate_limit(args.rpm)
            res = client.extract(system, user)
            for k in agg:
                agg[k].extend(res.get(k, []))

        # 论文内去重（按 name 归一化）
        for k in agg:
            seen = {}
            for e in agg[k]:
                key = re.sub(r"[\s\-_]+", "", str(e.get("name", "")).lower())
                if key and key in seen:
                    continue
                if key:
                    seen[key] = True
                if not args.no_verify and e.get("url"):
                    e["url_reachable"] = verify_url(e["url"])
                if isinstance(e.get("confidence"), (int, float)) and e["confidence"] < 0.5:
                    e["needs_review"] = True
            agg[k] = [e for e in agg[k] if (e.get("name") or e.get("description"))]

        out = {
            "doi": doi,
            "title": p.get("title"),
            "category": p.get("category"),
            "date": p.get("date"),
            "n_chunks": len(chunks),
            "citation_count": int(p.get("citation_count", 0) or 0),
            "extraction": agg,
            "stub": agg["tasks"] == [] and agg["tools"] == [] and "_stub" in res,
        }
        ext_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        cnt = {k: len(agg[k]) for k in ("tasks", "tools", "databases", "software")}
        print(f"[extract] {doi}: tasks={cnt['tasks']} tools={cnt['tools']} "
              f"dbs={cnt['databases']} sw={cnt['software']}")
        return cnt
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[extract] 论文提取失败 {doi}: {e}\n")
        out = {
            "doi": doi,
            "title": p.get("title"),
            "category": p.get("category"),
            "date": p.get("date"),
            "n_chunks": 0,
            "citation_count": int(p.get("citation_count", 0) or 0),
            "extraction": {"tasks": [], "tools": [], "databases": [], "software": []},
            "stub": True,
            "error": str(e),
        }
        try:
            ext_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {"error": True}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--workers", type=int, default=4, help="并发线程数（默认 4）")
    ap.add_argument("--papers", default=str(PAPERS_JSONL))
    ap.add_argument("--system-prompt", default=str(SYS_PROMPT),
                    help="system prompt 文件路径（默认 config/prompts/system_prompt.txt）")
    ap.add_argument("--run-dir", default=None,
                    help="产物隔离目录；读 <run-dir>/papers.jsonl，写 <run-dir>/extractions/（默认 data/）")
    ap.add_argument("--rpm", type=float, default=120.0,
                    help="全局限速：每分钟最大 LLM 调用数（默认 120，防 DeepSeek 429；0=不限）")
    ap.add_argument("--resume", action="store_true", default=True,
                    help="跳过已提取的 DOI（断点续传，崩溃后可重跑继续）")
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    ap.add_argument("--force", action="store_true", help="强制重提取全部（忽略已有结果）")
    args = ap.parse_args()

    client = LLMClient()
    print(f"[extract] LLM backend: {client.status()}  workers={args.workers}")
    if not HAVE_FITZ:
        print("[extract] 警告：未安装 pymupdf，将仅用摘要文本作为提取输入（pip install pymupdf）")

    system = Path(args.system_prompt).read_text(encoding="utf-8")
    user_tpl = USER_PROMPT_TPL.read_text(encoding="utf-8")

    # --run-dir 隔离：读 <run-dir>/papers.jsonl，写 <run-dir>/extractions/
    if args.run_dir:
        run_dir = Path(args.run_dir)
        papers_path = run_dir / "papers.jsonl"
        ext_dir = run_dir / "extractions"
    else:
        papers_path = Path(args.papers)
        ext_dir = EXTRACT_DIR
    ext_dir.mkdir(parents=True, exist_ok=True)

    papers = [json.loads(l) for l in papers_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        papers = papers[:args.limit]

    total_entities = {"tasks": 0, "tools": 0, "databases": 0, "software": 0}

    # 主线程预筛：已提取（且有实体）的直接计入并跳过；其余提交并发处理
    to_process = []
    for p in papers:
        doi = p.get("doi", "unknown")
        safe = doi.replace("/", "_").replace(":", "_")
        ext_path = ext_dir / f"{safe}.json"
        if not args.force and args.resume and ext_path.exists():
            try:
                ex = json.loads(ext_path.read_text(encoding="utf-8"))
                extraction = ex.get("extraction") or {}
                is_stub = bool(extraction.get("_stub") or ex.get("stub"))
                # 已成功提取（有实体，或 API 正常返回但为空、非 stub）→ 视为已完成，跳过
                if extraction and not is_stub:
                    for k in total_entities:
                        total_entities[k] += len(extraction.get(k, []))
                    continue
                # 若是 stub（API 不可达时降级）→ 不跳过，本轮重跑补全
            except Exception:
                pass
        to_process.append(p)

    print(f"[extract] 跳过 {len(papers) - len(to_process)} 篇已提取，待处理 {len(to_process)} 篇")

    if to_process:
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_paper, p, client, system, user_tpl, args, ext_dir): p for p in to_process}
            for fut in as_completed(futs):
                r = fut.result()
                done += 1
                if r and "error" not in r:
                    for k in total_entities:
                        total_entities[k] += r.get(k, 0)
                if done % 25 == 0:
                    print(f"[extract] 进度 {done}/{len(to_process)}")
                time.sleep(0.1)  # 轻微限速，避免瞬时打满

    print(f"[extract] 完成。累计实体: {total_entities}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

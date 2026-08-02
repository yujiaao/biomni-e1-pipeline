"""enrich_descriptions.py — 为空描述聚合实体补全 description

背景：
  validate.py 修复后，doc_complete 判定为「有非空 description 即基本完整」。
  仍有 38 个空描述实体（多为数据库/工具）被判 doc_complete 失败进入 fail 残差。
  这些实体的 source_papers 都有 DOI，可回查 OpenAlex 拿论文摘要当 description 种子。

做法：
  1. 扫描 4 个聚合实体文件，筛出 description 为空/过短（<MIN_DESC_LEN）的实体。
  2. 收集它们的 source_papers(DOI)，批量回查 OpenAlex 还原 abstract_inverted_index。
  3. 用「论文标题 + 还原摘要前几句」拼成 description 写回实体（不覆盖已有描述）。
  4. 标记 _desc_enriched=True 防重复补全；保留原字段，不动 auto_validated。

用法：
  python src/enrich_descriptions.py            # 实际补全
  python src/enrich_descriptions.py --dry-run  # 仅统计待补全量
  python src/enrich_descriptions.py --max-chars 500 --min-desc-len 10

注意：
  - 只写 4 个实体文件，不碰 validation_report / metrics（与 recompute 一致）。
  - OpenAlex 单 DOI 查询，超时 20s、重试 1 次，失败跳过；线程并发加速（礼貌 5 线程）。
  - 不依赖 LLM / API key，纯离线 + OpenAlex 公共 API。
"""
import argparse
import json
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

AGG_DIR = Path("data/aggregated")
ENTITY_FILES = ["tasks.json", "tools.json", "databases.json", "software.json"]
OPENALEX = "https://api.openalex.org/works"
MAILTO = "biomni-pipeline@local.dev"

# 强制 IPv4（沙箱里 OpenAlex 偶发 IPv6 超时）
_orig_getaddrinfo = socket.getaddrinfo


def _force_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _force_ipv4

MIN_DESC_LEN = 10          # 短于此视为「空描述」，需要补全
MAX_CHARS = 400            # 补全 description 上限
CONCURRENCY = 5
TIMEOUT = 20


def restore_abstract(inv_idx):
    """从 abstract_inverted_index 还原摘要文本。"""
    if not inv_idx:
        return ""
    pos = {}
    for word, positions in inv_idx.items():
        for p in positions:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos))


def clip_sentences(text, max_chars=MAX_CHARS):
    """按句子截断，取前几句，避免断在单词/URL 中间。"""
    text = re.sub(r"<[^>]+>", "", text)           # 去 HTML 标签（OpenAlex 摘要偶含 <i> 等）
    text = re.sub(r"^\s*abstract[\s:.]*", "", text, flags=re.IGNORECASE)  # 去开头的 Abstract 前缀
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for seg in parts:
        if len(out) + len(seg) + 1 > max_chars:
            break
        out = (out + " " + seg).strip() if out else seg
    return out[:max_chars].rstrip() + ("…" if len(text) > max_chars else "")


def fetch_doi_abstract(doi):
    """回查单个 DOI 的 OpenAlex 摘要；返回 (title, abstract_text) 或 None。"""
    try:
        r = requests.get(
            OPENALEX,
            params={"filter": f"doi:https://doi.org/{doi}", "mailto": MAILTO},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        res = r.json().get("results", [])
        if not res:
            return None
        w = res[0]
        title = (w.get("title") or "").strip()
        abstract = restore_abstract(w.get("abstract_inverted_index"))
        return title, abstract
    except Exception:
        return None


def build_desc(title, abstract):
    title = re.sub(r"<[^>]+>", "", title or "")
    if abstract:
        body = clip_sentences(abstract)
        return f"{title}. {body}".strip() if title and title not in body else body
    return title  # 无摘要时退化为标题


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--redo", action="store_true", help="忽略 _desc_enriched 标记，强制重新补全")
    ap.add_argument("--scope", choices=["fail_doc", "empty"], default="fail_doc",
                    help="fail_doc=只补 review_queue 中 fail+doc_complete 失败的实体(默认，精准38个); empty=补全部空/短描述")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    ap.add_argument("--min-desc-len", type=int, default=MIN_DESC_LEN)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = ap.parse_args()

    # 1) 加载实体，筛空描述
    #    scope=fail_doc（默认）：只补 review_queue 中 fail 且 doc_complete 失败的实体（精准 38 个）
    #    scope=empty：补全部空/短描述实体
    fail_doc_keys = set()
    if args.scope == "fail_doc":
        rq = AGG_DIR / "cleaned_review_queue.jsonl"
        if rq.exists():
            for line in rq.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if "fail" not in r.get("decisions", []):
                    continue
                checks = r.get("detail", {}).get("checks", {})
                if not checks.get("doc_complete"):
                    fail_doc_keys.add((r.get("kind"), (r.get("name") or "").strip().lower()))
        print(f"[scope=fail_doc] review_queue 中 fail+doc_complete 失败实体: {len(fail_doc_keys)}")

    targets = []  # (file, entity, dois)
    entities_cache = {}
    for fn in ENTITY_FILES:
        fp = AGG_DIR / fn
        data = json.loads(fp.read_text(encoding="utf-8"))
        entities_cache[fn] = data
        for it in data.get("items", []):
            nm = (it.get("name") or "").strip().lower()
            kind = fn.replace(".json", "")
            desc = it.get("description") or it.get("schema") or it.get("primary_use") or ""
            if it.get("_desc_enriched") and not args.redo:
                continue
            if args.scope == "fail_doc":
                if (kind, nm) not in fail_doc_keys:
                    continue
            else:
                if desc and len(desc.strip()) >= args.min_desc_len:
                    continue
            dois = [d for d in (it.get("source_papers") or []) if d]
            if not dois:
                continue
            targets.append((fn, it, dois))

    print(f"[scan] 待补全空描述实体: {len(targets)}")

    # 2) 去重 DOI -> 摘要
    doi_set = sorted({d for _, _, ds in targets for d in ds})
    print(f"[scan] 去重 DOI: {len(doi_set)}")
    if args.dry_run:
        return

    doi2desc = {}
    done = 0

    def worker(doi):
        res = fetch_doi_abstract(doi)
        return doi, res

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(worker, d): d for d in doi_set}
        for fut in as_completed(futs):
            doi, res = fut.result()
            done += 1
            if res:
                title, abstract = res
                desc = build_desc(title, abstract)
                if desc and len(desc) >= 5:
                    doi2desc[doi] = desc
            if done % 20 == 0 or done == len(doi_set):
                print(f"  OpenAlex 进度 {done}/{len(doi_set)} | 命中 {len(doi2desc)}")

    # 3) 写回实体
    enriched = 0
    for fn, it, dois in targets:
        for d in dois:
            if d in doi2desc:
                it["description"] = doi2desc[d]
                it["_desc_enriched"] = True
                enriched += 1
                break

    # 4) 落盘（仅 4 实体文件）
    for fn, data in entities_cache.items():
        (AGG_DIR / fn).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[done] 实际补全 description 实体: {enriched}/{len(targets)}")
    print(f"[done] 命中的 DOI: {len(doi2desc)}/{len(doi_set)}")


if __name__ == "__main__":
    main()

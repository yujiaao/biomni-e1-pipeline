"""enrich_io.py — 为 desc_needs_io 实体回填 IO 字段

背景：
  validate.py 修复后，tools/databases 类实体若无 input_format/output_format 则标记 desc_needs_io
  （软标记，写入 validation_report.json）。共 ~1431 个实体缺 IO。IO 字段让工具/数据库下游可用
  （知道输入输出格式）。

做法：
  1. 从 validation_report.json 筛 desc_needs_io=True 的 (name, kind)。
  2. 回聚合实体文件找对应实体，取 source_papers(DOI)。
  3. 对每个实体获取「最佳文本」：
     - 优先 Europe PMC fullTextXML（预批量 DOI→PMCID via idconv，再 Europe PMC 全文，结构化干净）
       —— 对已发表期刊文章有效；对 2025-2026 bioRxiv 预印本常 404（全文未开放），自动降级。
     - 否则 OpenAlex abstract_inverted_index（摘要）
  4. 用 DeepSeek（LLMClient）从文本抽取 {input_format, output_format}（JSON）。Prompt 已放宽，
     允许从上下文合理推断数据类型/文件格式。
  5. 写回实体：tools/databases/software → input_format/output_format；tasks → input_type/output_type。
  6. 标记 _io_enriched=True 防重复；断点续传（--redo 强制重跑）；失败跳过。

用法：
  python src/enrich_io.py --sample 50 --redo     # 抽样 50 重跑验证（清旧标记）
  python src/enrich_io.py                         # 全量 ~1431
  python src/enrich_io.py --prefer abstract       # 只用摘要（便宜，不取全文）
  python src/enrich_io.py --max-text 8000 --concurrency 4

注意：
  - 依赖 DEEPSEEK_API_KEY（自动从 .env load，无需手动 export）。
  - 只写 4 个实体文件，不碰 validation_report/metrics（与 enrich_descriptions/recompute 一致）。
  - Europe PMC 全文优先；无全文则摘要。全文成本高但用户接受。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# 强制 IPv4（沙箱里 OpenAlex/NCBI/EBI 偶发 IPv6 超时）
_orig_getaddrinfo = socket.getaddrinfo


def _force_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _force_ipv4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from llm_client import LLMClient  # noqa: E402

AGG = ROOT / "data" / "aggregated"
ENTITY_FILES = ["tasks.json", "tools.json", "databases.json", "software.json"]
IDCONV = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
EUROPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPENALEX = "https://api.openalex.org/works"
MAILTO = "biomni-pipeline@local.dev"
TIMEOUT = 30

IO_KINDS = {"tools", "databases", "software"}


def _load_dotenv():
    """自动从 .env 载入环境变量（llm_client 只读 os.environ，不 load .env）。"""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _xml_to_text(xml: str) -> str:
    """JATS/PMC XML → 纯文本（取 body 区域，去标签）。"""
    m = re.search(r"<body[^>]*>(.*)</body>", xml, re.S | re.I)
    body = m.group(1) if m else xml
    text = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", text).strip()


def get_pmc_text(pmcid: str, text_cache: dict, lock: threading.Lock):
    """Europe PMC fullTextXML 取全文（带缓存）。pmcid 形如 PMCxxxxxx。"""
    with lock:
        if pmcid in text_cache:
            return text_cache[pmcid]
    try:
        r = requests.get(f"{EUROPMC}/{pmcid}/fullTextXML", timeout=TIMEOUT)
        if r.status_code != 200:
            with lock:
                text_cache[pmcid] = None
            return None
        text = _xml_to_text(r.text)
        with lock:
            text_cache[pmcid] = text
        return text
    except Exception:
        with lock:
            text_cache[pmcid] = None
        return None


def get_abstract(doi: str) -> str:
    """OpenAlex 摘要还原。"""
    try:
        r = requests.get(OPENALEX, params={"filter": f"doi:https://doi.org/{doi}",
                                            "mailto": MAILTO}, timeout=TIMEOUT)
        w = r.json().get("results", [])
        if not w:
            return ""
        ai = w[0].get("abstract_inverted_index") or {}
        pos = {}
        for word, positions in ai.items():
            for p in positions:
                pos[p] = word
        return " ".join(pos[i] for i in sorted(pos))
    except Exception:
        return ""


SYS_PROMPT = ("你是生物医学文献信息抽取助手。从给定文本中抽取科研工具、数据库或任务的"
              "输入格式与输出格式。")
USER_TMPL = (
    "以下文本描述了一个科研工具/数据库/任务。请抽取它的输入格式(input_format)和输出格式"
    "(output_format)。格式指数据类型或文件格式，如 FASTA、FASTQ、VCF、GTF、CSV、JSON、XML、"
    "BAM、SAM、BED、PDF、图像、表格、文本、数值矩阵等；也可能是数据类型如『测序读段』『基因列表』"
    "『蛋白质序列』『表达矩阵』。若文本明确提及或可从上下文合理推断，请列出最可能的 1-3 项；"
    "若完全无法判断则返回空列表。\n\n"
    '只返回 JSON：{"input_format": [...], "output_format": [...]}\n\n文本：\n'
)


def llm_io(client: LLMClient, text: str):
    out = client.extract(SYS_PROMPT, USER_TMPL + text)
    if out.get("_stub"):
        return None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="仅抽样前 N 个验证")
    ap.add_argument("--prefer", choices=["auto", "abstract"], default="auto",
                    help="auto=Europe PMC全文优先+摘要兜底; abstract=只用摘要")
    ap.add_argument("--max-text", type=int, default=8000, help="发给 LLM 的文本截断长度")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--redo", action="store_true", help="忽略 _io_enriched 强制重跑")
    args = ap.parse_args()

    _load_dotenv()

    # 目标实体
    vr = json.load(open(AGG / "validation_report.json", encoding="utf-8"))
    targets = [(it["name"], it["kind"]) for it in vr["items"] if it.get("desc_needs_io")]

    cache = {}
    agg = {}
    for kf in ENTITY_FILES:
        data = json.load(open(AGG / kf, encoding="utf-8"))
        cache[kf] = data
        for e in data["items"]:
            agg[(e.get("name", "").lower(), kf.replace(".json", ""))] = e

    jobs = []
    for nm, kind in targets:
        e = agg.get((nm.lower(), kind))
        if e and not (e.get("_io_enriched") and not args.redo):
            jobs.append((kind + ".json", e))
    if args.sample:
        jobs = jobs[:args.sample]
    print(f"[targets] desc_needs_io 待补全: {len(jobs)}", end=" | LLM: ")
    client = LLMClient()
    print(client.status())

    # 预批量查 PMCID 映射（主线程，减少 worker 内锁竞争）
    pmcid_cache = {}
    all_dois = list(dict.fromkeys(d for _, e in jobs for d in (e.get("source_papers") or [])))
    for i in range(0, len(all_dois), 50):
        batch = all_dois[i:i + 50]
        try:
            r = requests.get(IDCONV, params={"ids": ",".join(batch), "format": "json"}, timeout=25)
            for rec in r.json().get("records", []):
                if "pmcid" in rec:
                    pmcid_cache[rec["doi"]] = rec["pmcid"]
        except Exception:
            pass
    print(f"[pmc] 预查 PMCID 命中: {len(pmcid_cache)}/{len(all_dois)}")

    lock = threading.Lock()
    text_cache = {}
    stats = {"hit": 0, "pmc": 0, "abstract": 0, "empty": 0, "fail": 0, "skip": 0}

    def worker(job):
        kf, e = job
        kind = kf.replace(".json", "")
        dois = e.get("source_papers") or []
        text, src = None, None
        if args.prefer != "abstract":
            for d in dois:
                pmcid = pmcid_cache.get(d)
                if not pmcid:
                    continue
                t = get_pmc_text(pmcid, text_cache, lock)
                if t and len(t) > 50:
                    text, src = t, "pmc"
                    break
        if not text:
            for d in dois:
                t = get_abstract(d)
                if t and len(t) > 20:
                    text, src = t, "abstract"
                    break
        if not text:
            return ("skip", e, kind, None)
        io = llm_io(client, text[:args.max_text])
        if io is None:
            return ("fail", e, kind, src)
        inf = [str(x).strip() for x in (io.get("input_format") or []) if str(x).strip()]
        outf = [str(x).strip() for x in (io.get("output_format") or []) if str(x).strip()]
        if not inf and not outf:
            return ("empty", e, kind, src)
        return ("hit", e, kind, (inf, outf, src))

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(worker, j): j for j in jobs}
        done = 0
        for fut in as_completed(futs):
            status, e, kind, payload = fut.result()
            with lock:
                if status == "hit":
                    inf, outf, src = payload
                    # 统一存为逗号拼接字符串，与原始 schema（tools/db 的 output_format、
                    # tasks 的 output_type 均为 str）保持一致，避免下游 validate/build_env 收到 list。
                    inf_s = ", ".join(inf)
                    outf_s = ", ".join(outf)
                    if kind in IO_KINDS:
                        if inf_s:
                            e["input_format"] = inf_s
                        if outf_s:
                            e["output_format"] = outf_s
                    else:  # tasks
                        if inf_s:
                            e["input_type"] = inf_s
                        if outf_s:
                            e["output_type"] = outf_s
                    e["_io_enriched"] = True
                    stats["hit"] += 1
                    stats[src] += 1
                elif status == "empty":
                    e["_io_enriched"] = True
                    stats["empty"] += 1
                    if payload:
                        stats[payload] += 1
                elif status == "fail":
                    e["_io_enriched"] = True
                    stats["fail"] += 1
                    if payload:
                        stats[payload] += 1
                else:  # skip
                    stats["skip"] += 1
                done += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"  进度 {done}/{len(jobs)} | hit={stats['hit']} "
                      f"pmc={stats['pmc']} abstract={stats['abstract']} "
                      f"empty={stats['empty']} fail={stats['fail']} skip={stats['skip']}")

    for kf, data in cache.items():
        (AGG / kf).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[done] 处理 {len(jobs)} | 写回 IO {stats['hit']} "
          f"(pmc {stats['pmc']}/abstract {stats['abstract']}) | "
          f"空 {stats['empty']} | 失败 {stats['fail']} | 无文本 {stats['skip']}")


if __name__ == "__main__":
    main()

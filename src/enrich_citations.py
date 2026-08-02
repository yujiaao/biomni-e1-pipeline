#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_citations.py — 批量回查 OpenAlex 补全 papers.jsonl 的 citation_count。

根因：原 fetch 阶段只有 OpenAlex 来源写了 citation_count 字段，且 36 个月前抓取时
新论文尚未被引（cited_by_count 多为 0）；PubMed/arXiv 来源根本没写该字段。
OpenAlex 的 cited_by_count 是实时累积的，现在回查能拿到真实被引数，
让 T4 的「影响力加权」(citation_sum) 真正可用。

做法：
  - 真实 DOI  -> OpenAlex filter=doi: 批量（每组最多 30 个，`|` 分隔）
  - arXiv ID  -> OpenAlex filter=arxiv: 逐个查询（返回顺序不保证，逐个更稳）
  - 默认只补「字段缺失」或「值为 0」的；`--all` 强制重查所有（含已为正数）
  - 重写前先备份 papers.jsonl
  - 单请求超时 20s、重试 1 次；失败批次跳过（不阻塞整轮），最后汇总未命中

用法：
  python -u src/enrich_citations.py --dry-run
  python -u src/enrich_citations.py
  python -u src/enrich_citations.py --all
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests
import socket as _socket

_orig = _socket.getaddrinfo


def _ipv4(host, port, family=0, type=0, proto=0, flags=0):
    # 强制 IPv4，规避部分环境 IPv6 解析超时
    return _orig(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _ipv4

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ROOT / "data" / "papers.jsonl"
OPENALEX = "https://api.openalex.org/works"
_MAILTO = os.environ.get("OPENALEX_MAILTO", "biomni-pipeline@local.dev")
_BURST = 0.15
_BATCH = 30


def _req(params: dict, timeout: int = 20, retries: int = 1) -> dict:
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(OPENALEX, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            sys.stderr.write(f"[enrich] 请求失败(尝试{attempt+1}): {e}\n")
            time.sleep(1.5)
    return {}  # 失败返回空，调用方跳过该批


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="连已为正数 citation_count 的也重查（默认只补缺失/为 0）")
    ap.add_argument("--dry-run", action="store_true", help="只统计待查量，不发起请求")
    args = ap.parse_args()

    papers: list[dict] = []
    for line in PAPERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            papers.append(json.loads(line))

    doi_q: dict[str, list[int]] = {}   # doi -> [paper index]
    arx_q: dict[str, list[int]] = {}   # arxiv id(去版本) -> [paper index]
    for i, p in enumerate(papers):
        doi = (p.get("doi") or "").strip()
        cc = p.get("citation_count")
        has = "citation_count" in p
        need = args.all or (not has) or (not cc)
        if not need:
            continue
        if doi.startswith("arXiv:"):
            aid = doi.split("arXiv:", 1)[1].rsplit("v", 1)[0]
            arx_q.setdefault(aid, []).append(i)
        elif doi:
            doi_q.setdefault(doi, []).append(i)

    print(f"[enrich] 论文总数={len(papers)} | 待查 doi={len(doi_q)} arxiv={len(arx_q)}", flush=True)
    if args.dry_run:
        return 0

    # 备份
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = PAPERS.with_name(f"papers_bak_{ts}.jsonl")
    bak.write_text(PAPERS.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[enrich] 备份 -> {bak}", flush=True)

    updated = 0
    missed: list[str] = []

    # 批量查 DOI（每组最多 BATCH）
    items = list(doi_q.items())
    total_batches = (len(items) + _BATCH - 1) // _BATCH
    for bi in range(0, len(items), _BATCH):
        batch = items[bi:bi + _BATCH]
        filt = "|".join(f"https://doi.org/{d}" for d, _ in batch)
        data = _req({"filter": f"doi:{filt}", "mailto": _MAILTO, "per-page": 200})
        cmap: dict[str, int] = {}
        for w in data.get("results", []):
            d = (w.get("doi") or "").replace("https://doi.org/", "")
            if d:
                cmap[d] = int(w.get("cited_by_count", 0) or 0)
        for d, idxs in batch:
            v = cmap.get(d, 0)
            if v:  # 仅统计查到正数的，便于观察
                updated += len(idxs)
            else:
                missed.append(d)
            for i in idxs:
                papers[i]["citation_count"] = v
        print(f"[enrich] doi 批 {bi // _BATCH + 1}/{total_batches} 完成 (累计查到 {updated} 处)", flush=True)
        time.sleep(_BURST)

    # 逐个查 arXiv
    for ai, (aid, idxs) in enumerate(arx_q.items(), 1):
        data = _req({"filter": f"arxiv:https://arxiv.org/abs/{aid}", "mailto": _MAILTO})
        w = (data.get("results") or [{}])[0]
        v = int(w.get("cited_by_count", 0) or 0)
        if v:
            updated += len(idxs)
        else:
            missed.append(f"arXiv:{aid}")
        for i in idxs:
            papers[i]["citation_count"] = v
        if ai % 20 == 0:
            print(f"[enrich] arxiv {ai}/{len(arx_q)} 完成", flush=True)
        time.sleep(_BURST)

    PAPERS.write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in papers) + "\n",
        encoding="utf-8",
    )

    nonzero = sum(1 for p in papers if p.get("citation_count", 0))
    print(f"[enrich] 写入完成 | 更新字段 {updated} 处（含0）| 现在 citation_count>0 论文 = {nonzero}/{len(papers)}", flush=True)
    if missed:
        print(f"[enrich] 未命中(查无/超时) {len(missed)} 个，示例: {missed[:10]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recompute_citation.py — 轻量重算聚合实体的 citation_sum（不动去重结果）。

enrich_citations.py 更新 papers.jsonl 后，聚合实体里旧的 citation_sum 仍停留在
「19 个非零」的旧值。本脚本按每个实体 source_papers(DOI 列表) 从最新的
papers doi->citation 映射重新汇总，写回 data/aggregated/*.json。

相比 deduplicate 原逻辑（按 members 逐 mention 加，同一论文多 mention 会重复计），
此处按去重后的 source_papers 求和，更准确。

用法：
  python src/recompute_citation.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ROOT / "data" / "papers.jsonl"
AGG_DIR = ROOT / "data" / "aggregated"


def main() -> int:
    # 论文级 DOI -> citation 映射
    doi_cit: dict[str, int] = {}
    if PAPERS.exists():
        for line in PAPERS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            dd = (d.get("doi") or "").strip()
            if dd:
                doi_cit[dd] = int(d.get("citation_count", 0) or 0)

    if not AGG_DIR.exists():
        print("[recompute] 未找到 data/aggregated，先跑 deduplicate")
        return 1

    touched = 0
    total_cit = 0
    ent_with = 0
    # 只处理四个聚合实体文件，避免误伤 validation_report/analysis 等含 items 的旁支文件
    for k in ("tasks", "tools", "databases", "software"):
        jf = AGG_DIR / f"{k}.json"
        if not jf.exists():
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("items")
        if not isinstance(items, list):
            continue
        for it in items:
            sp = it.get("source_papers") or []
            s = sum(doi_cit.get(str(x), 0) for x in sp)
            it["citation_sum"] = s
            total_cit += s
            if s > 0:
                ent_with += 1
        jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        touched += len(items)

    print(f"[recompute] 处理实体 {touched} 个 | citation_sum>0 实体 = {ent_with} | 累计总被引 = {total_cit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

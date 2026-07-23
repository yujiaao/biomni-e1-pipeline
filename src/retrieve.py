#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retrieve.py — Phase 5.4 工具检索系统（Agent 运行时调用）

对自然语言 query 生成 embedding，与 retrieval_index.jsonl 中的工具/数据库描述向量
做余弦相似度，返回 Top-K 最相关条目，供 Agent 注入上下文后选择调用。

用法（作为模块）：
  from retrieve import retrieve
  results = retrieve("如何做单细胞 RNA-seq 注释", top_k=5)

命令行：
  python src/retrieve.py "蛋白质稳定性优化" --top-k 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETRIEVAL = ROOT / "data" / "aggregated" / "retrieval_index.jsonl"

sys.path.insert(0, str(ROOT / "src"))
from llm_client import LLMClient, cosine  # noqa: E402


def load_index() -> list[dict]:
    if not RETRIEVAL.exists():
        return []
    rows = []
    for line in RETRIEVAL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def retrieve(query: str, top_k: int = 5, client: LLMClient | None = None) -> list[dict]:
    rows = load_index()
    if not rows:
        return []
    client = client or LLMClient()
    qv = client.embed([query])[0]
    scored = []
    for r in rows:
        v = r.get("vector")
        if not v:
            continue
        scored.append((cosine(qv, v), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:top_k]:
        item = dict(r)
        item["score"] = round(score, 4)
        item.pop("vector", None)
        out.append(item)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()
    res = retrieve(args.query, args.top_k)
    print(json.dumps(res, ensure_ascii=False, indent=2))

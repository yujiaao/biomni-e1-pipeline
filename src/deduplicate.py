#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deduplicate.py — Phase 3：去重与聚合

跨所有 extractions/*.json 聚合四类实体，采用三层匹配（从粗到细）：
  Layer 1 精确匹配  : 名称归一化（去大小写/空格/连字符）后完全一致
  Layer 2 模糊匹配  : difflib 序列相似度 >= fuzzy_threshold（近似 Levenshtein）
  Layer 3 语义匹配  : embedding 余弦相似度 > semantic_threshold（需配置 embedding 后端，否则跳过并提示）

性能设计（避免 Windows spawn 下大对象 pickle 导致 BrokenProcessPool）：
  - Layer 3 语义相似度：主进程用 numpy 矩阵一次性计算（n×512 @ 512×n），C 实现释放 GIL，秒级完成，
    不进多进程，避免传递大向量。
  - Layer 1/2 字符串比较：O(n^2) 纯 Python 循环，用 ProcessPoolExecutor 多进程并行；只传递小型
    norm_names 字符串列表，pickle 安全，绕开 BrokenProcessPool。

用法：
  python src/deduplicate.py
  python src/deduplicate.py --fuzzy 0.9 --semantic 0.85 --workers 16
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
EXTRACT_DIR = ROOT / "data" / "extractions"
AGG_DIR = ROOT / "data" / "aggregated"
ALIASES_CFG = ROOT / "config" / "aliases.json"

sys.path.insert(0, str(ROOT / "src"))
from llm_client import LLMClient, cosine  # noqa: E402 (cosine 备用)

try:
    from difflib import SequenceMatcher
except Exception:  # noqa: BLE001
    SequenceMatcher = None  # type: ignore

CPU_COUNT = os.cpu_count() or 4
_DEFAULT_WORKERS = min(16, CPU_COUNT)
# O(n^2) 字符串比较在实体数 <= 该阈值时，单进程即可（避免多进程 spawn 开销）
_SMALL_N = 500

# ---- Layer1/2 多进程比较（仅传小字符串列表，pickle 安全）----
_G: dict = {}


def _init_l12(names_norm, alias_rev, fuzzy):
    _G["names"] = names_norm
    _G["alias"] = alias_rev
    _G["fuzzy"] = fuzzy


def _compare_l12_i(i: int):
    """worker：计算第 i 个实体与所有 j>i 的 Layer1/2 相似边。纯计算、无状态。"""
    try:
        names = _G["names"]
        alias = _G["alias"]
        fuzzy = _G["fuzzy"]
        ni = names[i]
        if not ni:
            return []
        edges = []
        sm = SequenceMatcher() if SequenceMatcher is not None else None
        n = len(names)
        for j in range(i + 1, n):
            nj = names[j]
            if not nj:
                continue
            if ni == nj or (alias.get(ni) == alias.get(nj) and (ni in alias or nj in alias)):
                edges.append((i, j))
                continue
            if sm is not None:
                sm.set_seqs(ni, nj)
                if sm.ratio() >= fuzzy:
                    edges.append((i, j))
        return edges
    except Exception as ex:
        import traceback as _tb
        try:
            with open("data/_worker_err.log", "a", encoding="utf-8") as _f:
                _f.write(f"[worker] i={i} {type(ex).__name__}: {ex}\n{_tb.format_exc()}\n")
        except Exception:
            pass
        return []


def _fallback_l12(n, names_norm, alias_rev, fuzzy):
    edges = []
    for i in range(n):
        ni = names_norm[i]
        if not ni:
            continue
        for j in range(i + 1, n):
            nj = names_norm[j]
            if not nj:
                continue
            if ni == nj or (alias_rev.get(ni) == alias_rev.get(nj) and (ni in alias_rev or nj in alias_rev)):
                edges.append((i, j))
                continue
            if SequenceMatcher is not None and SequenceMatcher(None, ni, nj).ratio() >= fuzzy:
                edges.append((i, j))
    return edges


def norm_name(s: str) -> str:
    return re.sub(r"[\s\-_./]+", "", str(s).lower())


def load_aliases() -> dict[str, str]:
    if ALIASES_CFG.exists():
        data = json.loads(ALIASES_CFG.read_text(encoding="utf-8"))
        rev = {}
        for canon, alist in data.get("tool_aliases", {}).items():
            rev[norm_name(canon)] = canon
            for a in alist:
                rev[norm_name(a)] = canon
        return rev
    return {}


def build_canonical(entities: list[dict], alias_rev: dict[str, str], fuzzy: float,
                    semantic: float, client: LLMClient, workers: int = _DEFAULT_WORKERS) -> list[dict]:
    names_norm = [norm_name(e.get("name", "")) for e in entities]
    n = len(entities)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # ---- Layer 1/2：多进程字符串比较（放在 embed 之前，父进程尚未加载大模型，
    #      内存最小，spawn 的 16 个 worker 最稳）----
    if n >= 2 and workers and workers > 1 and n > _SMALL_N:
        try:
            with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_init_l12,
                initargs=(names_norm, alias_rev, fuzzy),
            ) as ex:
                chunksize = max(1, n // (workers * 8))
                for edges in ex.map(_compare_l12_i, range(n), chunksize=chunksize):
                    for (i, j) in edges:
                        union(i, j)
        except Exception as e:
            print(f"[dedup] L12 多进程失败({type(e).__name__})，回退单进程：{e}")
            for (i, j) in _fallback_l12(n, names_norm, alias_rev, fuzzy):
                union(i, j)
    else:
        for (i, j) in _fallback_l12(n, names_norm, alias_rev, fuzzy):
            union(i, j)

    # ---- Layer 3：主进程 numpy 语义相似度（模型与向量仅在主进程，不进多进程）----
    emb_live = client.has_embeddings()
    if emb_live and entities:
        names_for_embed = [e.get("name", "") or e.get("description", "") for e in entities]
        vecs = client.embed(names_for_embed)
        vectors = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors = vectors / norms  # 归一化，点积即余弦
        cos = vectors @ vectors.T  # (n, n) 余弦矩阵
        # 上三角（i<j）且超过阈值
        mask = (cos > semantic) & np.triu(np.ones((n, n), dtype=bool), k=1)
        ii, jj = np.where(mask)
        for i, j in zip(ii.tolist(), jj.tolist()):
            union(int(i), int(j))
    else:
        print("[dedup] 提示：未配置 embedding 后端，跳过 Layer 3 语义匹配（stub 向量不可用于语义）")

    # 合并聚类
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    merged = []
    for root, members in clusters.items():
        base = dict(entities[members[0]])
        sources = []
        latest_version = base.get("version") or ""
        urls = set()
        for m in members:
            e = entities[m]
            sources.append(e.get("_source_paper"))
            if e.get("version") and e["version"] > latest_version:
                latest_version = e["version"]
            if e.get("url"):
                urls.add(e["url"])
        base["version"] = latest_version
        base["urls"] = sorted(urls)
        base["frequency"] = len(set(s for s in sources if s))
        base["mentions"] = len(members)
        base["source_papers"] = sorted(set(s for s in sources if s))
        base["citation_sum"] = sum(int(m.get("_citation", 0) or 0) for m in members)
        merged.append(base)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzzy", type=float, default=0.9)
    ap.add_argument("--semantic", type=float, default=0.85)
    ap.add_argument("--workers", type=int, default=_DEFAULT_WORKERS,
                    help=f"并发进程数（Layer1/2 比较并行化，默认 {_DEFAULT_WORKERS}，机器逻辑核数 {CPU_COUNT}）")
    ap.add_argument("--force", action="store_true",
                    help="强制重建所有聚合（忽略已有聚合的断点续传复用）")
    args = ap.parse_args()

    client = LLMClient()
    alias_rev = load_aliases()

    files = sorted(EXTRACT_DIR.glob("*.json"))
    raw = {"tasks": [], "tools": [], "databases": [], "software": []}

    # 论文级引用信号（OpenAlex cited_by_count）：构建 DOI -> citation 映射。
    # citation 是「论文级」属性，比依赖 extraction 内字段更可靠——
    # 聚合时按每个实体 source_paper 的 DOI 关联，汇总得到 citation_sum。
    doi_cit: dict[str, int] = {}
    pj = ROOT / "data" / "papers.jsonl"
    if pj.exists():
        for line in pj.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            dd = d.get("doi")
            if dd:
                doi_cit[dd] = int(d.get("citation_count", 0) or 0)

    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        doi = data.get("doi")
        cit = doi_cit.get(doi, 0)
        for k in raw:
            for e in data.get("extraction", {}).get(k, []):
                e = dict(e)
                e["_source_paper"] = doi
                e["_citation"] = cit  # 论文级引用数，随聚合被 source_paper 关联
                raw[k].append(e)

    AGG_DIR.mkdir(parents=True, exist_ok=True)
    summary = {}
    for k in raw:
        out_path = AGG_DIR / f"{k}.json"
        # 断点续传：若已有有效聚合结果（count>0），直接复用，跳过重复计算
        # （机器休眠导致进程被杀后重跑时，已完成类型的聚合结果不丢失）
        if out_path.exists() and not args.force:
            try:
                prev = json.loads(out_path.read_text(encoding="utf-8"))
                if isinstance(prev, dict) and int(prev.get("count", 0)) > 0:
                    summary[k] = prev["count"]
                    print(f"[dedup] {k}: 复用已有聚合 {prev['count']} 项（断点跳过）")
                    continue
            except Exception:
                pass
        merged = build_canonical(raw[k], alias_rev, args.fuzzy, args.semantic, client, workers=args.workers)
        if k == "tasks":
            for e in merged:
                fq = e.get("frequency", 0)
                e["tier"] = "high" if fq >= 50 else ("medium" if fq >= 10 else "low")
        out = {"type": k, "count": len(merged), "items": merged}
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        summary[k] = len(merged)
        print(f"[dedup] {k}: {len(raw[k])} 原始 -> {len(merged)} 聚合 (workers={args.workers})")

    (AGG_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dedup] 完成 -> {AGG_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

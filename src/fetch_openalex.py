#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_openalex.py — Phase 1 (OpenAlex 版)：统一学术图谱检索，自带 cited_by_count 影响力信号。

复用 config/categories.json 中启用的子领域，按领域短语在 OpenAlex 检索最近 N 个月、每类前 M 篇。
无需 API key（礼貌池建议加 mailto，从 env OPENALEX_MAILTO 读取，缺省用占位值）。
返回的 cited_by_count 由下游聚合阶段用于「影响力加权」（替代 freq×confidence 代理分，见 T4）。

输出：
  data/papers_openalex.jsonl   与 papers.jsonl 相同 schema，额外 source="openalex" + citation_count
  （增量：已存在的 DOI 自动跳过）

用法：
  python src/fetch_openalex.py --days 1080 --per-category 250
  python src/fetch_openalex.py --dry-run
  python src/fetch_openalex.py --per-category 3 --days 60        # 小样本冒烟
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
    return _orig(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _ipv4

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_CFG = ROOT / "config" / "categories.json"
PAPERS_JSONL = ROOT / "data" / "papers.jsonl"

OPENALEX = "https://api.openalex.org/works"
_BURST = 0.2
_MAILTO = os.environ.get("OPENALEX_MAILTO", "biomni-pipeline@local.dev")


def load_categories() -> dict:
    return json.loads(CATEGORIES_CFG.read_text(encoding="utf-8"))


def _req(params: dict, timeout: int = 60, retries: int = 4) -> dict:
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(OPENALEX, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            sys.stderr.write(f"[fetch_openalex] 请求失败(尝试{attempt+1}): {e}\n")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OpenAlex 请求失败: {last}")


def _reconstruct_abstract(inv: dict | None) -> str:
    """OpenAlex 返回倒排索引 {word: [positions]}，按位置重组为可读文本。"""
    if not inv:
        return ""
    slots: list[tuple[int, str]] = []
    for w, pos in inv.items():
        for p in pos:
            slots.append((p, w))
    slots.sort()
    return " ".join(w for _, w in slots)


def fetch_category(name: str, per_cat: int, start: str, seen: set[str]) -> list[dict]:
    collected: list[dict] = []
    cursor = "*"
    filt = f"from_publication_date:{start},title_and_abstract.search:{name}"
    while len(collected) < per_cat:
        data = _req({
            "filter": filt,
            "sort": "publication_date:desc",
            "per-page": 200,
            "cursor": cursor,
            "mailto": _MAILTO,
        })
        results = data.get("results", [])
        if not results:
            break
        for res in results:
            doi = (res.get("doi") or "").replace("https://doi.org/", "")
            if not doi or doi in seen:
                continue
            seen.add(doi)
            authors = [a.get("author", {}).get("display_name", "")
                       for a in res.get("authorships", [])]
            pub_date = res.get("publication_date") or ""
            rec = {
                "doi": doi,
                "title": res.get("title_display") or res.get("title") or "",
                "authors": [a for a in authors if a],
                "date": pub_date,
                "version": "",
                "category": name,
                "category_id": (((res.get("primary_location") or {}).get("source") or {}).get("id", "")),
                "abstract": _reconstruct_abstract(res.get("abstract_inverted_index")),
                "pdf_url": (((res.get("primary_location") or {}).get("pdf_url") or "")),
                "source": "openalex",
                "citation_count": int(res.get("cited_by_count", 0) or 0),
            }
            collected.append(rec)
        cursor = data.get("meta", {}).get("next_cursor", "")
        if not cursor or cursor == "*":
            break
        if len(results) < 200:
            break
        time.sleep(_BURST)
    return collected[:per_cat]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CATEGORIES_CFG))
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--per-category", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sel = cfg.get("selection", {})
    months = sel.get("recency_months", 18)
    recency_days = args.days or (months * 30)
    per_cat = args.per_category or sel.get("papers_per_category", 100)

    enabled = [c for c in cfg.get("categories", []) if c.get("enabled", True)]
    start = (dt.date.today() - dt.timedelta(days=recency_days)).isoformat()
    print(f"[fetch_openalex] from={start} per_cat={per_cat} mailto={_MAILTO}")

    seen: set[str] = set()
    if PAPERS_JSONL.exists():
        for line in PAPERS_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    seen.add(json.loads(line)["doi"])
                except Exception:
                    pass
    print(f"[fetch_openalex] 已有 {len(seen)} 篇，跳过增量")

    PAPERS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    out_f = PAPERS_JSONL.open("a", encoding="utf-8")
    total = 0
    for c in enabled:
        name = c["name"]
        recs = fetch_category(name, per_cat, start, seen)
        for rec in recs:
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        total += len(recs)
        out_f.flush()
        print(f"  - {name}: {len(recs)} 篇 (cited 累计 {sum(r['citation_count'] for r in recs)})")
        time.sleep(_BURST)
    out_f.close()
    print(f"[fetch_openalex] 新增 {total} 篇 -> {PAPERS_JSONL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

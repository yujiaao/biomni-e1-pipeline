#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_arxiv.py — Phase 1 (arXiv 版)：论文选取（P1 物理/数学/化学/神经科学）

按 config/categories_arxiv.json 中启用的 arXiv 学科分类，取最近 N 个月、每类前 M 篇论文：
  1. 调用 arXiv API (export.arxiv.org/api/query) 按分类 + 提交日期倒序拉取
  2. 解析 Atom XML 为与 bioRxiv 版相同的 papers.jsonl schema
     （doi 字段填 arXiv ID，如 "arXiv:2401.12345v1"，供 extract.py 兼容）
  3. 仅抓元数据（标题/作者/摘要/日期/分类），不下载 PDF
  4. 用 discipline 字段标记学科（物理/数学/化学/神经科学）

用法：
  python src/fetch_arxiv.py --demo                    # 小样本验证（每分类 5 篇）
  python src/fetch_arxiv.py --days 540 --per-category 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import requests
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_CFG = ROOT / "config" / "categories_arxiv.json"
PAPERS_JSONL = ROOT / "data" / "papers.jsonl"
PAPERS_DIR = ROOT / "data" / "papers"

ATOM = "{http://www.w3.org/2005/Atom}"


def load_categories() -> dict:
    return json.loads(CATEGORIES_CFG.read_text(encoding="utf-8"))


def date_range(recency_days: int) -> tuple[dt.date, dt.date]:
    end = dt.date.today()
    start = end - dt.timedelta(days=recency_days)
    return start, end


def fetch_category(cat: str, max_results: int, start_dt: dt.date,
                   timeout: int = 60, retries: int = 4) -> list[dict]:
    """抓单个 arXiv 分类，返回已按日期过滤的 entry dict 列表。"""
    url = "http://export.arxiv.org/api/query"
    collected: list[dict] = []
    batch = 100
    for s in range(0, max_results, batch):
        n = min(batch, max_results - s)
        params = {
            "search_query": f"cat:{cat}",
            "start": s,
            "max_results": n,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params, timeout=timeout)
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                entries = root.findall(f"{ATOM}entry")
                if not entries:
                    return collected
                for e in entries:
                    published = e.findtext(f"{ATOM}published") or ""
                    pdate = None
                    try:
                        pdate = dt.date.fromisoformat(published[:10])
                    except Exception:
                        pass
                    if pdate and pdate < start_dt:
                        return collected  # 倒序，超出日期范围即停
                    collected.append(parse_entry(e, cat))
                break
            except Exception as ex:  # noqa: BLE001
                last_err = ex
                sys.stderr.write(f"[fetch_arxiv] {cat} 第{s}批失败(尝试{attempt+1}): {ex}\n")
                time.sleep(2 * (attempt + 1))
        else:
            sys.stderr.write(f"[fetch_arxiv] {cat} 放弃: {last_err}\n")
            return collected
        time.sleep(3)  # arXiv 礼貌限速（建议 ≥3s/请求）
    return collected


def parse_entry(e, cat: str) -> dict:
    aid = e.findtext(f"{ATOM}id") or ""  # http://arxiv.org/abs/2401.12345v1
    m = re.search(r"abs/(.+)$", aid)
    arxiv_id = f"arXiv:{m.group(1)}" if m else aid
    title = (e.findtext(f"{ATOM}title") or "").strip().replace("\n", " ")
    summary = (e.findtext(f"{ATOM}summary") or "").strip().replace("\n", " ")
    published = (e.findtext(f"{ATOM}published") or "")[:10]
    authors = [a.findtext(f"{ATOM}name") for a in e.findall(f"{ATOM}author")]
    authors = [a for a in authors if a]
    ver = "v1"
    mm = re.search(r"v(\d+)$", arxiv_id)
    if mm:
        ver = f"v{mm.group(1)}"
    primary = e.find(f"{ATOM}primary_category")
    primary_term = primary.get("term") if primary is not None else cat
    return {
        "doi": arxiv_id,
        "title": title,
        "authors": authors,
        "date": published,
        "version": ver,
        "category": cat,            # 暂存 arXiv 分类代码，main 会覆盖为学科名
        "category_id": primary_term,
        "abstract": summary,
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id.replace('arXiv:', '')}",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CATEGORIES_CFG))
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--per-category", type=int, default=None)
    ap.add_argument("--run-dir", default=None,
                    help="产物隔离目录；写 <run-dir>/papers.jsonl（默认 data/papers.jsonl）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sel = cfg.get("selection", {})
    use_demo = args.demo or sel.get("demo", {}).get("enabled", False)
    if use_demo:
        d = sel.get("demo", {})
        recency_days = args.days or d.get("recency_days", 30)
        per_cat = args.per_category or d.get("papers_per_category", 5)
    else:
        months = sel.get("recency_months", 18)
        recency_days = args.days or (months * 30)
        per_cat = args.per_category or sel.get("papers_per_category", 30)
    start_dt, _ = date_range(recency_days)
    print(f"[fetch_arxiv] range 近 {recency_days} 天, per_cat={per_cat}")

    if args.run_dir:
        run_dir = Path(args.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        out_path = run_dir / "papers.jsonl"
    else:
        out_path = PAPERS_JSONL
    buckets: dict[str, int] = {}
    seen: set[str] = set()   # 跨分类重复去重（同一 DOI 只保留首次出现）
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_f = out_path.open("w", encoding="utf-8")
    count = 0
    dup = 0
    for subj in cfg.get("subjects", []):
        disc = subj["discipline"]
        for cat in subj.get("arxiv_categories", []):
            print(f"[fetch_arxiv] 抓取 {disc} / {cat} ...")
            entries = fetch_category(cat, per_cat, start_dt)
            for rec in entries:
                rec["category"] = disc      # 学科名（供 extract prompt 显示）
                rec["discipline"] = disc
                if rec["doi"] in seen:      # 跨分类重复：跳过，避免后续统计注水
                    dup += 1
                    continue
                seen.add(rec["doi"])
                buckets[disc] = buckets.get(disc, 0) + 1
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                count += 1
            print(f"  -> {cat}: {len(entries)} 篇")
    if dup:
        print(f"[fetch_arxiv] 跳过跨分类重复 {dup} 篇，唯一论文 {count} 篇")
    out_f.close()
    print(f"[fetch_arxiv] 写入元数据 {count} 篇 -> {out_path}")
    for d, c in buckets.items():
        print(f"  - {d}: {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_pubmed.py — Phase 1 (PubMed / PubMed Central 版，基于 Europe PMC 开放 API)

复用 config/categories.json 中启用的子领域，对每个领域用其名称作为短语查询在 Europe PMC
检索最近 N 个月内、每领域前 M 篇论文（peer-review 文献，带结构化摘要）。无需 API key。
可选抓取 PMC 开放获取全文 XML（--with-fulltext），显著提升后续 Database/Software 实体召回。

输出：
  data/papers_pubmed.jsonl   与 papers.jsonl 相同 schema（doi/title/authors/date/version/
                             category/category_id/abstract/pdf_url），额外 source="pubmed"
  （增量：已存在的 DOI 自动跳过，避免重复抓取）

用法：
  python src/fetch_pubmed.py --days 1080 --per-category 250 --with-fulltext
  python src/fetch_pubmed.py --dry-run
  python src/fetch_pubmed.py --per-category 3 --days 60        # 小样本冒烟
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

# 强制 IPv4：Europe PMC 某些网络环境下 AAAA 记录不可达，强制解析为 IPv4 避免 HTTP=000。
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _getaddrinfo_ipv4

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_CFG = ROOT / "config" / "categories.json"
PAPERS_JSONL = ROOT / "data" / "papers.jsonl"
PAPERS_DIR = ROOT / "data" / "papers_pubmed"

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE_SIZE = 25
_BURST = 0.25  # 礼貌限速（秒/请求）


def load_categories() -> dict:
    return json.loads(CATEGORIES_CFG.read_text(encoding="utf-8"))


def _req(params: dict, timeout: int = 60, retries: int = 4) -> dict:
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(EUROPE_PMC, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            sys.stderr.write(f"[fetch_pubmed] 请求失败(尝试{attempt+1}/{retries}): {e}\n")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Europe PMC 请求失败: {last}")


def fetch_category(name: str, per_cat: int, start_date: str, end_date: str,
                   seen: set[str], with_fulltext: bool, dry_run: bool) -> list[dict]:
    """检索单个领域，返回已按时间过滤、去重后的记录列表（最多 per_cat 条）。"""
    collected: list[dict] = []
    cursor = "*"
    q = f'("{name}" AND (SRC:MED OR SRC:PMC))'
    stop = False
    while len(collected) < per_cat and not stop:
        data = _req({
            "query": q,
            "format": "json",
            "pageSize": PAGE_SIZE,
            "cursor": cursor,
            "sort": "P_PDATE_D desc",
            "resultType": "core",
        })
        results = data.get("resultList", {}).get("result", [])
        if not results:
            break
        for res in results:
            doi = res.get("doi") or ""
            if not doi or doi in seen:
                continue
            fpd = res.get("firstPublicationDate") or ""
            if not fpd:
                continue
            if fpd < start_date:
                stop = True  # 按出版日期降序排序，遇到更早的即停止翻页
                break
            seen.add(doi)
            rec = {
                "doi": doi,
                "title": res.get("title", ""),
                "authors": (res.get("authorString") or "").split(", "),
                "date": fpd,
                "version": "",
                "category": name,
                "category_id": res.get("source", ""),
                "abstract": res.get("abstractText", "") or "",
                "pdf_url": "",
                "source": "pubmed",
            }
            # 全文链接（PMC 开放获取）
            ft = res.get("fullTextUrlList", {}).get("fullTextUrl", [])
            for u in ft:
                if u.get("availability") == "OpenAccess":
                    rec["pdf_url"] = u.get("url", "")
                    rec["fulltext_style"] = u.get("documentStyle", "")
                    break
            collected.append(rec)
            if with_fulltext and rec["pdf_url"] and not dry_run:
                _download_fulltext(doi, rec["pdf_url"])
        cursor = data.get("nextCursor", "")
        if not cursor or cursor == "*":
            break
        time.sleep(_BURST)
        if len(results) < PAGE_SIZE:
            break
    return collected[:per_cat]


def _download_fulltext(doi: str, url: str):
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    fn = PAPERS_DIR / f"{doi.replace('/', '_').replace(':', '_')}.xml"
    if fn.exists():
        return
    try:
        r = requests.get(url, timeout=60)
        if r.status_code == 200 and b"<?xml" in r.content[:200]:
            fn.write_bytes(r.content)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[fetch_pubmed] 全文下载失败 {doi}: {e}\n")
        time.sleep(0.2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CATEGORIES_CFG))
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--per-category", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--with-fulltext", action="store_true", help="下载 PMC 开放全文 XML")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sel = cfg.get("selection", {})
    months = sel.get("recency_months", 18)
    recency_days = args.days or (months * 30)
    per_cat = args.per_category or sel.get("papers_per_category", 100)

    enabled = [c for c in cfg.get("categories", []) if c.get("enabled", True)]
    end = dt.date.today()
    start = end - dt.timedelta(days=recency_days)
    start_s, end_s = start.isoformat(), end.isoformat()
    print(f"[fetch_pubmed] range={start_s}..{end_s} per_cat={per_cat} fulltext={args.with_fulltext}")

    # 增量：加载已有 DOI
    seen: set[str] = set()
    if PAPERS_JSONL.exists():
        for line in PAPERS_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    seen.add(json.loads(line)["doi"])
                except Exception:
                    pass
    print(f"[fetch_pubmed] 已有 {len(seen)} 篇，跳过增量")

    PAPERS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    out_f = PAPERS_JSONL.open("a", encoding="utf-8")
    total = 0
    for c in enabled:
        name = c["name"]
        recs = fetch_category(name, per_cat, start_s, end_s, seen, args.with_fulltext, args.dry_run)
        for rec in recs:
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        total += len(recs)
        out_f.flush()
        print(f"  - {name}: {len(recs)} 篇")
        time.sleep(_BURST)
    out_f.close()
    print(f"[fetch_pubmed] 新增 {total} 篇 -> {PAPERS_JSONL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

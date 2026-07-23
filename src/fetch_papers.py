#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_papers.py — Phase 1：论文选取（文献驱动工具环境构建）

按 config/categories.json 中启用的 bioRxiv 子领域，取最近 N 个月发表、每领域前 M 篇论文：
  1. 调用 bioRxiv details API 按时间区间批量拉取元数据
  2. 按 subject_areas 归类到启用子领域，每领域取前 papers_per_category 篇
  3. 元数据写入 data/papers.jsonl（DOI/标题/作者/领域/日期/摘要）
  4. 可选下载全文 PDF 到 data/papers/<doi>.pdf

用法：
  python src/fetch_papers.py                 # 按 config 默认（demo 关闭时取 18 个月 × 100 篇/类）
  python src/fetch_papers.py --demo          # 30 天 × 3 篇/类，快速验证
  python src/fetch_papers.py --dry-run       # 只写元数据，不下载 PDF
  python src/fetch_papers.py --days 60 --per-category 5 --max-pages 20

注意：真实运行需联网；PDF 下载量较大（2500 篇约 50GB），演示请用 --demo 或 --dry-run。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

# 强制 IPv4：部分网络/代理环境下 api.biorxiv.org 的 AAAA(IPv6) 记录不可达，
# 导致 requests 默认走 IPv6 时连接超时（HTTP=000）。强制解析为 IPv4 后正常。
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _getaddrinfo_ipv4

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_CFG = ROOT / "config" / "categories.json"
PAPERS_JSONL = ROOT / "data" / "papers.jsonl"
PAPERS_DIR = ROOT / "data" / "papers"


def load_categories() -> dict:
    return json.loads(CATEGORIES_CFG.read_text(encoding="utf-8"))


def date_range(recency_days: int) -> tuple[str, str]:
    end = dt.date.today()
    start = end - dt.timedelta(days=recency_days)
    return start.isoformat(), end.isoformat()


def fetch_interval(platform: str, start: str, end: str, cursor: int, timeout: int = 60, retries: int = 4) -> dict:
    url = f"https://api.biorxiv.org/details/{platform}/{start}/{end}/{cursor}"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            sys.stderr.write(f"[fetch] 第{cursor}页请求失败(尝试{attempt + 1}/{retries}): {e}\n")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"bioRxiv API 请求失败(页 {cursor}): {last_err}")


def iter_papers(platform: str, start: str, end: str, max_pages: int):
    cursor = 0
    pages = 0
    while True:
        data = fetch_interval(platform, start, end, cursor)
        msgs = data.get("messages", [])
        status = msgs[0].get("status") if msgs else "error"
        if status != "ok":
            sys.stderr.write(f"[fetch] API status={status}: {msgs}\n")
            break
        items = data.get("collection", data.get("items", []))
        if not items:
            break
        for it in items:
            yield it
        total = int(msgs[0].get("total", 0) or 0)
        count = int(msgs[0].get("count", len(items)) or 0)
        cursor += count
        pages += 1
        if max_pages and pages >= max_pages:
            break
        if cursor >= total:
            break
        time.sleep(0.3)  # 礼貌限速


def norm_subject(s: str) -> str:
    return s.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CATEGORIES_CFG))
    ap.add_argument("--demo", action="store_true", help="使用 config.selection.demo 的小样本参数")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--per-category", type=int, default=None)
    ap.add_argument("--max-pages", type=int, default=0, help="0=不限制页数")
    ap.add_argument("--max-total", type=int, default=0, help="0=不限制总篇数；累计达到即停止（与各类是否填满解耦）")
    ap.add_argument("--dry-run", action="store_true", help="只写元数据，不下载 PDF")
    ap.add_argument("--platform", default="biorxiv")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sel = cfg.get("selection", {})
    use_demo = args.demo or sel.get("demo", {}).get("enabled", False)
    if use_demo:
        d = sel.get("demo", {})
        recency_days = args.days or d.get("recency_days", 30)
        per_cat = args.per_category or d.get("papers_per_category", 3)
    else:
        months = sel.get("recency_months", 18)
        recency_days = args.days or (months * 30)
        per_cat = args.per_category or sel.get("papers_per_category", 100)

    enabled = {c["name"]: c["id"] for c in cfg.get("categories", []) if c.get("enabled", True)}
    enabled_lower = {name.lower(): name for name in enabled}  # API 返回小写 category，需大小写不敏感匹配
    start, end = date_range(recency_days)
    print(f"[fetch] platform={args.platform} range={start}..{end} per_cat={per_cat} dry_run={args.dry_run}")

    buckets: dict[str, list[dict]] = {name: [] for name in enabled}

    def pdf_url(doi: str, version: str) -> str:
        base = cfg["platforms"][args.platform]["pdf_base"]
        if version:
            return f"{base}/{doi}v{version}.full.pdf"
        return f"{base}/{doi}.full.pdf"

    PAPERS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    out_f = PAPERS_JSONL.open("w", encoding="utf-8")  # 截断开始；之后增量追加，崩溃可恢复
    count = 0
    for it in iter_papers(args.platform, start, end, args.max_pages):
        # bioRxiv 现版本 API 返回单值小写 `category` 字段（subject_areas 已废弃为 None）
        raw = it.get("category") or it.get("subject_areas") or ""
        subjects = [norm_subject(s) for s in str(raw).split(";") if s.strip()]
        for sub in subjects:
            canon = enabled_lower.get(sub.lower())
            if canon and len(buckets[canon]) < per_cat:
                rec = {
                    "doi": it.get("doi"),
                    "title": it.get("title"),
                    "authors": it.get("authors"),
                    "date": it.get("date"),
                    "version": it.get("version"),
                    "category": canon,
                    "category_id": enabled[canon],
                    "abstract": it.get("abstract"),
                    "pdf_url": pdf_url(it.get("doi", ""), it.get("version", "")),
                }
                buckets[canon].append(rec)
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                count += 1
                if count % 50 == 0:
                    out_f.flush()
                    print(f"[fetch] 进度 {count}/{args.max_total or '∞'} 篇...")
        # 提前结束：达到目标总篇数
        if args.max_total and count >= args.max_total:
            break
        # 提前结束：所有启用子领域都已满
        if all(len(v) >= per_cat for v in buckets.values()):
            break
    out_f.close()

    print(f"[fetch] 写入元数据 {count} 篇 -> {PAPERS_JSONL}")
    for name, recs in buckets.items():
        print(f"  - {name}: {len(recs)}")

    if not args.dry_run:
        PAPERS_DIR.mkdir(parents=True, exist_ok=True)
        ok = 0
        for recs in buckets.values():
            for rec in recs:
                fn = PAPERS_DIR / f"{rec['doi'].replace('/', '_')}.pdf"
                if fn.exists():
                    ok += 1
                    continue
                try:
                    r = requests.get(rec["pdf_url"], timeout=60)
                    if r.status_code == 200 and r.content[:4] == b"%PDF":
                        fn.write_bytes(r.content)
                        ok += 1
                    else:
                        # 回退：不带版本号
                        r2 = requests.get(rec["pdf_url"].replace(f"v{rec['version']}", ""), timeout=60)
                        if r2.status_code == 200 and r2.content[:4] == b"%PDF":
                            fn.write_bytes(r2.content)
                            ok += 1
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[fetch] PDF failed {rec['doi']}: {e}\n")
                time.sleep(0.2)
        print(f"[fetch] 下载 PDF {ok}/{count}")
    else:
        print("[fetch] dry-run：跳过 PDF 下载")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_fetch.py — Phase 1 论文选取（DB 驱动 / 可断点续传版本）

相比 fetch_papers.py，本模块把抓取过程持久化到 SQLite：
  - papers 表：每篇论文一行（doi 唯一，INSERT OR IGNORE 天然去重）
  - meta 表：cursor（API 扫描偏移）、pages、run_status、target 等运行态
  - 每抓取一页就提交一次，并把 cursor 写回 DB → 进程/网络中断后，
    再次 start() 会从已保存的 cursor 继续，实现真正的断点续传
  - 暂停用 threading.Event 实现：pause() 置位，抓取线程在下一页前检测并退出，
    cursor 已落库，resume 即 start() 从 cursor 继续

用法：
  python src/db_fetch.py                      # 命令行前台抓取 2500 篇（DB 驱动）
  python src/db_fetch.py --target 200 --days 120
  python src/app.py                           # 启动 Web UI（默认 http://localhost:8080）

注意：真实运行需联网；--dry-run（默认）只存元数据不下载 PDF；若要全文 PDF 去掉 --dry-run
（全量 2500 篇约 50GB，请确认磁盘空间）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import requests

# 强制 IPv4：api.biorxiv.org 在某些网络下 AAAA(IPv6) 不可达，强制解析 IPv4 才通。
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _getaddrinfo_ipv4

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_CFG = ROOT / "config" / "categories.json"
PAPERS_JSONL = ROOT / "data" / "papers.jsonl"
DEFAULT_DB = ROOT / "data" / "fetch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS papers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doi         TEXT UNIQUE,
    title       TEXT,
    authors     TEXT,
    date        TEXT,
    version     TEXT,
    category    TEXT,
    category_id TEXT,
    abstract    TEXT,
    pdf_url     TEXT,
    fetched_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_papers_category ON papers(category);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
"""

# 全局停止信号（暂停/重置时置位）
STOP_EVENT = threading.Event()


# ----------------------------------------------------------------------------
# 网络层
# ----------------------------------------------------------------------------
def fetch_interval(platform: str, start: str, end: str, cursor: int,
                   timeout: int = 60, retries: int = 4) -> dict:
    """拉取 bioRxiv details API 单页（100 条），带重试。"""
    url = f"https://api.biorxiv.org/details/{platform}/{start}/{end}/{cursor}"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            sys.stderr.write(f"[db_fetch] 第{cursor}页请求失败(尝试{attempt + 1}/{retries}): {e}\n")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"bioRxiv API 请求失败(页 {cursor}): {last_err}")


def date_range(recency_days: int) -> tuple[str, str]:
    end = dt.date.today()
    start = end - dt.timedelta(days=recency_days)
    return start.isoformat(), end.isoformat()


# ----------------------------------------------------------------------------
# 引擎
# ----------------------------------------------------------------------------
class FetchEngine:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._thread: threading.Thread | None = None
        self.init_db()
        self._maybe_import_jsonl()

    # ---- DB 基础 ----
    def init_db(self) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.executescript(SCHEMA)
            c.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def get_meta(self, key: str, default=None):
        with self._connect() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            c.commit()

    # ---- 配置 ----
    def load_categories(self):
        cfg = json.loads(CATEGORIES_CFG.read_text(encoding="utf-8"))
        enabled = {c["name"]: c["id"] for c in cfg.get("categories", []) if c.get("enabled", True)}
        enabled_lower = {n.lower(): n for n in enabled}
        return cfg, enabled, enabled_lower

    # ---- 统计 ----
    def counts_by_category(self) -> dict:
        with self._connect() as c:
            rows = c.execute("SELECT category, COUNT(*) FROM papers GROUP BY category").fetchall()
        return {cat: cnt for cat, cnt in rows}

    def total(self) -> int:
        with self._connect() as c:
            return c.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 导入已有 papers.jsonl（首次初始化，保留旧进度）----
    def _maybe_import_jsonl(self) -> None:
        if not PAPERS_JSONL.exists():
            return
        with self._connect() as c:
            n = c.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        if n > 0:
            return  # 已导入过
        rows = []
        for line in PAPERS_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows.append((
                d.get("doi"), d.get("title"), d.get("authors"), d.get("date"),
                d.get("version"), d.get("category"), d.get("category_id"),
                d.get("abstract"), d.get("pdf_url"),
            ))
        with self._connect() as c:
            c.executemany(
                "INSERT OR IGNORE INTO papers"
                "(doi,title,authors,date,version,category,category_id,abstract,pdf_url) "
                "VALUES(?,?,?,?,?,?,?,?,?)", rows)
            c.commit()
        self.export_jsonl()
        self.set_meta("run_status", "idle")
        self.set_meta("target_total", str(len(rows)))
        self.set_meta("cursor", "0")
        self.set_meta("pages", "0")
        sys.stderr.write(f"[db_fetch] 已从 papers.jsonl 导入 {len(rows)} 篇到 DB\n")

    # ---- 导出 papers.jsonl（兼容下游 Phase 2+ 流水线）----
    def export_jsonl(self) -> None:
        with self._connect() as c:
            rows = c.execute(
                "SELECT doi,title,authors,date,version,category,category_id,abstract,pdf_url "
                "FROM papers").fetchall()
        PAPERS_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with PAPERS_JSONL.open("w", encoding="utf-8") as f:
            for r in rows:
                rec = {
                    "doi": r[0], "title": r[1], "authors": r[2], "date": r[3],
                    "version": r[4], "category": r[5], "category_id": r[6],
                    "abstract": r[7], "pdf_url": r[8],
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- 控制：start / pause / reset ----
    def start(self, target: int = 2500, per_cat: int = 100, recency_days: int = 540,
              max_pages: int = 0, dry_run: bool = True, platform: str = "biorxiv") -> bool:
        if self.is_running():
            return False
        STOP_EVENT.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(target, per_cat, recency_days, max_pages, dry_run, platform),
            daemon=True,
        )
        self._thread.start()
        return True

    def pause(self) -> None:
        STOP_EVENT.set()

    def reset(self) -> None:
        STOP_EVENT.set()
        time.sleep(0.2)
        with self._connect() as c:
            c.executescript("DROP TABLE IF EXISTS papers; DROP TABLE IF EXISTS meta;")
            c.commit()
        self.init_db()
        if PAPERS_JSONL.exists():
            PAPERS_JSONL.unlink()
        STOP_EVENT.clear()

    # ---- 抓取主循环 ----
    def _run(self, target, per_cat, recency_days, max_pages, dry_run, platform) -> None:
        try:
            cfg, enabled, enabled_lower = self.load_categories()
            self.set_meta("run_status", "running")
            self.set_meta("last_error", "")
            self.set_meta("target_total", target)
            self.set_meta("per_cat", per_cat)
            self.set_meta("recency_days", recency_days)
            self.set_meta("max_pages", max_pages)
            self.set_meta("platform", platform)
            self.set_meta("dry_run", int(dry_run))
            self.set_meta("started_at", dt.datetime.now().isoformat(timespec="seconds"))

            start, end = date_range(recency_days)
            cursor = int(self.get_meta("cursor", 0) or 0)
            pages = int(self.get_meta("pages", 0) or 0)
            counts = self.counts_by_category()
            total = self.total()
            pdf_base = cfg["platforms"][platform]["pdf_base"]
            stale_pages = 0  # 连续零新增页数 → 判定窗口内可填类别已填满

            sys.stderr.write(f"[db_fetch] 启动: cursor={cursor} total={total} target={target}\n")

            while True:
                if STOP_EVENT.is_set():
                    self.set_meta("run_status", "paused")
                    break

                # 达标即收尾（断点续传时，若已达标直接结束，不再发起网络请求）
                if target and total >= target:
                    self.set_meta("run_status", "done")
                    self.set_meta("finished_reason", "target reached")
                    self.set_meta("finished_at", dt.datetime.now().isoformat(timespec="seconds"))
                    break

                data = fetch_interval(platform, start, end, cursor)
                msgs = data.get("messages", [])
                status = msgs[0].get("status") if msgs else "error"
                if status != "ok":
                    self.set_meta("run_status", "error")
                    self.set_meta("last_error", f"API status={status}: {msgs}")
                    break
                items = data.get("collection", data.get("items", []))
                if not items:
                    self.set_meta("run_status", "done")
                    self.set_meta("finished_at", dt.datetime.now().isoformat(timespec="seconds"))
                    break

                prev_total = total
                with self._connect() as c:
                    for it in items:
                        raw = it.get("category") or it.get("subject_areas") or ""
                        subjects = [s.strip() for s in str(raw).split(";") if s.strip()]
                        for sub in subjects:
                            canon = enabled_lower.get(sub.lower())
                            if canon and counts.get(canon, 0) < per_cat:
                                doi = it.get("doi")
                                ver = it.get("version")
                                if ver:
                                    pdf_url = f"{pdf_base}/{doi}v{ver}.full.pdf"
                                else:
                                    pdf_url = f"{pdf_base}/{doi}.full.pdf"
                                try:
                                    cur = c.execute(
                                        "INSERT INTO papers"
                                        "(doi,title,authors,date,version,category,category_id,abstract,pdf_url) "
                                        "VALUES(?,?,?,?,?,?,?,?,?)",
                                        (doi, it.get("title"), it.get("authors"), it.get("date"),
                                         ver, canon, enabled[canon], it.get("abstract"), pdf_url),
                                    )
                                    if cur.rowcount > 0:
                                        counts[canon] = counts.get(canon, 0) + 1
                                        total += 1
                                except sqlite3.IntegrityError:
                                    pass  # doi 已存在，跳过（断点续传去重）
                    c.commit()
                added = total - prev_total
                stale_pages = 0 if added > 0 else stale_pages + 1

                # 每页提交进度 → 断点续传的关键
                cursor += int(msgs[0].get("count", len(items)) or len(items))
                pages += 1
                self.set_meta("cursor", cursor)
                self.set_meta("pages", pages)
                self.set_meta("total_count", total)
                self.export_jsonl()

                if target and total >= target:
                    self.set_meta("run_status", "done")
                    self.set_meta("finished_at", dt.datetime.now().isoformat(timespec="seconds"))
                    break
                if all(v >= per_cat for v in counts.values()):
                    self.set_meta("run_status", "done")
                    break
                if max_pages and pages >= max_pages:
                    self.set_meta("run_status", "paused")
                    break
                # 停滞检测：连续 120 页零新增 → 剩余类别在窗口内已无可填论文，提前结束
                if stale_pages >= 120:
                    self.set_meta("run_status", "done")
                    self.set_meta("finished_reason",
                                  "stall: 连续120页零新增，剩余类别在窗口内已无可填论文")
                    self.set_meta("finished_at", dt.datetime.now().isoformat(timespec="seconds"))
                    break
                time.sleep(0.15)

            sys.stderr.write(f"[db_fetch] 结束: status={self.get_meta('run_status')} total={total}\n")
        except Exception as e:  # noqa: BLE001
            if target and total >= target:
                # 已在达标后发生的网络错误：视为正常收尾，不打成 error
                self.set_meta("run_status", "done")
                self.set_meta("finished_reason",
                              "target reached (ended on network error after target met)")
                self.set_meta("finished_at", dt.datetime.now().isoformat(timespec="seconds"))
            else:
                self.set_meta("run_status", "error")
                self.set_meta("last_error", f"{type(e).__name__}: {e}")
            sys.stderr.write(f"[db_fetch] 异常: {e}\n")
        finally:
            self.export_jsonl()

    # ---- 状态 / 列表（供 API 使用）----
    def status(self) -> dict:
        cfg, enabled, _ = self.load_categories()
        counts = self.counts_by_category()
        per_cat = int(self.get_meta("per_cat", 100) or 100)
        cats = [
            {"name": n, "id": enabled[n], "count": counts.get(n, 0), "cap": per_cat}
            for n in enabled
        ]
        return {
            "status": self.get_meta("run_status", "idle"),
            "running": self.is_running(),
            "total": self.total(),
            "target": int(self.get_meta("target_total", 2500) or 2500),
            "cursor": int(self.get_meta("cursor", 0) or 0),
            "pages": int(self.get_meta("pages", 0) or 0),
            "per_cat": per_cat,
            "recency_days": int(self.get_meta("recency_days", 540) or 540),
            "platform": self.get_meta("platform", "biorxiv"),
            "dry_run": bool(int(self.get_meta("dry_run", 1) or 1)),
            "started_at": self.get_meta("started_at"),
            "finished_at": self.get_meta("finished_at"),
            "last_error": self.get_meta("last_error"),
            "categories": cats,
        }

    def list_papers(self, category=None, limit: int = 50, offset: int = 0) -> dict:
        with self._connect() as c:
            if category:
                rows = c.execute(
                    "SELECT doi,title,authors,date,category FROM papers "
                    "WHERE category=? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (category, limit, offset)).fetchall()
                tot = c.execute("SELECT COUNT(*) FROM papers WHERE category=?", (category,)).fetchone()[0]
            else:
                rows = c.execute(
                    "SELECT doi,title,authors,date,category FROM papers "
                    "ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
                tot = self.total()
        return {
            "total": tot,
            "papers": [
                {"doi": r[0], "title": r[1], "authors": r[2], "date": r[3], "category": r[4]}
                for r in rows
            ],
        }


# ----------------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=2500)
    ap.add_argument("--per-cat", type=int, default=100)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--max-pages", type=int, default=0, help="0=不限制页数")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    args = ap.parse_args()

    eng = FetchEngine(args.db)
    eng.start(target=args.target, per_cat=args.per_cat,
              recency_days=args.days, max_pages=args.max_pages, dry_run=args.dry_run)
    print(f"[db_fetch] 抓取中（cursor 已落库，可 Ctrl+C 后重新运行续传）...", flush=True)
    try:
        while eng.is_running():
            time.sleep(3)
            print(f"  total={eng.total()} cursor={eng.get_meta('cursor')} pages={eng.get_meta('pages')}", flush=True)
    except KeyboardInterrupt:
        print("\n[db_fetch] 收到中断，发送暂停信号（cursor 已保存）...")
        eng.pause()
    print("最终状态:", eng.status()["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

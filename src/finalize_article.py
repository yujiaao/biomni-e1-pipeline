#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finalize_article.py — 流水线跑通后，采集真实统计数字，填充文章模板，生成最终总结文章。

读取：
  - data/fetch.db                （抓取总数、状态、起止时间）
  - data/extractions/*.json      （提取文件数、四类实体累计、失败标记数）
  - data/aggregated/*.json       （去重后工具/数据库/任务/软件数量）
  - data/aggregated/validation_report.json
  - config/tool_registry.json    （注册表条目数）
  - tools/                        （生成的封装文件数）
  - data/aggregated/retrieval_index.jsonl （检索索引行数）
  - data/extract_full.log         （提取起止时间）

输出：
  docs/biomni-e1-pipeline-总结.md
  data/article.done
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
AGG = DATA / "aggregated"
DOCS = ROOT / "docs"
TEMPLATE = DOCS / "summary_template.md"
OUT = DOCS / "biomni-e1-pipeline-总结.md"
DONE = DATA / "article.done"
EXTRACT_DIR = DATA / "extractions"
REGISTRY = ROOT / "config" / "tool_registry.json"
TOOLS_ROOT = ROOT / "tools"
RETRIEVAL = AGG / "retrieval_index.jsonl"
FETCH_DB = DATA / "fetch.db"
EXTRACT_LOG = DATA / "extract_full.log"


def jcount(path: Path) -> int:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(d, list):
        return len(d)
    if isinstance(d, dict):
        # 可能是 {category: [items]} 或 {name: meta}
        items = [v for v in d.values() if isinstance(v, list)]
        if items:
            return sum(len(i) for i in items)
        return len(d)
    return 0


def main() -> int:
    stats = {}
    # ---- 抓取 ----
    try:
        c = sqlite3.connect(str(FETCH_DB))
        row = c.execute("select key,value from meta").fetchall()
        meta = {k: v for k, v in row}
        # 真实抓取数优先取 papers 表行数（权威），回退到 meta 的 total_count
        fetch_total = 0
        try:
            fetch_total = c.execute("select count(*) from papers").fetchone()[0]
        except Exception:
            fetch_total = int(meta.get("total_count", meta.get("total", 0)) or 0)
        stats["fetch_total"] = fetch_total
        stats["fetch_status"] = meta.get("run_status", meta.get("status", "?"))
        stats["fetch_target"] = int(meta.get("target_total", meta.get("target", 0)) or 0)
        stats["fetch_cursor"] = int(meta.get("cursor", 0))
        stats["fetch_finished_reason"] = meta.get("finished_reason", "")
        stats["fetch_started"] = meta.get("started_at", "")
        stats["fetch_finished"] = meta.get("finished_at", "")
        c.close()
    except Exception as e:
        stats["fetch_error"] = str(e)

    # ---- 提取 ----
    n_files = 0
    tot = {"tasks": 0, "tools": 0, "databases": 0, "software": 0}
    n_err = 0
    if EXTRACT_DIR.exists():
        for fp in EXTRACT_DIR.glob("*.json"):
            n_files += 1
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            ex = d.get("extraction")
            if ex is None:
                continue
            if d.get("error"):
                n_err += 1
            for k in tot:
                tot[k] += len(ex.get(k, []))
    stats["extract_files"] = n_files
    stats["extract_entities"] = tot
    stats["extract_errors"] = n_err

    # ---- 聚合去重 ----
    agg_counts = {}
    for name in ["tools", "databases", "tasks", "software"]:
        p = AGG / f"{name}.json"
        if p.exists():
            agg_counts[name] = jcount(p)
    stats["agg"] = agg_counts

    # ---- 验证 ----
    vr = AGG / "validation_report.json"
    if vr.exists():
        try:
            rep = json.loads(vr.read_text(encoding="utf-8"))
            stats["validate"] = rep if isinstance(rep, dict) else {"raw": rep}
        except Exception as e:
            stats["validate_error"] = str(e)
    rq = AGG / "review_queue.jsonl"
    if rq.exists():
        n = sum(1 for _ in rq.read_text(encoding="utf-8").splitlines() if _.strip())
        stats["review_queue"] = n

    # ---- 构建环境 ----
    if REGISTRY.exists():
        try:
            reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
            stats["registry_entries"] = len(reg) if isinstance(reg, list) else len(reg.get("tools", reg))
        except Exception:
            stats["registry_entries"] = "?"
    if TOOLS_ROOT.exists():
        stats["tool_wrappers"] = len(list(TOOLS_ROOT.rglob("*.py")))
    if RETRIEVAL.exists():
        stats["retrieval_index"] = sum(1 for _ in RETRIEVAL.read_text(encoding="utf-8").splitlines() if _.strip())

    # ---- 时长 ----
    # 提取起止：从 extract_full.log 的 [supervise] start 时间到当前
    extract_start = None
    try:
        txt = EXTRACT_LOG.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"\[supervise\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+supervisor start", txt)
        if m:
            extract_start = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    now = dt.datetime.now()
    if extract_start:
        mins = (now - extract_start).total_seconds() / 60.0
        stats["extract_minutes"] = round(mins, 1)
    stats["finished_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 组装统计区块 ----
    lines = []
    lines.append(f"- **抓取论文**：{stats.get('fetch_total','?')} 篇（目标 {stats.get('fetch_target','?')}，状态 `{stats.get('fetch_status','?')}`，结束原因：{stats.get('fetch_finished_reason','') or '达标'}）")
    lines.append(f"- **LLM 提取**：{stats.get('extract_files','?')} 篇完成，累计抽取实体 —— 任务 **{tot['tasks']}**、工具 **{tot['tools']}**、数据库 **{tot['databases']}**、软件 **{tot['software']}**（失败/空标记 {stats.get('extract_errors','?')} 篇）")
    if agg_counts:
        lines.append(f"- **跨论文去重聚合**：工具 **{agg_counts.get('tools',0)}**、数据库 **{agg_counts.get('databases',0)}**、任务 **{agg_counts.get('tasks',0)}**、软件 **{agg_counts.get('software',0)}**（同名/近义合并后）")
    if "registry_entries" in stats:
        lines.append(f"- **工具注册表**：`config/tool_registry.json` 共 **{stats['registry_entries']}** 条；生成可调用封装 `tools/*.py` **{stats.get('tool_wrappers','?')}** 个")
    if "retrieval_index" in stats:
        lines.append(f"- **语义检索索引**：`data/aggregated/retrieval_index.jsonl` **{stats['retrieval_index']}** 条（本地 bge-small-zh-v1.5 向量）")
    if "validate" in stats and isinstance(stats["validate"], dict):
        v = stats["validate"]
        passed = v.get("passed") or v.get("total_passed")
        if passed is not None:
            lines.append(f"- **验收报告**：通过 **{passed}** / 需人工复核队列 **{stats.get('review_queue','?')}** 条")
    if "extract_minutes" in stats:
        lines.append(f"- **提取耗时**：约 **{stats['extract_minutes']}** 分钟（断点续传，崩溃自动重启）")
    lines.append(f"- **完成时间**：{stats['finished_at']}")
    stats_block = "\n".join(lines)

    # ---- 填充模板 ----
    tpl = TEMPLATE.read_text(encoding="utf-8")
    final = tpl.replace("{{STATS}}", stats_block)
    OUT.write_text(final, encoding="utf-8")

    summary = {
        "article": str(OUT),
        "finished_at": stats["finished_at"],
        "stats": stats,
    }
    DONE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[finalize] 文章已生成: {OUT}")
    print(f"[finalize] {stats_block}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aggregate_p1.py — 聚合 P1 真实抽取结果，按学科统计去重后的任务/工具分布。

读取 <run-dir>/papers.jsonl（arXiv 论文，含 discipline 标签）与
<run-dir>/extractions/<doi>.json（DeepSeek 真实抽取），产出：
  - 各学科论文数 / 实体数（按唯一 DOI 去重，避免跨分类重复注水）
  - 全局去重工具数、任务数
  - Top 工具 / Top 任务（按出现频次）
  - 各学科 Top 工具
结果写入 <run-dir>/p1_pipeline_stats.json 与 <run-dir>/p1_pipeline_report.md。

用法：
  python src/aggregate_p1.py                         # 默认 data/p1_arxiv_run
  python src/aggregate_p1.py --run-dir data/xxx      # 指定某次 run 目录
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def safe_name(doi: str) -> str:
    return doi.replace("/", "_").replace(":", "_") + ".json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=str(ROOT / "data" / "p1_arxiv_run"))
    args = ap.parse_args()
    run = Path(args.run_dir)
    papers_path = run / "papers.jsonl"
    ext_dir = run / "extractions"

    papers = [json.loads(l) for l in papers_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 按唯一 DOI 去重（保留首次出现，含其 discipline 标记）
    seen: set[str] = set()
    uniq: list[dict] = []
    dup = 0
    for p in papers:
        if p["doi"] in seen:
            dup += 1
            continue
        seen.add(p["doi"])
        uniq.append(p)
    if dup:
        print(f"[aggregate_p1] 跳过重复论文 {dup} 篇，唯一 {len(uniq)} 篇")

    disc_stats: dict[str, dict] = {}
    tool_names: Counter = Counter()
    task_names: Counter = Counter()
    per_disc_tools: dict[str, Counter] = {}
    tot = {"papers": 0, "tasks": 0, "tools": 0, "dbs": 0, "sw": 0}

    for p in uniq:
        ep = ext_dir / safe_name(p["doi"])
        if not ep.exists():
            continue
        ex = json.loads(ep.read_text(encoding="utf-8"))
        ext = ex.get("extraction") or {}
        disc = p.get("discipline", "?")
        d = disc_stats.setdefault(disc, {"papers": 0, "tasks": 0, "tools": 0, "dbs": 0, "sw": 0})
        d["papers"] += 1
        tot["papers"] += 1
        for k, kk in (("tasks", "tasks"), ("tools", "tools"), ("databases", "dbs"), ("software", "sw")):
            n = len(ext.get(k, []))
            d[kk] += n
            tot[kk] += n
        for t in ext.get("tools", []):
            nm = re.sub(r"[\s\-_]+", "", str(t.get("name", "")).lower())
            if nm:
                tool_names[t["name"]] += 1
                per_disc_tools.setdefault(disc, Counter())[t["name"]] += 1
        for t in ext.get("tasks", []):
            nm = re.sub(r"[\s\-_]+", "", str(t.get("name", "")).lower())
            if nm:
                task_names[t["name"]] += 1

    out = {
        "total": tot,
        "unique_papers": len(uniq),
        "by_discipline": disc_stats,
        "unique_tools": len(tool_names),
        "unique_tasks": len(task_names),
        "top_tools": tool_names.most_common(20),
        "top_tasks": task_names.most_common(15),
        "top_tools_by_discipline": {d: c.most_common(8) for d, c in per_disc_tools.items()},
    }
    stats_path = run / "p1_pipeline_stats.json"
    stats_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n[aggregate_p1] 统计已写入 {stats_path}")

    # 生成可读报告
    report = build_report(out)
    (run / "p1_pipeline_report.md").write_text(report, encoding="utf-8")
    print(f"[aggregate_p1] 报告已写入 {run / 'p1_pipeline_report.md'}")
    return 0


def build_report(out: dict) -> str:
    t = out["total"]
    lines = []
    lines.append("# P1 arXiv 流水线真实抽取报告\n")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"> 数据来源：arXiv API 抓取元数据 + DeepSeek (`deepseek-chat`) 真实抽取（非 stub）\n")
    lines.append("## 总览\n")
    lines.append(f"- 论文（唯一）：**{out['unique_papers']}** 篇")
    lines.append(f"- 任务：**{t['tasks']}** / 工具：**{t['tools']}** / 数据库：**{t['dbs']}** / 软件：**{t['sw']}**")
    lines.append(f"- 去重后唯一工具：**{out['unique_tools']}** / 唯一任务：**{out['unique_tasks']}**\n")
    lines.append("## 分学科分布\n")
    lines.append("| 学科 | 论文 | 任务 | 工具 | 数据库 | 软件 |")
    lines.append("|------|-----:|-----:|-----:|-------:|-----:|")
    for disc, d in out["by_discipline"].items():
        lines.append(f"| {disc} | {d['papers']} | {d['tasks']} | {d['tools']} | {d['dbs']} | {d['sw']} |")
    lines.append("")
    lines.append("## Top 工具（全局）\n")
    for name, n in out["top_tools"]:
        lines.append(f"- {name}（{n}）")
    lines.append("")
    lines.append("## Top 任务（全局）\n")
    for name, n in out["top_tasks"]:
        lines.append(f"- {name}（{n}）")
    lines.append("")
    lines.append("## 各学科 Top 工具\n")
    for disc, lst in out["top_tools_by_discipline"].items():
        lines.append(f"### {disc}\n")
        for name, n in lst:
            lines.append(f"- {name}（{n}）")
        lines.append("")
    lines.append("## 说明\n")
    lines.append("- 仅抽取论文**摘要**（未下载全文 PDF），故 Database/Software 类实体偏少；全文抽取可进一步翻倍，成本仍极低。")
    lines.append("- arXiv board 级分类（如 `astro-ph`）在 API `cat:` 查询下返回 0 篇，全量时需拆分为具体子类（`astro-ph.CO/GA/EP` 等）。")
    lines.append(f"- 本批 {out['unique_papers']} 篇与人工精读的 109 篇 P1 文献**互补**，不互相替代。")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())

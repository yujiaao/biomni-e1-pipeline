#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py — 五阶段流水线编排（Biomni-E1）

默认离线跑完整下游闭环（基于已有 data/extractions，无需联网/API key）：
  deduplicate → validate → build_env → auto_review → analyze → metrics
（auto_review / analyze / metrics 均为确定性、零 LLM 依赖的本地计算）

加 --with-fetch / --with-extract 可串联 Phase 1/2（需联网；Phase 2 还需配置 OPENAI_API_KEY）。

用法：
  python src/run_all.py                  # 完整下游闭环（Phase 3→5 + 指标层）
  python src/run_all.py --with-extract   # Phase 2→5（需 API key）
  python src/run_all.py --all            # Phase 1→5（需联网 + API key）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def run(script: str, *extra) -> int:
    cmd = [sys.executable, str(SRC / script), *extra]
    print(f"\n>>> {' '.join(cmd)}")
    return subprocess.call(cmd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-fetch", action="store_true")
    ap.add_argument("--with-extract", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--demo", action="store_true", help="Phase1 用演示小样本")
    args = ap.parse_args()

    do_fetch = args.all or args.with_fetch
    do_extract = args.all or args.with_extract

    if do_fetch:
        run("fetch_papers.py", "--demo" if args.demo else "")
    if do_extract:
        run("extract.py")
    rc = 0
    rc |= run("deduplicate.py")
    rc |= run("validate.py")
    rc |= run("build_env.py")
    # —— 下游分析 + 指标闭环（确定性、零 LLM 依赖）——
    rc |= run("auto_review.py")      # Phase 4b：复核队列自动收敛，产出 auto_review_metrics.json
    rc |= run("analyze.py")          # Phase 5+：洞察层，产出 analysis.json / analysis_report.md
    rc |= run("metrics.py")          # 指标层：精准度量化仪表盘 metrics.json（本任务目标）
    print("\n=== 完成 ===")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

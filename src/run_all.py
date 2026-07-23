#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py — 五阶段流水线编排（Biomni-E1）

默认离线跑 Phase 3→5（基于已有 data/extractions，无需联网/API key）。
加 --with-fetch / --with-extract 可串联 Phase 1/2（需联网；Phase 2 还需配置 OPENAI_API_KEY）。

用法：
  python src/run_all.py                  # Phase 3→5
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
    print("\n=== 完成 ===")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

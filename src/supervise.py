#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supervise.py — 全流水线脱离式监督器（无人值守长跑）

运行方式（由 PowerShell Start-Process 派生，脱离 agent 运行时）：
  1. 提取循环：并发调用 extract.py；崩溃则 15s 后自动重启（断点续传，rc=0 才收尾）。
     每轮结束后若仍有“空占位(stub)”文件（多因网络/VPN 抖动时 LLM 不可达所致），
     则再跑一轮补全，直到无 stub 或达到最大轮次。
  2. 去重 / 验证 / 构建环境（链式）
  3. 生成总结文章（finalize_article.py）
  4. 写 data/pipeline.done

只依赖机器是否开机，不受对话上下文/会话切换影响。
"""
from __future__ import annotations

import subprocess
import os
import time
import re
import json
import datetime as dt
from pathlib import Path

PY = r"C:\Users\Dell\.workbuddy\binaries\python\envs\biomni_e1\Scripts\python.exe"
ROOT = Path(r"D:\ai\biomni-e1-pipeline")
PIPE_LOG = ROOT / "data" / "pipeline.log"
DONE = ROOT / "data" / "pipeline.done"
EXTRACT_DIR = ROOT / "data" / "extractions"
WORKERS = 4
MAX_EXTRACT_PASSES = 6


def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[supervise] {ts} {msg}\n"
    with PIPE_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def env() -> dict:
    e = os.environ.copy()
    e.setdefault("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
    e.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    return e


def run(args: list[str], step: str) -> int:
    log(f"===== START {step} ===== {' '.join(args)}")
    with PIPE_LOG.open("a", encoding="utf-8") as f:
        rc = subprocess.run(args, cwd=str(ROOT), env=env(),
                            stdout=f, stderr=subprocess.STDOUT).returncode
    log(f"===== END   {step} (rc={rc}) =====")
    return rc


def count_stub_extractions() -> int:
    """统计“LLM 不可达导致”的空占位文件（extraction 内含 _stub 标记），这些需要网络恢复后重跑。"""
    n = 0
    if not EXTRACT_DIR.exists():
        return 0
    for fp in EXTRACT_DIR.glob("*.json"):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        ex = d.get("extraction") or {}
        if ex.get("_stub") or d.get("stub"):
            n += 1
    return n


def main() -> int:
    log("supervisor start (full pipeline, concurrent)")
    # ---- 1. 提取循环（含 stub 补全）----
    extract_attempt = 0
    while True:
        extract_attempt += 1
        rc = run([PY, "src/extract.py", "--no-verify", "--workers", str(WORKERS)],
                 f"extract#{extract_attempt}")
        if rc != 0:
            log(f"extract 异常退出 rc={rc}，15s 后重启（断点续传）")
            if extract_attempt >= MAX_EXTRACT_PASSES:
                log(f"已达最大重试 {MAX_EXTRACT_PASSES} 次仍失败，强行进入下游阶段")
                break
            time.sleep(15)
            continue
        # rc==0：本轮完成。检查是否还有 stub 占位（网络抖动遗留）
        stubs = count_stub_extractions()
        if stubs == 0 or extract_attempt >= MAX_EXTRACT_PASSES:
            log(f"extract 完成（第 {extract_attempt} 轮，stub 占位 {stubs} 篇）")
            break
        log(f"第 {extract_attempt} 轮完成，仍有 {stubs} 篇 stub 占位（疑似网络抖动），30s 后补跑一轮")
        time.sleep(30)
    # ---- 2-4. 去重 / 验证 / 构建（任一阶段失败则重试，耗尽后中止，不写 done）----
    downstream = [
        ("deduplicate", [PY, "src/deduplicate.py"]),
        ("validate", [PY, "src/validate.py"]),
        ("build_env", [PY, "src/build_env.py"]),
        ("finalize_article", [PY, "src/finalize_article.py"]),
    ]
    MAX_STEP_RETRY = 3
    for step, args in downstream:
        ok = False
        for attempt in range(1, MAX_STEP_RETRY + 1):
            rc = run(args, f"{step} (try {attempt})")
            if rc == 0:
                ok = True
                break
            log(f"{step} 失败 rc={rc}，30s 后重试（{attempt}/{MAX_STEP_RETRY}）")
            time.sleep(30)
        if not ok:
            log(f"!! {step} 连续 {MAX_STEP_RETRY} 次失败，流水线中止（不写 pipeline.done）")
            return 1
    # ---- 完成标记 ----
    DONE.write_text(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    log("PIPELINE DONE - 全流程完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

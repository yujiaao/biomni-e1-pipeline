#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_run.py — 一键跑到完成（断点续传安全）

串联整个流水线，专为"无人值守长跑"设计：
  1. 抓取（db_fetch，停滞检测自动结束，DB 持久化 cursor → 可续传）
  2. 提取（extract --resume，跳过已提取 DOI，单篇容错）
  3. 去重聚合（deduplicate）
  4. 验证（validate）
  5. 环境构建（build_env）

任意阶段被中断：重新运行本脚本即可从断点继续（DB/提取结果均已落盘）。
日志：data/auto_run.log ；完成标记：data/auto_run.done
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "auto_run.log"
DONE = ROOT / "data" / "auto_run.done"
VPY = os.environ.get("VPY", r"C:\Users\Dell\.workbuddy\binaries\python\envs\biomni_e1\Scripts\python.exe")

TARGET = int(os.environ.get("FETCH_TARGET", "2500"))
RECENCY_DAYS = int(os.environ.get("FETCH_DAYS", "540"))


def ts() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_cmd(args: list[str], step: str) -> int:
    log(f"===== START {step} =====  {' '.join(args)}")
    with LOG.open("a", encoding="utf-8") as f:
        proc = subprocess.run(args, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT)
    log(f"===== END   {step} (rc={proc.returncode}) =====")
    return proc.returncode


def fetch_status() -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from db_fetch import FetchEngine
    return FetchEngine().status()


def extract_counts() -> tuple[int, dict]:
    ext_dir = ROOT / "data" / "extractions"
    files = list(ext_dir.glob("*.json"))
    tot = {"tasks": 0, "tools": 0, "databases": 0, "software": 0}
    n_err = 0
    for fp in files:
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
    return len(files), tot, n_err


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log("========== auto_run 启动 ==========")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        log("警告：未设置 DEEPSEEK_API_KEY，提取将走 stub（不产出真实实体）")
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    # ---- 1. 抓取（可续传；最多重试 3 次）----
    for attempt in range(1, 4):
        s = fetch_status()
        if s["status"] == "done" or s["total"] >= s["target"]:
            log(f"抓取已完成：total={s['total']}（跳过）")
            break
        log(f"抓取第 {attempt} 次尝试（当前 status={s['status']} total={s['total']} cursor={s['cursor']}）")
        rc = run_cmd(
            [VPY, "src/db_fetch.py", "--target", str(TARGET), "--days", str(RECENCY_DAYS), "--dry-run"],
            f"fetch#{attempt}",
        )
        s = fetch_status()
        if s["status"] == "done" or s["total"] >= s["target"]:
            log(f"抓取结束：total={s['total']} reason={s.get('finished_reason','')}")
            break
        else:
            log(f"抓取未正常结束（status={s['status']}），准备重试…")
    else:
        s = fetch_status()
        log(f"抓取在 3 次尝试后仍非 done（status={s['status']} total={s['total']}），继续下游但结果可能不完整")

    # ---- 2. 提取（可续传；反复重试直到覆盖全部论文）----
    expected = 0
    try:
        expected = sum(1 for _ in open(ROOT / "data" / "papers.jsonl", encoding="utf-8") if _.strip())
    except Exception:
        pass
    log(f"待提取论文数（expected）={expected}")
    for attempt in range(1, 21):
        n_before, _, n_err_before = extract_counts()
        rc = run_cmd([VPY, "src/extract.py", "--resume", "--no-verify"], f"extract#{attempt}")
        n_after, tot, n_err = extract_counts()
        added = n_after - n_before
        log(f"提取第 {attempt} 次：文件数 {n_before} -> {n_after}（expected={expected}），"
            f"本轮新增 {added}，累计实体 {tot}，失败标记 {n_err}")
        if expected and n_after >= expected and n_err == 0:
            log("提取全部完成且无错误，结束提取循环")
            break
        if added == 0 and rc == 0:
            log("本轮无新增且无崩溃，剩余论文可能为最终失败/空实体，停止提取循环")
            break
    else:
        log("提取在 20 次尝试后仍未完全覆盖，进入下游（结果可能不完整）")

    # ---- 3-5. 去重 / 验证 / 构建 ----
    run_cmd([VPY, "src/deduplicate.py"], "deduplicate")
    run_cmd([VPY, "src/validate.py"], "validate")
    run_cmd([VPY, "src/build_env.py"], "build_env")

    # ---- 完成标记 ----
    s = fetch_status()
    n_files, tot, n_err = extract_counts()
    summary = {
        "finished_at": ts(),
        "fetch_total": s["total"],
        "fetch_status": s["status"],
        "extract_files": n_files,
        "extract_entities": tot,
        "extract_errors": n_err,
    }
    DONE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"========== auto_run 完成 ========== {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

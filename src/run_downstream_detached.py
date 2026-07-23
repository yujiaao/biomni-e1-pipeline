#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_downstream_detached.py — 下游四阶段脱离式驱动（无人值守长跑 + 断点续传）

由 PowerShell Start-Process / 看门狗 派生，脱离 agent 运行时，只取决于机器是否开机。
顺序运行：deduplicate -> validate -> build_env -> finalize_article
  * 每阶段带「完成标记」检查：输出已存在且有效则跳过（机器休眠被杀后重跑不重复已完成阶段）
  * 每阶段失败重试 3 次；全部成功才写 data/pipeline.done
  * 某阶段连续失败 -> 写 data/pipeline.error 并退出（看门狗见到 error 不再无限重试）
必须环境变量：HF_ENDPOINT=https://hf-mirror.com（sentence_transformers 走镜像下载模型）
"""
from __future__ import annotations

import subprocess
import os
import time
import sys
import json
import datetime as dt
from pathlib import Path

PY = r"C:\Users\Dell\.workbuddy\binaries\python\envs\biomni_e1\Scripts\python.exe"
ROOT = Path(r"D:\ai\biomni-e1-pipeline")
PIPE_LOG = ROOT / "data" / "pipeline.log"
DONE = ROOT / "data" / "pipeline.done"
ERR = ROOT / "data" / "pipeline.error"
LOCK = ROOT / "data" / "downstream.lock"
MAX_STEP_RETRY = 3

AGG = ROOT / "data" / "aggregated"
REGISTRY = ROOT / "config" / "tool_registry.json"
RETRIEVAL = AGG / "retrieval_index.jsonl"
VALIDATION_REPORT = AGG / "validation_report.json"
ARTICLE_DONE = ROOT / "data" / "article.done"


def acquire_lock() -> bool:
    """单实例锁：若已有同管线进程在跑则退出，避免重复启动互相干扰。"""
    if LOCK.exists():
        try:
            old_pid = int(LOCK.read_text(encoding="utf-8").strip())
            if old_pid != os.getpid():
                try:
                    os.kill(old_pid, 0)  # Windows 下 signal 0 仅检测进程是否存在
                    return False
                except OSError:
                    pass  # 旧进程已死，可覆盖
        except Exception:
            pass
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        LOCK.unlink()
    except Exception:
        pass


def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[downstream] {ts} {msg}\n"
    with PIPE_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def env() -> dict:
    e = os.environ.copy()
    # 仅当父进程环境已设置时才下传，避免明文 key 入库
    e["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "")
    e["HF_ENDPOINT"] = "https://hf-mirror.com"
    e["HF_HUB_ENDPOINT"] = "https://hf-mirror.com"
    return e


def run(args: list[str], step: str) -> int:
    log(f"===== START {step} ===== {' '.join(args)}")
    with PIPE_LOG.open("a", encoding="utf-8") as f:
        rc = subprocess.run(args, cwd=str(ROOT), env=env(),
                            stdout=f, stderr=subprocess.STDOUT).returncode
    log(f"===== END   {step} (rc={rc}) =====")
    return rc


def _read_count(path: Path) -> int:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return int(d.get("count", 0))
    except Exception:
        pass
    return 0


def dedup_done() -> bool:
    for k in ("tasks", "tools", "databases", "software"):
        p = AGG / f"{k}.json"
        if not p.exists() or _read_count(p) <= 0:
            return False
    return True


def validate_done() -> bool:
    return VALIDATION_REPORT.exists() and VALIDATION_REPORT.stat().st_size > 0


def build_env_done() -> bool:
    return REGISTRY.exists() and RETRIEVAL.exists() and RETRIEVAL.stat().st_size > 0


def finalize_done() -> bool:
    return ARTICLE_DONE.exists()


def main() -> int:
    if not acquire_lock():
        log("另一实例已在运行，本进程退出（避免重复）")
        return 0
    try:
        log("downstream driver start (deduplicate -> validate -> build_env -> finalize_article)")
        steps = [
            ("deduplicate", [PY, "src/deduplicate.py", "--workers", "16"], dedup_done),
            ("validate", [PY, "src/validate.py"], validate_done),
            ("build_env", [PY, "src/build_env.py"], build_env_done),
            ("finalize_article", [PY, "src/finalize_article.py"], finalize_done),
        ]
        for step, args, done_check in steps:
            if done_check():
                log(f"{step} 已完成（输出存在且有效），跳过")
                continue
            ok = False
            for attempt in range(1, MAX_STEP_RETRY + 1):
                rc = run(args, f"{step} (try {attempt})")
                if rc == 0 and done_check():
                    ok = True
                    break
                log(f"{step} 失败 rc={rc}（done_check={done_check()}），30s 后重试（{attempt}/{MAX_STEP_RETRY}）")
                time.sleep(30)
            if not ok:
                log(f"!! {step} 连续 {MAX_STEP_RETRY} 次失败，写 pipeline.error 并中止（不写 pipeline.done）")
                ERR.write_text(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                return 1
        DONE.write_text(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        log("DOWNSTREAM DONE - 去重/验证/构建/文章 全部完成")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    raise SystemExit(main())

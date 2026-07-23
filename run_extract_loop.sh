#!/usr/bin/env bash
# 自修复提取循环：extract.py 若异常退出则 15s 后自动重启，直到 rc=0（完成）才退出。
# 密钥从环境变量读取，避免明文入库。运行前先：export DEEPSEEK_API_KEY=sk-xxx
: "${DEEPSEEK_API_KEY:?未设置 DEEPSEEK_API_KEY，请先 export 你的 DeepSeek key}"
export DEEPSEEK_API_KEY
cd /d/ai/biomni-e1-pipeline || exit 1
PY="C:/Users/Dell/.workbuddy/binaries/python/envs/biomni_e1/Scripts/python.exe"
LOG="data/extract_full.log"
echo "[watchdog] start $(date)" >> "$LOG"
while true; do
  "$PY" src/extract.py --no-verify >> "$LOG" 2>&1
  rc=$?
  echo "[watchdog] extract exited rc=$rc at $(date)" >> "$LOG"
  if [ $rc -eq 0 ]; then
    echo "[watchdog] ALL DONE" >> "$LOG"
    break
  fi
  sleep 15
done

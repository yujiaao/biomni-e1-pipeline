# watchdog.ps1 — Biomni-E1 流水线看门狗
# 每 5 分钟由 Task Scheduler 触发：若流水线未完成且驱动未运行，则脱离式重新拉起。
# 这样机器休眠杀死驱动进程后，唤醒会在 5 分钟内自愈，无需人工干预。
$ErrorActionPreference = 'SilentlyContinue'

$ROOT = "D:\ai\biomni-e1-pipeline"
$LOCK = Join-Path $ROOT "data\downstream.lock"
$DONE = Join-Path $ROOT "data\pipeline.done"
$ERR  = Join-Path $ROOT "data\pipeline.error"
$PY   = "C:\Users\Dell\.workbuddy\binaries\python\envs\biomni_e1\Scripts\python.exe"

# 已完成 -> 无需动作
if (Test-Path $DONE) { exit 0 }
# 真实失败 -> 不再无限重试，等人工排查
if (Test-Path $ERR)  { exit 0 }

# 驱动是否还活着
$alive = $false
if (Test-Path $LOCK) {
    $pidStr = (Get-Content $LOCK -ErrorAction SilentlyContinue).Trim()
    if ($pidStr -match '^\d+$') {
        $p = Get-Process -Id ([int]$pidStr) -ErrorAction SilentlyContinue
        if ($p) { $alive = $true }
    }
}
if ($alive) { exit 0 }

# 重新拉起驱动（脱离式）
Start-Process -FilePath $PY -ArgumentList "src/run_downstream_detached.py" -WorkingDirectory $ROOT -WindowStyle Hidden
exit 0

# keep-alive.ps1 — 守护榜单服务与 ngrok 隧道
# 每 60 秒检查一次：本地服务没了就重启，ngrok 没了就重连。
# 注意：不要在本脚本里清空 HTTP_PROXY，否则 serve.py 连不上交易所。
# ngrok 需要的"无代理"环境由 run-ngrok.cmd 单独处理。

$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $here "keep-alive.log"

$python  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$servePy = Join-Path $here "tools\serve.py"
$ngrokCmd = Join-Path $here "run-ngrok.cmd"
$port    = 8765

function Log($m) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
  Add-Content -Path $log -Value $line -Encoding utf8
  if ((Get-Item $log).Length -gt 2MB) { Move-Item $log "$log.old" -Force }
}

Log "守护进程启动（python=$python）"

while ($true) {
  # ---- 1) 本地榜单服务 ----
  $alive = $false
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/status" -TimeoutSec 6 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $alive = $true }
  } catch { $alive = $false }

  if (-not $alive) {
    Log "本地服务无响应，重启 serve.py"
    Start-Process -FilePath $python `
      -ArgumentList "-u", "`"$servePy`"", "--port", "$port", "--every", "300", "--top", "400" `
      -WorkingDirectory $here -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $here "serve.out.log") `
      -RedirectStandardError  (Join-Path $here "serve.err.log")
    Start-Sleep -Seconds 25
  }

  # ---- 2) ngrok 隧道 ----
  $ng = Get-Process ngrok -ErrorAction SilentlyContinue
  if (-not $ng) {
    Log "ngrok 不在运行，重连"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$ngrokCmd`"" `
      -WorkingDirectory $here -WindowStyle Hidden
    Start-Sleep -Seconds 10
  }

  Start-Sleep -Seconds 60
}

# update.ps1 — 把在线服务的当前内容镜像到 GitHub Pages（备用通道）
# 不重复扫盘：直接读取 127.0.0.1:8765 上的最新一份，写到仓库根目录并推送。
# 由计划任务 AltcoinBoardPages 每 30 分钟触发一次。

$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$log  = Join-Path $here "pages-sync.log"
$git  = (Get-Command git).Source
$base = "http://127.0.0.1:8765"
$enc  = New-Object System.Text.UTF8Encoding($false)

function Log($m) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
  Add-Content -Path $log -Value $line -Encoding utf8
  if ((Get-Item $log).Length -gt 1MB) { Move-Item $log "$log.old" -Force }
}

try {
  $st = Invoke-RestMethod -Uri "$base/status" -TimeoutSec 15
  if (-not $st.ok) { Log "在线服务未就绪，本轮跳过"; exit 0 }

  $html = (Invoke-WebRequest -Uri "$base/"         -TimeoutSec 40 -UseBasicParsing).Content
  $md   = (Invoke-WebRequest -Uri "$base/markdown" -TimeoutSec 40 -UseBasicParsing).Content

  if (-not $html -or $html.Length -lt 5000) { Log "抓到的内容异常（长度不足），跳过"; exit 1 }
  if ($html -notmatch "涨幅榜")             { Log "内容里没有榜单标志，跳过"; exit 1 }

  [System.IO.File]::WriteAllText((Join-Path $here "index.html"),     $html, $enc)
  [System.IO.File]::WriteAllText((Join-Path $here "board.md"),       $md,   $enc)
  [System.IO.File]::WriteAllText((Join-Path $here "last-build.txt"), "$($st.last_success) UTC", $enc)

  & $git add index.html board.md last-build.txt 2>&1 | Out-Null
  & $git commit -q -m ("pages mirror " + $st.last_success) 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Log "无变化，跳过推送"; exit 0 }

  & $git push -q origin main 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Log "推送失败（检查凭据）"; exit 1 }

  Log ("已同步 Pages：" + $st.last_success)
} catch {
  Log ("异常: " + $_.Exception.Message)
  exit 1
}

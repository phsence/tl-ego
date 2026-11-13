# restart_lnnexport.ps1 -- 2026-10-16: 为 lnnexport 改动(neural_snapshots 3 列导出)重启 TL-Ego.
# 无 WMI: netstat 找监听 PID -> 杀 -> 等端口 -> 起 bat -> 轮询监听+/status. 单次 UAC.
$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_lnnexport.log'
function L($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }

L "=== lnnexport restart === self_pid=$PID"

$t = @(netstat -ano | Select-String '127\.0\.0\.1:8766\s+.*LISTENING')
if ($t.Count -eq 0) {
  L "no listener on 8766"
} else {
  $tp = ($t[0].ToString().Trim() -split '\s+')[-1]
  L "listener pid=$tp -> kill"
  Stop-Process -Id ([int]$tp) -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 3
$free = $false
for ($i = 0; $i -lt 40; $i++) {
  if (-not (netstat -ano | Select-String ':8766\s+.*LISTENING')) { L "port free after $i s"; $free = $true; break }
  Start-Sleep -Seconds 1
}
if (-not $free) { L "ABORT: port 8766 still held - NOT launching a second instance"; exit 1 }

$bat = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat'
Start-Process -FilePath cmd.exe -ArgumentList '/c', $bat -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' -WindowStyle Hidden
L "bat launched"

$up = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 5
  $x = @(netstat -ano | Select-String '127\.0\.0\.1:8766\s+.*LISTENING')
  if ($x.Count -gt 0) {
    $np = ($x[0].ToString().Trim() -split '\s+')[-1]
    L "listening after $($i * 5 + 5)s pid=$np"
    $up = $true
    break
  }
}
if (-not $up) { L "ABORT: no listener after 300s"; exit 2 }

for ($i = 0; $i -lt 40; $i++) {
  try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 15 -UseBasicParsing
    if ($r.StatusCode -eq 200) {
      L ("status ok ({0} bytes) after {1}s" -f $r.Content.Length, ($i * 5))
      break
    }
  } catch { }
  Start-Sleep -Seconds 5
}
L "=== done ==="

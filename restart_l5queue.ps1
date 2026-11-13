$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_l5queue.log'
function L($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
L "=== restart (l5 trigger queue) ==="

# 1. ego server (8766) — carries the producer-side queue append
$conns = netstat -ano | Select-String ':8766\s+.*LISTENING'
$pids = @()
foreach ($c in $conns) {
  $f = @(($c.Line -split '\s+') | Where-Object { $_ -ne '' })
  if ($f.Count -ge 5) { $pids += [int]$f[-1] }
}
$pids = $pids | Select-Object -Unique
L ("8766 listener pids: " + ($pids -join ','))
foreach ($p in $pids) {
  try { Stop-Process -Id $p -Force -ErrorAction Stop; L "killed server $p" }
  catch { L "kill server $p failed: $($_.Exception.Message)" }
}
for ($i = 0; $i -lt 25; $i++) {
  if (-not (netstat -ano | Select-String ':8766\s+.*LISTENING')) { L "port free after $i s"; break }
  Start-Sleep -Seconds 1
}

# 2. dispatcher node child (standalone-launcher.mjs) — watcher.cmd relaunches it
$launcher = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match 'standalone-launcher\.mjs' }
foreach ($lp in $launcher) {
  try { Stop-Process -Id $lp.ProcessId -Force -ErrorAction Stop; L "killed launcher $($lp.ProcessId)" }
  catch { L "kill launcher $($lp.ProcessId) failed: $($_.Exception.Message)" }
}

Start-Sleep -Seconds 3
Start-Process -FilePath cmd.exe -ArgumentList '/c', 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat' -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' -WindowStyle Hidden
L "ego server bat launched"

$ok = $false
for ($i = 0; $i -lt 90; $i++) {
  Start-Sleep -Seconds 2
  if (netstat -ano | Select-String ':8766\s+.*LISTENING') { L "8766 listening after $(($i + 1) * 2)s"; $ok = $true; break }
}
if ($ok) {
  try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 20
    L ("server gen=" + $r.runtime_generation_id + " tick=" + $r.resting_ticks + " status=" + $r.status)
  } catch { L "status failed: $($_.Exception.Message)" }
} else { L "TIMEOUT waiting 8766 listener" }

# 3. watcher relaunch check
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 2
  $now = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -match 'standalone-launcher\.mjs' }
  if ($now) { L ("launcher back: pid=" + ($now.ProcessId -join ',')); break }
}
L "=== done ==="

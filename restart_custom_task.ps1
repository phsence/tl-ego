$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_custom_task.log'
function L($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
L "=== restart (custom-task dispatch) ==="
$conns = netstat -ano | Select-String ':8766\s+.*LISTENING'
$pids = @()
foreach ($c in $conns) {
  $f = @(($c.Line -split '\s+') | Where-Object { $_ -ne '' })
  if ($f.Count -ge 5) { $pids += [int]$f[-1] }
}
$pids = $pids | Select-Object -Unique
L ("listener pids: " + ($pids -join ','))
foreach ($p in $pids) {
  try { Stop-Process -Id $p -Force -ErrorAction Stop; L "killed $p" }
  catch { L "kill $p failed: $($_.Exception.Message)" }
}
for ($i = 0; $i -lt 25; $i++) {
  if (-not (netstat -ano | Select-String ':8766\s+.*LISTENING')) { L "port free after $i s"; break }
  Start-Sleep -Seconds 1
}
if (netstat -ano | Select-String ':8766\s+.*LISTENING') { L "WARN port still held -- aborting start"; L "=== done ==="; exit 1 }
Start-Process -FilePath cmd.exe -ArgumentList '/c', 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat' -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' -WindowStyle Hidden
L "bat launched"
$ok = $false
for ($i = 0; $i -lt 90; $i++) {
  Start-Sleep -Seconds 2
  if (netstat -ano | Select-String ':8766\s+.*LISTENING') { L "listening after $(($i + 1) * 2)s"; $ok = $true; break }
}
if ($ok) {
  try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 20
    L ("NEW gen=" + $r.runtime_generation_id + " tick=" + $r.resting_ticks + " status=" + $r.status)
  } catch { L "status failed: $($_.Exception.Message)" }
} else { L "TIMEOUT waiting listener" }
L "=== done ==="

$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_hermes_l7fix.log'
function L($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
L "=== elevated restart (L7 bridge fix load) ==="
foreach ($q in 30460,30164,5132) {
  try { Stop-Process -Id $q -Force -ErrorAction Stop; L "killed $q" }
  catch { L "kill $q failed: $($_.Exception.Message)" }
}
Start-Sleep -Seconds 3
for ($i=0; $i -lt 20; $i++) {
  if (-not (netstat -ano | Select-String ':8766\s+.*LISTENING')) { L "port free (after $i s)"; break }
  Start-Sleep -Seconds 1
}
if (netstat -ano | Select-String ':8766\s+.*LISTENING') { L 'WARN port still held' }
Start-Process -FilePath cmd.exe -ArgumentList '/c','C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat' -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' -WindowStyle Hidden
L "bat launched"
$ok = $false
for ($i=0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 2
  if (netstat -ano | Select-String ':8766\s+.*LISTENING') { L "listening after $($i*2+2)s"; $ok = $true; break }
}
if ($ok) {
  try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 15
    L ("NEW gen=" + $r.runtime_generation_id + " uptime=" + $r.uptime_s)
  } catch { L "status failed: $($_.Exception.Message)" }
} else { L 'TIMEOUT waiting listener' }
L "=== done ==="

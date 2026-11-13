param(
  [int[]]$ExtraKillPids = @()
)
# CIM-free restart for TL-Ego (2026-10-11).
# Why: the standard restart_tlego_dynamic.ps1 stalled inside Get-CimInstance
# (WMI queries on this box now take >30s each; the ancestor walk never finished).
# This variant finds the process tree WITHOUT WMI: netstat for the listener PID,
# then a tight StartTime window (+-8s) over python/cmd for the launch cohort
# (cmd-> python shell -> python listener all start within ~1-2s of each other).
$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_cimfree.log'
function L($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
L "=== cim-free restart ==="
$conns = netstat -ano | Select-String ':8766\s+.*LISTENING'
$lpids = @()
foreach ($c in $conns) { $f = @(($c.Line -split '\s+') | Where-Object { $_ -ne '' }); if ($f.Count -ge 5) { $lpids += [int]$f[-1] } }
$lpids = $lpids | Select-Object -Unique
L ("listener pids: " + ($lpids -join ','))
$targets = @()
foreach ($lp in $lpids) {
  $p = Get-Process -Id $lp -ErrorAction SilentlyContinue
  if ($p) {
    $st = $p.StartTime
    L ("listener {0} start={1}" -f $lp, $st)
    $lo = $st.AddSeconds(-8); $hi = $st.AddSeconds(8)
    $cohort = Get-Process python,cmd -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -ge $lo -and $_.StartTime -le $hi }
    foreach ($q in $cohort) { L ("cohort: {0} pid={1}" -f $q.ProcessName, $q.Id); $targets += $q.Id }
  }
}
foreach ($q in $ExtraKillPids) { $targets += $q }
$targets = @($targets | Sort-Object -Unique | Where-Object { $_ -ne $PID })
L ("tree to kill: " + ($targets -join ','))
foreach ($q in $targets) {
  try { Stop-Process -Id $q -Force -ErrorAction Stop; L "killed $q" }
  catch { L "kill $q failed: $($_.Exception.Message)" }
}
Start-Sleep -Seconds 3
for ($i = 0; $i -lt 30; $i++) { if (-not (netstat -ano | Select-String ':8766\s+.*LISTENING')) { L "port free after $i s"; break }; Start-Sleep -Seconds 1 }
if (netstat -ano | Select-String ':8766\s+.*LISTENING') {
  L "ABORT: port 8766 still held (kill likely needs elevation) - NOT launching a second instance"
  L "=== done ==="
  exit 1
}
Remove-Item 'C:\Users\Administrator\.openclaw\workspace\.lae\ego.pid' -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Start-Process -FilePath cmd.exe -ArgumentList '/c', 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat' -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' -WindowStyle Hidden
L "bat launched"
$ok = $false
for ($i = 0; $i -lt 90; $i++) {
  Start-Sleep -Seconds 2
  if (netstat -ano | Select-String ':8766\s+.*LISTENING') { L "listening after $(($i + 1) * 2)s"; $ok = $true; break }
}
if ($ok) {
  for ($j = 0; $j -lt 6; $j++) {
    try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 25; L ("NEW gen=" + $r.runtime_generation_id + " uptime=" + $r.uptime_s); break }
    catch { L ("status attempt $j failed: " + $_.Exception.Message); Start-Sleep -Seconds 5 }
  }
} else { L "TIMEOUT waiting listener" }
L "=== done ==="

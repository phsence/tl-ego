$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_hermes_creditloop.log'
function L($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
L "=== elevated restart (round-2 regulator fix + stdp_mult restore) ==="
# 1) kill 8766 listener tree (same walk as restart_tlego_dynamic.ps1)
$conns = netstat -ano | Select-String ':8766\s+.*LISTENING'
$pids = @()
foreach ($c in $conns) { $f = @(($c.Line -split '\s+') | Where-Object { $_ -ne '' }); if ($f.Count -ge 5) { $pids += [int]$f[-1] } }
$pids = $pids | Select-Object -Unique
L ("listener pids: " + ($pids -join ','))
$all = @()
foreach ($p in $pids) {
  $cur = $p
  for ($i = 0; $i -lt 4 -and $cur -gt 0; $i++) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
    if (-not $proc) { break }
    if ($proc.Name -match 'python|cmd') { $all += $cur; $cur = [int]$proc.ParentProcessId } else { break }
  }
}
$all = $all | Select-Object -Unique
L ("tree to kill: " + ($all -join ','))
foreach ($q in $all) {
  try { Stop-Process -Id $q -Force -ErrorAction Stop; L "killed $q" }
  catch { L "kill $q failed: $($_.Exception.Message)" }
}
Start-Sleep -Seconds 3
for ($i = 0; $i -lt 20; $i++) { if (-not (netstat -ano | Select-String ':8766\s+.*LISTENING')) { L "port free after $i s"; break }; Start-Sleep -Seconds 1 }
if (netstat -ano | Select-String ':8766\s+.*LISTENING') { L "WARN port still held" }
# 2) restore explore_stdp_mult -> 0.0808 in the persisted self-model (edit during the stopped gap;
#    the running process held 0.7 in memory after the round-1 ratchet, so file edit must happen now)
$sm = 'C:\Users\Administrator\.openclaw\workspace\.lae\lae_self_model.json'
$py = 'C:/Users/Administrator/unsloth_env/Scripts/python.exe'
$out = & $py -B 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\restore_stdp_mult.py' 2>&1
L ("stdp-edit: " + ($out -join ' | '))
# 3) start + poll
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
    L ("NEW gen=" + $r.runtime_generation_id + " uptime=" + $r.uptime_s)
  } catch { L "status failed: $($_.Exception.Message)" }
} else { L "TIMEOUT waiting listener" }
L "=== done ==="

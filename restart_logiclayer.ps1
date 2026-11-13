# One-shot elevated restart for TL-Ego 8766 after the logic-layer patch.
# netstat/IP-helper only (no per-process CIM: WMI退化会让脚本假死).
# exit codes: 0 ok / 3 port still bound / 4 status not ok / 5 no listener
$ErrorActionPreference = 'Continue'
$log  = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_logiclayer.log'
$bat  = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat'
$wd   = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'
$port = 8766
function W($m) { "$((Get-Date).ToString('HH:mm:ss')) $m" | Out-File -FilePath $log -Append -Encoding utf8 }

W "start requester=$env:USERNAME"
try { $c = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop) } catch { $c = @() }
if ($c.Count -gt 0) {
    $old = ($c.OwningProcess | Sort-Object -Unique)
    W "old listener pid=$($old -join ',')"
    foreach ($p in $old) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
} else {
    W "no listener at start"
}
$free = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    $still = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($still.Count -eq 0) { $free = $true; break }
}
if (-not $free) { W "ABORT port still bound"; exit 3 }
W "port free -> starting bat"
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $bat -WorkingDirectory $wd -WindowStyle Hidden

$newp = $null
for ($i = 0; $i -lt 240; $i++) {
    Start-Sleep -Seconds 1
    $l = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($l.Count -gt 0) { $newp = ($l.OwningProcess | Sort-Object -Unique) -join ','; break }
}
if (-not $newp) { W "ABORT no listener after 240s"; exit 5 }
W "new listener pid=$newp"

$ok = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 1
    try {
        $s = Invoke-RestMethod "http://127.0.0.1:$port/status" -TimeoutSec 5
        if ($s.status -eq 'ok') { $ok = $true; break }
    } catch { }
}
if (-not $ok) { W "listener up but /status not ok yet"; exit 4 }
W "status ok gen=$($s.runtime_generation_id)"
W "logic_layer=$($s.logic_layer | ConvertTo-Json -Compress -Depth 6)"
exit 0

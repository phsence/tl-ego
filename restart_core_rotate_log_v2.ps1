# restart_core_rotate_log_v2.ps1 - 2026-11-05
# v1 bugs: netstat trailing CR broke '^\d+$' so the kill was skipped; rotate retried 1s x30,
# which loses the race to the supervisor relaunch (~12s). v2: trim the pid, rotate in a tight
# loop immediately after the kill, then relaunch, and verify by a CHANGED runtime_generation_id.
$ErrorActionPreference = 'SilentlyContinue'
$root = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'
$log  = "$root\logs\restart_rotate.log"
function L($m) { "$([DateTime]::Now.ToString('HH:mm:ss'))] $m" | Out-File -FilePath $log -Append -Encoding utf8 }

L '=== v2 restart + rotate start ==='

$idle = $false
for ($i = 0; $i -lt 120; $i++) {
    $s = Get-Content "$root\..\.lae\ego_dispatch_state.json" -Raw | ConvertFrom-Json
    if (-not $s.running_l5 -and -not $s.running_comm) { $idle = $true; break }
    Start-Sleep -Seconds 5
}
L ("idle=" + $idle + " after " + ($i * 5) + "s")

$oldGen = ''
try { $oldGen = (Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 15).runtime_generation_id } catch {}
L ("old gen=" + $oldGen)

$line = netstat -ano | Select-String ':8766' | Select-String 'LISTENING' | Select-Object -First 1
$lpid = (($line -split '\s+')[-1] + '').Trim()
L ("listener pid '" + $lpid + "'")
if ($lpid -match '^\d+$') {
    Stop-Process -Id ([int]$lpid) -Force
    L ("killed " + $lpid)
} else { L 'no listener pid parsed' }

$out  = "$root\logs\tlego-stdout.log"
$arch = "$root\logs\archive"
New-Item -ItemType Directory -Force -Path $arch | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$sizeMB = [int]((Get-Item $out).Length / 1MB)
L ("log size MB " + $sizeMB)
$moved = $false
if ($sizeMB -gt 100) {
    for ($i = 0; $i -lt 400; $i++) {
        try { Move-Item -LiteralPath $out -Destination "$arch\tlego-stdout-$stamp.log" -ErrorAction Stop; $moved = $true; break }
        catch { Start-Sleep -Milliseconds 250 }
    }
}
L ("rotated=" + $moved)
if ($moved) {
    Set-Content -LiteralPath $out -Value '' -Encoding utf8
    Start-Process cmd.exe -ArgumentList '/c', "tar -czf `"$arch\tlego-stdout-$stamp.log.tgz`" -C `"$arch`" `"tlego-stdout-$stamp.log`" && del `"$arch\tlego-stdout-$stamp.log`"" -WindowStyle Hidden
}

Start-Process -FilePath cmd.exe -ArgumentList '/c', "$root\run_tlego_service.bat" -WorkingDirectory $root -WindowStyle Hidden
L 'relaunched bat'

$newGen = ''
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 10
        if ($r.runtime_generation_id -and $r.runtime_generation_id -ne $oldGen) { $newGen = $r.runtime_generation_id; L ("new gen=" + $newGen + " uptime=" + [int]$r.uptime_s); break }
    } catch {}
}
if (-not $newGen) { L 'NO NEW GENERATION - restart failed' }
L '=== done ==='

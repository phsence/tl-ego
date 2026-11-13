# restart_core_rotate_log.ps1 - 2026-11-05
# 1) bounded idle wait (running_l5/running_comm null, max 10 min)
# 2) kill the :8766 listener
# 3) rotate logs\tlego-stdout.log (>100MB) into logs\archive + gzip in background
# 4) relaunch via run_tlego_service.bat (single source of env)
# 5) verify by listener + /status generation/tick (never by process existence)
$ErrorActionPreference = 'SilentlyContinue'
$root = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'
$log  = "$root\logs\restart_rotate.log"
function L($m) { "$([DateTime]::Now.ToString('HH:mm:ss'))] $m" | Out-File -FilePath $log -Append -Encoding utf8 }

L '=== core restart + log rotate start ==='

# 1) idle wait
$idle = $false
for ($i = 0; $i -lt 120; $i++) {
    $s = Get-Content "$root\..\.lae\ego_dispatch_state.json" -Raw | ConvertFrom-Json
    if (-not $s.running_l5 -and -not $s.running_comm) { $idle = $true; break }
    Start-Sleep -Seconds 5
}
L ("idle=" + $idle + " after " + ($i * 5) + "s")

# 2) kill listener
$line = netstat -ano | Select-String ':8766' | Select-String 'LISTENING' | Select-Object -First 1
$lpid = ($line -split '\s+')[-1]
L "listener pid $lpid"
if ($lpid -match '^\d+$') { taskkill /F /PID $lpid | Out-Null }

# 3) rotate
$out  = "$root\logs\tlego-stdout.log"
$arch = "$root\logs\archive"
New-Item -ItemType Directory -Force -Path $arch | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$size  = (Get-Item $out).Length / 1MB
L ("log size MB " + [int]$size)
$moved = $false
if ($size -gt 100) {
    for ($i = 0; $i -lt 30; $i++) {
        try { Move-Item -LiteralPath $out -Destination "$arch\tlego-stdout-$stamp.log" -ErrorAction Stop; $moved = $true; break }
        catch { Start-Sleep -Seconds 1 }
    }
}
L ("rotated=" + $moved)
if ($moved) {
    Start-Process cmd.exe -ArgumentList '/c', "tar -czf `"$arch\tlego-stdout-$stamp.log.tgz`" -C `"$arch`" `"tlego-stdout-$stamp.log`" && del `"$arch\tlego-stdout-$stamp.log`"" -WindowStyle Hidden
}

# 4) relaunch
Start-Process -FilePath cmd.exe -ArgumentList '/c', "$root\run_tlego_service.bat" -WorkingDirectory $root -WindowStyle Hidden
L 'relaunched bat'

# 5) verify
$up = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 2
    if (netstat -ano | Select-String ':8766' | Select-String 'LISTENING') { $up = $true; L ("listener up after " + ($i * 2 + 2) + "s"); break }
}
try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 20
    L ("gen=" + $r.runtime_generation_id + " uptime=" + [int]$r.uptime_s)
} catch { L 'status probe failed' }
L '=== done ==='

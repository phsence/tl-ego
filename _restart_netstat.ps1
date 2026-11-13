# Elevated restart without Get-NetTCPConnection (that cmdlet hung on 2026-11-03 08:53 UTC+8).
# Kills the stuck previous script, frees 8766 via netstat, restarts the official launcher.
$ErrorActionPreference = 'Continue'
$Port    = 8766
$ProjDir = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'
$Bat     = Join-Path $ProjDir 'run_tlego_service.bat'
$LogPath = Join-Path $ProjDir 'logs\restart_netstat.log'
function L($m) { "$([DateTime]::Now.ToString('HH:mm:ss')) $m" | Out-File $LogPath -Append -Encoding utf8 }

L "requester=$env:USERNAME pid=$PID"

# 0. clear stuck restart scripts (WMI/CIM queries hang on this box right now —
#    both Get-NetTCPConnection and Get-CimInstance stall), so kill by known PID.
foreach ($stuck in @(33760, 43912)) {
    & taskkill /F /PID $stuck 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { L "killed stuck script $stuck" }
}

# 1. free the port (netstat, not the hanging cmdlet)
for ($i = 0; $i -lt 5; $i++) {
    $line = (netstat -ano | Select-String ":$Port\s+.*LISTENING").ToString()
    if (-not $line) { break }
    $procId = ($line -split '\s+')[-1]
    if (-not $procId) { break }
    L "killing listener pid=$procId"
    try { Stop-Process -Id ([int]$procId) -Force -ErrorAction Stop; L "  killed $procId" }
    catch { L "  KILL FAILED $procId : $_" }
    Start-Sleep -Seconds 4
}
$left = (netstat -ano | Select-String ":$Port\s+.*LISTENING")
if ($left) { L "ABORT: port still listening: $left"; exit 3 }
L "port $Port confirmed free"

# 2. start through the official launcher
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $Bat -WorkingDirectory $ProjDir -WindowStyle Hidden
L "launcher started"

# 3. prove it
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $s = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/status" -TimeoutSec 5
        if ($s.uptime_s -lt 120) { L "OK gen=$($s.runtime_generation_id) uptime=$([math]::Round($s.uptime_s,1))s"; exit 0 }
    } catch { }
}
L "ABORT: no fresh /status"; exit 5

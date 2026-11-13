# Elevated TL-Ego (8766) restart that PROVES the new process is serving.
#
# Why this exists: the older one-shot scripts printed "port free after 20 s" from a fixed
# sleep loop without re-checking, then read /status and announced success. When the kill
# was silently refused (Session 0 / SYSTEM-owned generation), that /status was answered by
# the process it had failed to kill -- log said success, old code kept running.
#
# Contract: exit code is the verdict, not the log text.
#   0 = new listener confirmed (port was released, /status ok, fresh uptime)
#   3 = port 8766 still listening after 3 kill attempts -- nothing was started
#   4 = /status came back with a suspiciously old uptime -- start may have failed
#   5 = no healthy /status within the wait window
#
# Usage (must be elevated; Stop-Process on a SYSTEM generation is refused otherwise):
#   powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\_restart_elev_fix.ps1' -Wait"
# -Wait blocks until the elevated child exits, so the caller sees the real code.
# Then read logs\restart_elev_fix.log for the step trace (UTF-8, timestamps on every line).

$ErrorActionPreference = 'Continue'
$Port     = 8766
$ProjDir  = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'
$Bat      = Join-Path $ProjDir 'run_tlego_service.bat'
$LogPath  = Join-Path $ProjDir 'logs\restart_elev_fix.log'

function L($m) { "$([DateTime]::Now.ToString('HH:mm:ss')) $m" | Out-File $LogPath -Append -Encoding utf8 }

$elevated = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
L "requester=$env:USERNAME pid=$PID elevated=$elevated"
if (-not $elevated) { L "ABORT: not elevated -- the kill would be silently refused"; exit 6 }

# --- 1. kill until the port is actually released (re-query every attempt) ---
for ($try = 0; $try -lt 3; $try++) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { break }
    foreach ($p in ($conns.OwningProcess | Sort-Object -Unique)) {
        L "killing listener pid=$p"
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" -ErrorAction SilentlyContinue |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; L "  child killed $($_.ProcessId)" } catch {} }
        try { Stop-Process -Id $p -Force -ErrorAction Stop; L "  killed $p" }
        catch { L "  KILL FAILED $p : $_" }
    }
    Start-Sleep -Seconds 3
}

# --- 2. assert, do not assume ---
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) {
    L "ABORT: port $Port still listening (pid $($still.OwningProcess -join ',')) after 3 attempts"
    exit 3
}
L "port $Port confirmed free"

# --- 3. start through the official launcher (never bare python: needs PYTHONPATH + EGO_* env) ---
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c',$Bat -WorkingDirectory $ProjDir -WindowStyle Hidden

# --- 4. wait for a healthy status from a process with a young uptime ---
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $s = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/status" -TimeoutSec 4
        if ($s.status -eq 'ok') {
            L "status ok gen=$($s.runtime_generation_id) uptime=$([math]::Round($s.uptime_s,1))s"
            if ($s.uptime_s -lt 120) { L 'OK fresh process'; exit 0 }
            L "SUSPECT: uptime $($s.uptime_s)s looks like the old process; check the listener pid"
            exit 4
        }
    } catch {}
}
L 'ABORT: no healthy /status after 120s (slow boots happen -- netstat + /status before re-running)'
exit 5

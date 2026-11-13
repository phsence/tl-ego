# restart_tlego_freedispatch.ps1 — 2026-10-13 free-dispatch restart (no WMI)
# Kills the tl_ego_server tree and relaunches run_tlego_service.bat, then verifies
# by netstat listener + /status runtime_generation_id (NOT by process existence).
$ErrorActionPreference = "SilentlyContinue"
$log = "C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_freedispatch.log"
function L($m) { "$([DateTime]::Now.ToString('HH:mm:ss'))] $m" | Out-File -FilePath $log -Append -Encoding utf8 }

L "=== free-dispatch restart start ==="

# 1) find listener pids on 8766 (no Get-CimInstance: WMI stalls were the old failure)
$listen = netstat -ano | Select-String ":8766" | Select-String "LISTENING"
$pids = @()
foreach ($line in $listen) {
    $p = ($line -split "\s+")[-1]
    if ($p -match "^\d+$") { $pids += [int]$p }
}
$pids = $pids | Sort-Object -Unique
L ("listener pids: " + ($pids -join ","))

# 2) kill python tree by cmdline match (server + launcher bat)
$victims = Get-Process -Name python,cmd | Where-Object {
    $_.Path -ne $null -or $true
}
foreach ($proc in $victims) {
    try {
        $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.Id)").CommandLine
    } catch { $cl = "" }
    if ($cl -match "tl_ego_server\.py|run_tlego_service\.bat") {
        L "kill $($proc.Name) pid=$($proc.Id)"
        Stop-Process -Id $proc.Id -Force
    }
}
Start-Sleep -Seconds 3

# 3) relaunch via bat (single source of env)
Start-Process -FilePath cmd.exe -ArgumentList "/c","C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat" `
    -WorkingDirectory "C:\Users\Administrator\.openclaw\workspace\projects\tl-ego" -WindowStyle Hidden
L "relaunched bat"

# 4) wait for listener
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $l2 = netstat -ano | Select-String ":8766" | Select-String "LISTENING"
    if ($l2) { L ("listener up after " + ($i * 2 + 2) + "s"); break }
}

# 5) verify /status
try {
    $st = Invoke-RestMethod -Uri "http://127.0.0.1:8766/status" -TimeoutSec 10
    L ("status=" + $st.status + " gen=" + $st.runtime_generation_id + " uptime=" + [int]$st.uptime_s + " tick=" + $st.tick)
} catch {
    L "status probe failed: $($_.Exception.Message)"
}
L "=== done ==="

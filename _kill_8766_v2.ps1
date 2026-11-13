$ErrorActionPreference = "Continue"
$log = "C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\kill_8766_v2.log"
"requester: $env:USERNAME self_pid=$PID at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File -Append -Encoding utf8 $log
# netstat 取 PID (不用 WMI/Get-NetTCPConnection —— 实测会挂)
$lines = netstat -ano | Select-String "127\.0\.0\.1:8766\s+.*LISTENING"
if (-not $lines) { "no listener on 8766 (already free)" | Out-File -Append -Encoding utf8 $log }
foreach ($l in $lines) {
    $pid8766 = ($l.ToString().Trim() -split '\s+')[-1]
    "listening: $pid8766" | Out-File -Append -Encoding utf8 $log
    & taskkill /F /PID $pid8766 2>&1 | Out-File -Append -Encoding utf8 $log
    Start-Sleep -Seconds 2
}
Start-Sleep -Seconds 3
$again = netstat -ano | Select-String "127\.0\.0\.1:8766\s+.*LISTENING"
if ($again) { "PORT STILL BUSY" | Out-File -Append -Encoding utf8 $log } else { "PORT FREE" | Out-File -Append -Encoding utf8 $log }

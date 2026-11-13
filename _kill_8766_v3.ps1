$log = "C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\kill_8766_v3.log"
function L($m) { "$m" | Out-File -Append -Encoding utf8 $log }
L "start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') self_pid=$PID"
# 旧的提权脚本(22068)挂在 Get-NetTCPConnection 上, 先清掉
Stop-Process -Id 22068 -Force -ErrorAction SilentlyContinue
L "cleared hung kill-script 22068 (if present)"
$t = @(netstat -ano | Select-String "127\.0\.0\.1:8766\s+.*LISTENING")
if ($t.Count -gt 0) {
    $tp = ($t[0].ToString().Trim() -split '\s+')[-1]
    L "target python pid=$tp"
    Stop-Process -Id ([int]$tp) -Force -ErrorAction SilentlyContinue
    L "Stop-Process issued"
} else { L "no listener on 8766" }
Start-Sleep -Seconds 4
$again = @(netstat -ano | Select-String "127\.0\.0\.1:8766\s+.*LISTENING")
if ($again.Count -gt 0) { L "PORT STILL BUSY: $($again[0])" } else { L "PORT FREE" }

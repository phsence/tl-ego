# restart_tickcadence_20261018.ps1 — UAC-elevated nssm restart for TL-Ego-Server (8766)
# Reason: deploy bounded edge-growth patch (edge_sparsifier.py) — tick cadence collapse fix.
$ErrorActionPreference = "Continue"
$log = "C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\.lae\restart_tickcadence_20261018.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
"=== restart start $(Get-Date -Format o) ===" | Out-File $log -Encoding utf8

sc.exe stop TL-Ego-Server 2>&1 | Out-File $log -Append -Encoding utf8
Start-Sleep -Seconds 8

$conn = netstat -ano | Select-String ":8766\s.*LISTENING"
"after_stop_port: $conn" | Out-File $log -Append -Encoding utf8

if ($conn) {
  $procIds = ($conn -replace '.*\s(\d+)$','$1') | Sort-Object -Unique
  foreach ($p in $procIds) {
    "killing orphan PID $p" | Out-File $log -Append -Encoding utf8
    taskkill /F /PID $p 2>&1 | Out-File $log -Append -Encoding utf8
  }
  Start-Sleep -Seconds 3
}

$still = netstat -ano | Select-String ":8766\s.*LISTENING"
"port8766_after_kill: $still" | Out-File $log -Append -Encoding utf8

$pidFile = "C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\ego.pid"
if (Test-Path $pidFile) { Remove-Item $pidFile -Force; "ego.pid removed" | Out-File $log -Append -Encoding utf8 }

sc.exe start TL-Ego-Server 2>&1 | Out-File $log -Append -Encoding utf8
Start-Sleep -Seconds 8

$ready = netstat -ano | Select-String ":8766\s.*LISTENING"
"port8766_after_start: $ready" | Out-File $log -Append -Encoding utf8

$st = (sc.exe query TL-Ego-Server | Out-String)
$st | Out-File $log -Append -Encoding utf8

"=== restart done $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8

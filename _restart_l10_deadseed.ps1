# Restart TL-Ego after the L10 dead-type seed injection change.
$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\restart_l10_deadseed.log'
"requester: $env:USERNAME self_pid=$PID" | Out-File $log -Append -Encoding utf8

# find the listening PID on 8766 and its process tree
$conns = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
$pids = @()
foreach ($c in $conns) { $pids += $c.OwningProcess }
$pids = $pids | Sort-Object -Unique
"listening_pids: $($pids -join ',')" | Out-File $log -Append -Encoding utf8

foreach ($p in $pids) {
    # kill the whole tree: cmd(bat) -> python shell -> python listener
    $tree = Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" -ErrorAction SilentlyContinue
    foreach ($t in $tree) {
        try { Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
              "killed child $($t.ProcessId) $($t.Name)" | Out-File $log -Append -Encoding utf8 } catch {}
    }
    try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
          "killed $p" | Out-File $log -Append -Encoding utf8 } catch { "kill failed $p : $_" | Out-File $log -Append -Encoding utf8 }
}

# wait for the port to be released
for ($i=0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    $still = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
    if (-not $still) { break }
}
"port free after $i s" | Out-File $log -Append -Encoding utf8

Start-Process -FilePath 'cmd.exe' `
  -ArgumentList '/c','C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat' `
  -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' `
  -WindowStyle Hidden

for ($i=0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $s = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 4
        if ($s.status -eq 'ok') {
            "NEW_GEN $($s.runtime_generation_id) uptime=$($s.uptime_s)" | Out-File $log -Append -Encoding utf8
            break
        }
    } catch {}
}
"done" | Out-File $log -Append -Encoding utf8

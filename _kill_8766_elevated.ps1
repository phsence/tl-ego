# Kill the TL-Ego 8766 listener (elevated). Start is a SEPARATE non-elevated step on purpose:
# a kill failure must not be able to swallow the start.
$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\kill_8766_elevated.log'
"requester: $env:USERNAME self_pid=$PID at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $log -Append -Encoding utf8
$conns = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
$pids = @($conns | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique)
"listening: $($pids -join ',')" | Out-File $log -Append -Encoding utf8
foreach ($p in $pids) {
    $tree = Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" -ErrorAction SilentlyContinue
    foreach ($t in $tree) {
        try { Stop-Process -Id $t.ProcessId -Force -ErrorAction Stop
              "killed child $($t.ProcessId) $($t.Name)" | Out-File $log -Append -Encoding utf8 }
        catch { "child kill failed $($t.ProcessId): $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }
    }
    try { Stop-Process -Id $p -Force -ErrorAction Stop
          "killed $p" | Out-File $log -Append -Encoding utf8 }
    catch { "kill failed $p : $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }
}
Start-Sleep -Seconds 2
$still = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if ($still) { "PORT STILL BUSY" | Out-File $log -Append -Encoding utf8 } else { "PORT FREE" | Out-File $log -Append -Encoding utf8 }

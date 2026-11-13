# Elevated swap: stop 8766, archive the corrupt grid_edges.sqlite, install the rebuilt one,
# start the service, prove a NEW generation came up. Mirrors _restart_elev_fix.ps1.
$log = 'H:\grid_rebuild\swap.log'
function L($m) { "$([DateTime]::Now.ToString('HH:mm:ss')) $m" | Out-File $log -Append -Encoding utf8 }

$NEW = 'H:\grid_rebuild\grid_edges.rebuilt.sqlite'
$DST = 'C:\Users\Administrator\.openclaw\skills\neural_grid\data\grid_edges.sqlite'
$BAK = 'H:\grid_rebuild\grid_edges.sqlite.corrupt'

L "start elevated=$((New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"

# 1. stop the real listener (session 0 / SYSTEM-owned)
for ($try = 0; $try -lt 3; $try++) {
    $conns = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { break }
    foreach ($p in ($conns.OwningProcess | Sort-Object -Unique)) {
        L "killing listener pid=$p"
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" -ErrorAction SilentlyContinue |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; L "  child killed $($_.ProcessId)" } catch {} }
        try { Stop-Process -Id $p -Force -ErrorAction Stop; L "  killed $p" } catch { L "  KILL FAILED $p : $_" }
    }
    Start-Sleep -Seconds 3
}
if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) { L 'ABORT: 8766 still listening'; exit 3 }
L 'port confirmed free'

# 2. archive the corrupt db (27.6GB) so C: has room for the 28.3GB replacement
if (Test-Path $DST) {
    if (Test-Path $BAK) { Remove-Item $BAK -Force }
    L "moving corrupt db -> $BAK"
    Move-Item $DST $BAK -Force
}
Remove-Item "$DST-wal", "$DST-shm" -Force -ErrorAction SilentlyContinue

# 3. install the rebuilt db
L "copying $NEW -> $DST"
Copy-Item $NEW $DST -Force
$sz = (Get-Item $DST).Length / 1GB
L ("installed {0:N1} GB" -f $sz)
if ($sz -lt 25) { L 'ABORT: installed file too small'; exit 4 }

# 4. start
Start-Process -FilePath 'cmd.exe' `
  -ArgumentList '/c','C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat' `
  -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' `
  -WindowStyle Hidden

for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $s = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status' -TimeoutSec 4
        if ($s.status -eq 'ok') {
            L "NEW_GEN $($s.runtime_generation_id) uptime=$([math]::Round($s.uptime_s,1))"
            L "edge_store active_sources=$($s.edge_store.active_source_count) active_edges=$($s.edge_store.active_edge_count) cold_edges=$($s.edge_store.cold_edge_count) total=$($s.edge_store.total_edge_count)"
            if ($s.uptime_s -lt 120) { L 'OK fresh process'; exit 0 }
            L "SUSPECT: uptime $($s.uptime_s)s looks like the old process"; exit 4
        }
    } catch {}
}
L 'ABORT: no healthy /status after 120s'
exit 5

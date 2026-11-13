# ego_keepalive.ps1 - TL-Ego 保活 (scheduled task, every 2 min)
# 背景: ego 每代仅活 15-95 tick 就静默死亡(无traceback), PRIV 稳定爬升(~100MB/min,
# C层内存, 非Python对象泄漏 - mem-audit 的 gc_objs 稳定可证)。根因未定位(2个月历史问题),
# 本脚本做稳定化: 没listener就拉活, 心跳 stale>25min(超过实测最长单tick 1465s)就杀后拉活。
$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\keepalive.log'
function L($m) { "$([DateTime]::Now.ToString('MM-dd HH:mm:ss'))] $m" | Out-File -FilePath $log -Append -Encoding utf8 }

$proj = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'
$listen = netstat -ano | Select-String ':8766' | Select-String 'LISTENING'

if ($listen) {
    $hb = 'C:\Users\Administrator\.openclaw\workspace\.lae\.heartbeat_diag.log'
    if (Test-Path $hb) {
        $last = Get-Content $hb -Tail 1
        if ($last -match 'ts=(\d+)') {
            $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$matches[1]
            if ($age -gt 1500) {
                L "STALL heartbeat_age=${age}s -> kill for clean restart"
                $pids = ($listen | ForEach-Object { ($_ -split '\s+')[-1] }) | Sort-Object -Unique
                foreach ($p in $pids) { try { Stop-Process -Id $p -Force -ErrorAction Stop; L "killed $p" } catch { L "kill fail $p" } }
                Start-Sleep -Seconds 5
                $listen = $null
            }
        }
    }
    if ($listen) { exit 0 }
}

L 'no listener -> relaunch'
$env:PYTHONPATH = 'C:\Users\Administrator\.openclaw\skills\neural_grid;C:\Users\Administrator\.openclaw\workspace'
$env:NO_PROXY = '127.0.0.1,localhost'
$env:EGO_GPU_KWTA = '0'
$env:EGO_GPU_SNN = '1'
$env:EGO_LTP_GAIN = '4.0'
$env:EGO_REPAIR_TOOL = '0'
$env:EGO_LLM_MODEL = 'step-5-preview'
$env:EGO_PRED_STATE = 'C:\Users\Administrator\.openclaw\workspace\.lae\predictive_plasticity.json'
$env:EGO_RESTING_KWTA_ROUNDS = '8'
$env:EGO_LLM_API_KEY = (Get-Content "$proj\ego_key.txt" -Raw).Trim()
Start-Process -FilePath cmd.exe -ArgumentList '/c', "cd /d $proj && C:\Users\Administrator\unsloth_env\Scripts\python.exe tl_ego_server.py >> logs\tlego-stdout.log 2>&1" -WindowStyle Hidden
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 4
    $l2 = netstat -ano | Select-String ':8766' | Select-String 'LISTENING'
    if ($l2) { L "listener up after $($i * 4 + 4)s"; exit 0 }
}
L 'relaunch FAILED - no listener after 120s'

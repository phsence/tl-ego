# start_shim_8770.ps1 - relaunch the ego numen shim (127.0.0.1:8770) with env + logs
# 2026-10-18: 旧启动器 run_ego_api_service.bat 只剩 .bak，用这个重拉并保留 stdout。
$ErrorActionPreference = 'Stop'
$CK = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'
Set-Location $CK
$env:PYTHONPATH = 'C:\Users\Administrator\.openclaw\skills\neural_grid;C:\Users\Administrator\.openclaw\workspace'
$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = '127.0.0.1,localhost'
$key = (Get-Content (Join-Path $CK 'ego_key.txt') -Raw).Trim()
if ($key.Length -lt 30) { throw "ego_key.txt too short: $($key.Length)" }
$env:STEPFUN_API_KEY = $key
$env:EGO_LLM_API_KEY = $key
$py = 'C:\Users\Administrator\unsloth_env\Scripts\python.exe'
Start-Process -FilePath $py -ArgumentList 'ego_openai_shim.py', '--port', '8770' `
    -WorkingDirectory $CK `
    -RedirectStandardOutput (Join-Path $CK 'logs\ego_shim_8770.out.log') `
    -RedirectStandardError (Join-Path $CK 'logs\ego_shim_8770.err.log') `
    -WindowStyle Hidden
Write-Host "shim launched (8770), logs: logs\ego_shim_8770.out.log / .err.log"

# Start ego OpenAI shim + memory API
# Runs both services in foreground. Ctrl+C to stop.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Use unsloth_env Python (correct for ego project)
$Python = "H:\unsloth_env\Scripts\python.exe"

# Clean env vars that would break the right Python
$env:PYTHONPATH = $null
$env:PYTHONHOME = $null

Write-Host "[start_ego_api] Using Python: $Python" -ForegroundColor Cyan
Write-Host "[start_ego_api] Project root: $ProjectRoot" -ForegroundColor Cyan

# Start both servers in background jobs
$shimJob = Start-Job -ScriptBlock {
    param($Python, $ProjectRoot)
    Set-Location $ProjectRoot
    & $Python ego_openai_shim.py --port 8770
} -ArgumentList $Python, $ProjectRoot

$memoryJob = Start-Job -ScriptBlock {
    param($Python, $ProjectRoot)
    Set-Location $ProjectRoot
    & $Python ego_memory_api.py --port 8772
} -ArgumentList $Python, $ProjectRoot

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host " ego OpenAI shim:    http://127.0.0.1:8770" -ForegroundColor Green
Write-Host " ego memory API:     http://127.0.0.1:8772" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Endpoints:"
Write-Host "  POST /v1/chat/completions"
Write-Host "  GET  /v1/tools"
Write-Host "  GET  /v1/memory/tool_executions?source=internal_l10"
Write-Host "  GET  /v1/memory/summary"
Write-Host ""
Write-Host "Press Ctrl+C to stop both services."

# Wait for Ctrl+C
try {
    while ($true) {
        Start-Sleep -Seconds 1
    }
} finally {
    Write-Host "`nStopping services..." -ForegroundColor Yellow
    Stop-Job $shimJob -ErrorAction SilentlyContinue
    Stop-Job $memoryJob -ErrorAction SilentlyContinue
    Remove-Job $shimJob -ErrorAction SilentlyContinue
    Remove-Job $memoryJob -ErrorAction SilentlyContinue
}

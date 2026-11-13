# Targeted restart for the 8766 listener only; keeps unrelated Python services alive.
$ErrorActionPreference = 'Stop'
$listener = Get-NetTCPConnection -LocalPort 8766 -State Listen | Select-Object -First 1
if (-not $listener) { throw 'No listener on 8766; refusing ambiguous restart.' }
$oldPid = [int]$listener.OwningProcess
$proc = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid"
if (-not $proc -or $proc.Name -notmatch 'python') { throw "Unexpected 8766 owner $oldPid ($($proc.Name)); refusing." }
$oldGen = (Invoke-RestMethod 'http://127.0.0.1:8766/status' -TimeoutSec 15).runtime_identity.runtime_generation_id
Stop-Process -Id $oldPid -Force
for ($i=0; $i -lt 30; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 1
}
if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) { throw '8766 did not release.' }
Start-Process -FilePath 'C:\Windows\System32\cmd.exe' -ArgumentList '/c','C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat' -WorkingDirectory 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego' -WindowStyle Hidden
for ($i=0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 2
    try {
        $s = Invoke-RestMethod 'http://127.0.0.1:8766/status' -TimeoutSec 5
        $newGen = $s.runtime_identity.runtime_generation_id
        if ($newGen -and $newGen -ne $oldGen) {
            "RESTART_OK old=$oldGen new=$newGen pid=$((Get-NetTCPConnection -LocalPort 8766 -State Listen | Select-Object -First 1).OwningProcess)"
            exit 0
        }
    } catch {}
}
throw 'New runtime generation did not become healthy within 180s.'

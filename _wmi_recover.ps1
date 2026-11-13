# WMI recovery: the whole WMI stack is wedged (tasklist/taskkill/Get-CimInstance/
# Get-NetTCPConnection all hang, and torch's import calls platform.machine()
# -> _wmi_query, so TL-Ego cannot even boot). Kill the provider hosts; Windows
# respawns them on demand. Kernel-level TerminateProcess, no WMI calls here.
$out = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\wmi_recover.log'
function L($m) { "$([DateTime]::Now.ToString('HH:mm:ss')) $m" | Out-File $out -Append -Encoding utf8 }

L "start pid=$PID elevated=$((New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"

$killed = 0
Get-Process -Name WmiPrvSE -ErrorAction SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $killed++; L "killed WmiPrvSE $($_.Id)" }
    catch { L "fail $($_.Id): $_" }
}
L "killed=$killed"
L "done"

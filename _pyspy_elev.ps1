# 2026-11-03: 提权抓栈。TL-Ego 是以 RunAs 启动的, 普通权限 py-spy 报 os error 5。
$out = 'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\pyspy.log'
$spy = 'C:\Users\Administrator\unsloth_env\Scripts\py-spy.exe'
Remove-Item $out -ErrorAction SilentlyContinue
foreach ($pid in @(45820, 47552)) {
    "=== PID $pid ===" | Out-File $out -Append -Encoding utf8
    & $spy dump --pid $pid 2>&1 | Out-File $out -Append -Encoding utf8
}
"done" | Out-File $out -Append -Encoding utf8

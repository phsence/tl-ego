@echo off
rem Proves the exact guard line in run_tlego_service.bat parses and does the right thing.
rem Writes to a log so bash quoting never has to touch this.
set LOG=C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\guard_check.log
echo === run %DATE% %TIME% === >> "%LOG%"
netstat -ano | findstr ":8002 " | findstr LISTENING >> "%LOG%" 2>&1
netstat -ano | findstr ":8002 " | findstr LISTENING >nul || powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','C:\Users\Administrator\.openclaw\workspace\bge8002.cmd' -WindowStyle Hidden"
echo GUARD_RAN >> "%LOG%"

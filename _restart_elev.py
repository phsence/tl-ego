# 2026-11-03: TL-Ego 是提权进程 (由 _restart_elev_fix.ps1 以 RunAs 启动), 普通权限
# 的 psutil kill 会 AccessDenied, 于是"重启"其实没换代码。本脚本必须提权运行。
# 刻意不用 WMI/CIM (本机 WMI 挂死时它们全超时), 只用 psutil + socket。
import os
import socket
import subprocess
import sys
import time

import psutil

BAT = r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\run_tlego_service.bat"
LOG = r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\logs\elev_restart_py.log"


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def port_open(port=8766):
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


log(f"elevated={psutil.Process().username()} port_open={port_open()}")

killed = []
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        if "tl_ego_server.py" in " ".join(p.info["cmdline"] or []):
            p.kill()
            killed.append(p.info["pid"])
    except Exception as e:
        log(f"kill fail {p.info.get('pid')}: {e}")
log(f"killed={killed}")

for _ in range(20):
    if not port_open():
        break
    time.sleep(1)
log(f"port_free={not port_open()}")

subprocess.Popen(["cmd", "/c", BAT], cwd=os.path.dirname(BAT),
                 creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW)
log("launched run_tlego_service.bat")

for i in range(40):
    time.sleep(5)
    if port_open():
        log(f"UP after {i * 5}s")
        sys.exit(0)
log("DID NOT COME UP in 200s")
sys.exit(1)

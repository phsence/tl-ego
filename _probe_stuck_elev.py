# 2026-11-03: 提权诊断启动卡住。普通权限 py-spy 抓不到 (os error 5)。
import glob
import os
import subprocess
import time

import psutil

ROOT = r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego"
LOG = os.path.join(ROOT, "logs", "stuck_diag.log")
SPY = r"C:\Users\Administrator\unsloth_env\Scripts\py-spy.exe"


def log(msg=""):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(msg) + "\n")


log(f"=== diag {time.strftime('%H:%M:%S')} ===")
pids = []
for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
    try:
        if "tl_ego_server.py" in " ".join(p.info["cmdline"] or []):
            pids.append(p.info["pid"])
            log(f"pid {p.info['pid']} started {time.strftime('%H:%M:%S', time.localtime(p.info['create_time']))}")
            try:
                log(f"  threads={p.num_threads()} rss={p.memory_info().rss / 2**30:.2f}GB cpu={p.cpu_percent(1.0)}")
                names = [os.path.basename(f.path) for f in p.open_files()][:10]
                log(f"  files={names}")
            except Exception as e:
                log(f"  info err {e}")
    except Exception:
        pass

for pid in pids:
    log(f"--- py-spy {pid} ---")
    try:
        out = subprocess.run([SPY, "dump", "--pid", str(pid)], capture_output=True, text=True, timeout=60)
        log(out.stdout[:3000] or out.stderr[:1000])
    except Exception as e:
        log(f"spy err {e}")
log("done")

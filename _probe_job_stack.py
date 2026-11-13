# 2026-11-03: 抓 contemplate job 线程在等什么。job 跑在 ego 进程内的 Thread-N。
# py-spy 需要提权 (ego 是 RunAs 启的), 所以这个脚本必须提权跑。
import glob
import os
import re
import subprocess

import psutil

ROOT = r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego"
LOG = os.path.join(ROOT, "logs", "job_stack.log")
SPY = r"C:\Users\Administrator\unsloth_env\Scripts\py-spy.exe"

pid = None
for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
    try:
        if "tl_ego_server.py" in " ".join(p.info["cmdline"] or []):
            pid = p.info["pid"]
    except Exception:
        pass

# 2026-11-03: 不能只取"最后一个" ego 进程 —— 机器上可能有多个残留, 真正服务 8766 的
# 未必是它。用 netstat 找 LISTENING (不碰 WMI), 并对所有候选都抓栈。
listen_pids = set()
try:
    txt = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=30).stdout
    for line in txt.splitlines():
        if ":8766" in line and "listening" in line.lower():
            parts = line.split()
            if parts:
                listen_pids.add(parts[-1])
except Exception:
    pass

ego_pids = []
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        if "tl_ego_server.py" in " ".join(p.info["cmdline"] or []):
            ego_pids.append(p.info["pid"])
    except Exception:
        pass

with open(LOG, "w", encoding="utf-8") as f:
    f.write(f"listen_pids={sorted(listen_pids)} ego_pids={ego_pids}\n")
    for _pid in sorted(listen_pids) or ([pid] if pid else []):
        try:
            out = subprocess.run([SPY, "dump", "--pid", str(_pid)],
                                 capture_output=True, text=True, timeout=180)
            txt = out.stdout or out.stderr
        except Exception as e:
            txt = f"spy err {e}"
        blocks = re.split(r"\n(?=Thread )", txt)
        keep = [b for b in blocks if "contemplate" in b.lower() or "_bg_run" in b]
        f.write(f"\n=== pid {_pid} threads={len(blocks) - 1} "
                f"job_blocks={len(keep)} ===\n")
        f.write("\n\n".join(keep)[:6000] or "NO_JOB_THREAD\n")
        if not keep:
            f.write(txt[:2500])

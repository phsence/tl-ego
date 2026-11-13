# 2026-11-03: 启动卡住时的兜底启动器。
# 走 run_tlego_service.bat 会同时拉起多个 tl_ego_server.py 且 stdout 被缓冲, 卡住时看不到进度。
# 这里直接单进程启动: 清掉所有旧实例 -> 端口释放 -> python -u 前台跑(输出即时可见) -> 轮询端口。
import os
import socket
import subprocess
import sys
import time

import psutil

ROOT = r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego"
PY = r"C:\Users\Administrator\unsloth_env\Scripts\python.exe"
LOG = os.path.join(ROOT, "logs", "direct_start.log")


def port_open(port=8766):
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


killed = []
for p in psutil.process_iter(["pid", "cmdline", "name"]):
    try:
        # 2026-11-03: 只杀 python 进程。曾因为 "tl_ego_server.py" 也出现在
        # bash/py_compile 的命令行里, 把调用者自己的 shell 一起杀了 (exit 15)。
        if (p.info["name"] or "").lower() not in ("python.exe", "pythonw.exe", "py.exe"):
            continue
        if "tl_ego_server.py" in " ".join(p.info["cmdline"] or []):
            p.kill()
            killed.append(p.info["pid"])
    except Exception:
        pass
print("killed", killed, flush=True)
for _ in range(30):
    if not port_open():
        break
    time.sleep(1)
print("port_free", not port_open(), flush=True)

env = dict(os.environ)
env["PYTHONPATH"] = r"C:\Users\Administrator\.openclaw\skills\neural_grid;C:\Users\Administrator\.openclaw\workspace"
env["NO_PROXY"] = "127.0.0.1,localhost"
env["EGO_GPU_SNN"] = "1"
env["EGO_LTP_GAIN"] = "4.0"
env["EGO_REPAIR_TOOL"] = "0"
env["EGO_PRED_STATE"] = r"C:\Users\Administrator\.openclaw\workspace\.lae\predictive_plasticity.json"
env["EGO_RESTING_KWTA_ROUNDS"] = "8"
env["EGO_LLM_MODEL"] = "step-5-preview"
# 2026-11-03: 试跑次数透传 (默认 3 = 原行为)。验证交互延迟时用 EGO_KWTA_TRIALS=1 启动。
env["EGO_KWTA_TRIALS"] = os.environ.get("EGO_KWTA_TRIALS", "1")
# 2026-11-03: 提权(RunAs)不继承调用方环境变量 —— 实测 `$env:EGO_PARAM_FLOORS` 传不进来,
# apply 完 value 仍是 3.0。所以旋钮必须落在启动器这份 env 里。
# kwta_pool_mult 下限: 自主调参曾把它从 10.0 悄悄改回 3.0, n_act 1800 -> 540。
env["EGO_PARAM_FLOORS"] = os.environ.get("EGO_PARAM_FLOORS", "kwta_pool_mult=10")

with open(LOG, "ab") as out:
    out.write(b"\n=== direct start %s ===\n" % time.strftime("%H:%M:%S").encode())
    out.flush()
    proc = subprocess.Popen([PY, "-u", "tl_ego_server.py"], cwd=ROOT, env=env,
                            stdout=out, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW)
print("spawned pid", proc.pid, flush=True)

for i in range(48):
    time.sleep(5)
    if port_open():
        print(f"UP after {i * 5}s (pid {proc.pid})", flush=True)
        sys.exit(0)
print("NOT LISTENING after 240s", flush=True)
sys.exit(1)

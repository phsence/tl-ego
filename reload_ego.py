"""Reload the TL-Ego server generation with the current source on disk.

Why this has to exist as a script and not as a taskkill: the live generation runs
in session 0 under a principal this shell cannot touch (taskkill -> access denied,
schtasks/sc are on the local program blacklist, Start-Process -Verb RunAs is
refused by the command validator). What we CAN do is drive the machine's own
watchdog: ego_keepalive.py decides "wedged" from the newest `ts=<epoch>` in the
tail of .lae/.heartbeat_diag.log, and it can spawn but cannot kill. So the one
piece only an operator can supply is the kill; everything after it is automatic.

    Right-click this file -> run as administrator is enough:
        H:/unsloth_env/Scripts/python.exe reload_ego.py

It saves a checkpoint first, kills the listener, waits for the port to free, then
spawns the replacement with exactly the ENV ego_keepalive.py uses (single source
of truth for the launch environment), pointing stdout at a log we are allowed to
write. If the canonical log is unwritable it falls back instead of dying with
PermissionError -- that bug is why the watchdog could not respawn for hours.

Exit codes: 0 reloaded and serving, 2 nothing to reload, 1 failed.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego")
WORKSPACE = BASE.parents[1]
PY = r"C:\Users\Administrator\unsloth_env\Scripts\python.exe"
CANON_LOG = BASE / "logs" / "tlego-stdout.log"
FALLBACK_LOG = BASE / "logs" / "tlego-operator-reload.log"
KEY = BASE / "ego_key.txt"

# Mirrors ego_keepalive.py / run_tlego_service.bat. Keep in sync if either moves.
ENV = dict(os.environ)
ENV["PYTHONPATH"] = r"C:\Users\Administrator\.openclaw\skills\neural_grid;C:\Users\Administrator\.openclaw\workspace"
ENV["NO_PROXY"] = "127.0.0.1,localhost"
ENV["no_proxy"] = "127.0.0.1,localhost"
ENV["EGO_GPU_KWTA"] = "0"
ENV["EGO_GPU_SNN"] = "1"
ENV["EGO_LTP_GAIN"] = "4.0"
ENV["EGO_REPAIR_TOOL"] = "0"
ENV["EGO_LLM_MODEL"] = "step-5-preview"
ENV["EGO_PRED_STATE"] = str(WORKSPACE / ".lae" / "predictive_plasticity.json")
ENV["EGO_RESTING_KWTA_ROUNDS"] = "5"
ENV["EGO_STDP_MAX_SRCS"] = "400"
ENV["EGO_STDP_BUDGET_S"] = "20"
ENV["EGO_STAG_KWTA_ROUNDS"] = "6"
try:
    ENV["EGO_LLM_API_KEY"] = KEY.read_text(encoding="utf-8").strip()
except Exception:                                          # noqa: BLE001
    pass


def status(timeout=8):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=timeout) as r:
            return json.load(r)
    except Exception:                                      # noqa: BLE001
        return None


def listener_pids(port=8766):
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
    except Exception:                                      # noqa: BLE001
        return set()
    pids = set()
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            m = re.search(r"(\d+)\s*$", line)
            if m:
                pids.add(int(m.group(1)))
    return pids


def pick_log():
    """Prefer the canonical log; fall back rather than crash on PermissionError."""
    for p in (CANON_LOG, FALLBACK_LOG):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8"):
                return p
        except Exception:                                  # noqa: BLE001
            continue
    raise SystemExit(f"neither {CANON_LOG} nor {FALLBACK_LOG} is writable -- cannot capture server output")


def save_checkpoint():
    try:
        req = urllib.request.Request("http://127.0.0.1:8766/save-checkpoint", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:                                 # noqa: BLE001
        return {"status": "error", "error": str(e)}


def main():
    st = status()
    if st is None:
        print("8766 is not answering. If it is down, ego_keepalive will relaunch it within 2 minutes;"
              " nothing to do here.")
        return 2
    old_gen = st.get("runtime_generation_id")
    print(f"live generation : {old_gen}  (uptime {st.get('uptime_s')}s, tick {st.get('resting_ticks')})")

    cp = save_checkpoint()
    print(f"checkpoint      : {cp.get('status')} {cp.get('connections_saved', '')} "
          f"{cp.get('error', '')}".rstrip())

    pids = listener_pids()
    if not pids:
        print("no listener pid found")
        return 1
    print(f"killing         : {sorted(pids)}")
    denied = []
    for pid in sorted(pids):
        # taskkill writes GBK on this box; decoding as utf-8 raises inside the reader
        # thread and hides the real result.
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        msg = (r.stdout + r.stderr).decode("gbk", "replace").strip()
        ok = r.returncode == 0
        print(f"  {pid}: {'ok' if ok else 'DENIED'} {msg}")
        if not ok:
            denied.append(pid)
    if denied:
        print("\nSTILL DENIED. This shell is not elevated -- run this file from an elevated"
              " (Administrator) prompt; see the module docstring.")
        return 1

    for i in range(40):
        if not listener_pids():
            print(f"port free after {i}s")
            break
        time.sleep(1)
    else:
        print("port still held after 40s; not starting a second instance")
        return 1

    log = pick_log()
    print(f"spawning        : stdout -> {log}")
    with open(log, "a", encoding="utf-8") as out:
        out.write(f"\n[operator-reload] {time.strftime('%Y-%m-%d %H:%M:%S')} replacing {old_gen}\n")
        out.flush()
        subprocess.Popen(
            [PY, "tl_ego_server.py"], cwd=str(BASE), env=ENV,
            creationflags=0x00000008 | 0x00000200,        # DETACHED_PROCESS | NEW_PROCESS_GROUP
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            close_fds=True,
        )

    print("waiting for boot (10M neurons, ~2 min) ...")
    t0 = time.time()
    while time.time() - t0 < 420:
        time.sleep(5)
        st = status(timeout=6)
        if st and st.get("runtime_generation_id") != old_gen:
            print(f"NEW generation  : {st['runtime_generation_id']}  uptime={st.get('uptime_s')}s")
            req = urllib.request.Request(
                "http://127.0.0.1:8766/causal-arm",
                data=json.dumps({"key": "type_dominance_cap", "read": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    print(f"/causal-arm    : {json.load(r)}")
            except Exception as e:                         # noqa: BLE001
                print(f"/causal-arm    : NOT AVAILABLE ({e})")
            return 0
    print("TIMEOUT: no new generation after 420s")
    return 1


if __name__ == "__main__":
    sys.exit(main())

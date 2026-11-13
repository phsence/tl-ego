"""telemetry_selfcheck.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: server 469 imports run_telemetry_selfcheck; 485-495 calls it with NO args behind
a 300s cache ("l6p diagnose-the-diagnoser"); 23306 exposes the result as
status["telemetry_selfcheck"]. The comment at 474-476 names what it must check:
split-brain / freshness / roundtrip / stale repair state.
Judgement (not measured): thresholds (fresh 1800s, repair 86400s, lock 21600s) and the
issue strings are rebuild choices; the checks themselves come from that comment.
"""
import json
import os
import time
from pathlib import Path

FRESH_MAX_S = 1800.0
REPAIR_STALE_S = 86400.0
LOCK_STALE_S = 21600.0
WATCH = ("causal_events.jsonl", "lae_self_model.json", "stage3_timeline.jsonl")
PROBE = b"tl-ego-roundtrip-probe"


def _workspace():
    ws = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("EGO_WORKSPACE")
    return Path(ws) if ws else Path(__file__).resolve().parents[2]


def _age(p):
    try:
        return time.time() - p.stat().st_mtime
    except Exception:
        return None


def _pid_alive(pid):
    """Liveness probe. NEVER os.kill(pid, 0) on Windows: that is TerminateProcess, not a check
    (CPython docs: any sig other than CTRL_C/CTRL_BREAK kills the process unconditionally).
    Returns True / False / None (None = could not determine, e.g. access denied)."""
    pid = int(pid)
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259          # STILL_ACTIVE
                return None
            finally:
                k32.CloseHandle(h)
    except Exception:
        pass
    try:                                          # fallback: match by pid in tasklist
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, timeout=10, encoding="gbk", errors="replace").stdout or ""
        if str(pid) in out:
            return True
        return None                               # tasklist absent/parsing failure -> unknown
    except Exception:
        return None


def run_telemetry_selfcheck():
    t0 = time.time()
    lae = _workspace() / ".lae"
    issues, checks = [], {}
    fresh = {}
    for name in WATCH:
        a = _age(lae / name)
        fresh[name] = None if a is None else round(a, 1)
        if a is None:
            issues.append("missing:" + name)
        elif a > FRESH_MAX_S:
            issues.append("stale:%s(%.0fs)" % (name, a))
    checks["freshness_s"] = fresh

    pid, alive = None, None
    try:
        pid = int((lae / "ego.pid").read_text(encoding="utf-8").strip() or 0) or None
    except Exception:
        pid = None
    if pid:
        alive = _pid_alive(pid)
    checks["pid"] = pid
    checks["pid_alive"] = alive
    checks["pid_probe"] = "alive" if alive is True else ("dead" if alive is False else "unknown")
    checks["split_brain"] = bool(pid and alive is False)
    if checks["split_brain"]:
        issues.append("pid_file_dead:%s" % pid)

    # ponytail: mkdir keeps the roundtrip check meaningful on a fresh workspace;
    # .lae is required infrastructure, and the service would create it at boot anyway.
    try:
        lae.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    probe = lae / "_telemetry_selfcheck.tmp"
    try:
        probe.write_bytes(PROBE)
        ok = probe.read_bytes() == PROBE
        probe.unlink()
    except Exception as e:
        ok = False
        checks["roundtrip_error"] = str(e)[:160]
    checks["roundtrip"] = ok
    if not ok:
        issues.append("roundtrip_failed")

    rp = lae / "l5_repair_state.json"
    ra = _age(rp)
    checks["repair_state_age_s"] = None if ra is None else round(ra, 1)
    try:
        rs = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        rs = None
    suppress = (rs or {}).get("escape_suppress_until_tick") if isinstance(rs, dict) else None
    checks["escape_suppress_until_tick"] = suppress
    if ra is not None and ra > REPAIR_STALE_S and suppress:
        issues.append("stale_repair_state")

    locks = {}
    for d in ("subagent_locks", "l5_goal_result_locks"):
        n = 0
        try:
            for f in (lae / d).iterdir():
                if not f.is_file():
                    continue  # archive dirs are not stale locks
                a = _age(f)
                if a is not None and a > LOCK_STALE_S:
                    n += 1
        except Exception:
            pass
        locks[d] = n
    checks["stale_locks"] = locks
    if sum(locks.values()):
        issues.append("stale_locks:%d" % sum(locks.values()))

    status = "error" if (checks["split_brain"] or not ok) else ("warn" if issues else "ok")
    return {
        "schema": "telemetry_selfcheck.v1",
        "status": status,
        "issues": issues,
        "checks": checks,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "took_ms": round((time.time() - t0) * 1000, 2),
    }


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        os.environ["OPENCLAW_WORKSPACE"] = td
        r = run_telemetry_selfcheck()
        assert r["status"] in ("warn", "error") and any(x.startswith("missing:") for x in r["issues"]), r
        assert r["checks"]["split_brain"] is False and r["checks"]["roundtrip"] is True, r
        assert r["took_ms"] < 2000
        assert _pid_alive(os.getpid()) is True, "live pid must probe alive"
        assert _pid_alive(999999) in (False, None), "bogus pid must not probe alive"
    del os.environ["OPENCLAW_WORKSPACE"]
    real = run_telemetry_selfcheck()
    assert real["schema"] == "telemetry_selfcheck.v1" and isinstance(real["checks"]["freshness_s"], dict)
    print("telemetry_selfcheck selfcheck OK ->", json.dumps({k: real[k] for k in ("status", "issues", "took_ms")}, ensure_ascii=False))

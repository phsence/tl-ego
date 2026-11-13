#!/usr/bin/env python3
"""runtime_policy_loader.py — deployment ledger：记录/回读策略 profile 上线、回滚父本。

2026-10-10 重建（原文件在 09-30 wipe 丢失，全机零副本）。证据来源：
  * 调用方 `run_minecraft_benchmark.py.bak-holdout12-20260922`——record_load /
    find_bridge_pid / rollback_to_parent 的实参、返回值用法（readback.active_sha256 /
    matches_parent / live_generation）；
  * `.lae/deployment/load_records.jsonl` 136 条实样（字段逐一对齐：extra/loaded_at/
    pid/profile_id/runtime_generation_id/sha256）；
  * `.lae/deployment/rollback_records.jsonl` 33 条实样（active_sha256/from_profile/
    readback_matches_parent/reason/rolled_back_at/to_profile）；
  * GitNexus 解析缓存（原文件 230 行的全部符号与行段——本文件按该结构复刻函数集：
    deployment_dir/_append_jsonl/_read_jsonl/fetch_generation/find_bridge_pid/record_load/
    read_load_records/verify_load_readback/rollback_to_parent/read_rollback_records/
    canary_selected/restart_storm/note_restart/restart_allowed）。
口径以实样为准；字节级不保证逐行相同。canary/restart_storm 系列原模块即有、当前无活消费者
（rr harness 仍是缺件），按最小可核语义重建。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
STATUS_URL = "http://127.0.0.1:8766/status"
BRIDGE_JS_MARKER = "tl-ego-bridge"


def deployment_dir(base_dir=None) -> Path:
    d = Path(base_dir) if base_dir else _HERE / ".lae" / "deployment"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_dir(base: Path):
    """profile 目录 = deployment 的兄弟目录 policy_profiles（存在才用，否则走 policy_runtime 默认）。"""
    d = base.parent / "policy_profiles"
    return d if d.exists() else None


def _append_jsonl(path: Path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path: Path) -> list:
    out = []
    if not Path(path).exists():
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def fetch_generation(timeout: float = 4.0) -> str:
    """现役 Ego 进程的 runtime_generation_id（/status）；不可达返回 'unknown'。"""
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=timeout) as fh:
            doc = json.loads(fh.read().decode("utf-8", "replace"))
        return str(doc.get("runtime_generation_id") or doc.get("generation_id") or "unknown")
    except Exception:
        return "unknown"


def find_bridge_pid():
    """MC 桥（node.exe 跑 tl-ego-bridge.js）进程 pid；查不到返回 None。"""
    cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and "
           "$_.CommandLine -match '" + BRIDGE_JS_MARKER + "' } | "
           "Select-Object -First 1 -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def record_load(profile_id, *, sha256, runtime_generation_id=None, pid=None,
                extra=None, base_dir=None) -> dict:
    """追加一条 load 记录并返回它（runner 将其嵌进报告 arm_load_records[arm].load_record.deployment）。"""
    base = deployment_dir(base_dir)
    rec = {
        "profile_id": str(profile_id),
        "sha256": str(sha256),
        "runtime_generation_id": runtime_generation_id,
        "pid": pid,
        "extra": extra or {},
        "loaded_at": time.time(),
    }
    _append_jsonl(base / "load_records.jsonl", rec)
    return rec


def read_load_records(base_dir=None) -> list:
    return _read_jsonl(deployment_dir(base_dir) / "load_records.jsonl")


def read_rollback_records(base_dir=None) -> list:
    return _read_jsonl(deployment_dir(base_dir) / "rollback_records.jsonl")


def _active_pointer(profile_dir) -> dict:
    p = (profile_dir / "active.json") if profile_dir else None
    if p and p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    try:
        import policy_runtime as pr
        return pr.load_active_profile() or {}
    except Exception:
        return {}


def verify_load_readback(profile_id=None, *, base_dir=None) -> dict:
    """回读验证：最新 load 记录 vs 现役 active.json sha + live generation。"""
    base = deployment_dir(base_dir)
    recs = _read_jsonl(base / "load_records.jsonl")
    if not recs:
        return {"ok": False, "reason": "no_records"}
    rec = None
    for r in reversed(recs):
        if profile_id is None or r.get("profile_id") == profile_id:
            rec = r
            break
    if rec is None:
        return {"ok": False, "reason": "no_records"}
    pointer = _active_pointer(_profile_dir(base))
    active_sha = str(pointer.get("sha256") or "")
    live_gen = fetch_generation()
    out = {
        "ok": False,
        "reason": "sha_mismatch",
        "record": rec,
        "active_profile": pointer.get("profile"),
        "active_sha256": active_sha,
        "expected_sha256": rec.get("sha256"),
        "live_generation": live_gen,
    }
    if active_sha == str(rec.get("sha256") or ""):
        if rec.get("runtime_generation_id") in (None, "", "unknown") or live_gen == rec.get("runtime_generation_id"):
            out["ok"] = True
            out["reason"] = "ok"
        else:
            out["reason"] = "generation_changed"
    return out


def rollback_to_parent(parent_id: str = "baseline", *, reason: str = "", base_dir=None) -> dict:
    """回滚到父本 profile → 回读 active.json 验证 sha 链 → 写 rollback_records.jsonl。

    返回 {"to_profile","from_profile","reason","readback":{active_profile,active_sha256,
    parent_sha256,matches_parent,live_generation,readback_at},"record"}。
    """
    import policy_runtime as pr
    base = deployment_dir(base_dir)
    recs = _read_jsonl(base / "load_records.jsonl")
    from_id = recs[-1].get("profile_id") if recs else None
    prof_dir = _profile_dir(base)
    ptr = pr.activate(parent_id, activated_by="rollback_to_parent", base_dir=prof_dir)
    d = _active_pointer(prof_dir)
    active_sha = str(d.get("sha256") or (ptr or {}).get("sha256") or "")
    parent_file = (prof_dir / f"{parent_id}.json") if prof_dir else None
    parent_sha = (hashlib.sha256(parent_file.read_bytes()).hexdigest()
                  if parent_file and parent_file.exists() else None)
    readback = {
        "active_profile": d.get("profile"),
        "active_sha256": active_sha,
        "parent_sha256": parent_sha,
        "matches_parent": bool(parent_sha and active_sha == parent_sha),
        "live_generation": fetch_generation(),
        "readback_at": time.time(),
    }
    rec = {
        "from_profile": from_id,
        "to_profile": parent_id,
        "reason": reason,
        "active_sha256": active_sha,
        "readback_matches_parent": readback["matches_parent"],
        "rolled_back_at": time.time(),
    }
    _append_jsonl(base / "rollback_records.jsonl", rec)
    return {"to_profile": parent_id, "from_profile": from_id, "reason": reason,
            "readback": readback, "record": rec}


def canary_selected(candidate_id, rate: float = 0.1) -> bool:
    """确定性金丝雀抽样：同一 candidate_id 结果恒定；rate∈[0,1]。"""
    h = hashlib.sha256(str(candidate_id).encode("utf-8")).hexdigest()
    frac = int(h[:8], 16) / 0xFFFFFFFF
    return frac < max(0.0, min(1.0, float(rate)))


def restart_storm(*, window_s: float = 1800.0, threshold: int = 3, now=None,
                  base_dir=None) -> dict:
    recs = _read_jsonl(deployment_dir(base_dir) / "restart_events.jsonl")
    now = time.time() if now is None else now
    recent = [r for r in recs if (now - float(r.get("ts") or 0)) <= window_s]
    return {"recent": len(recent), "threshold": threshold, "window_s": window_s,
            "storm": len(recent) >= threshold}


def note_restart(rec: dict | None = None, *, base_dir=None) -> dict:
    rec = {"ts": time.time(), **(rec or {})}
    _append_jsonl(deployment_dir(base_dir) / "restart_events.jsonl", rec)
    return rec


def restart_allowed(*, window_s: float = 1800.0, threshold: int = 3, now=None,
                    base_dir=None) -> bool:
    events = restart_storm(window_s=window_s, threshold=threshold, now=now, base_dir=base_dir)
    return not events["storm"]

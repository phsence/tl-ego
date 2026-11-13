"""Policy runtime: rollback-able strategy parameter profiles for TL-Ego (plan Task 3).

Scope: the ONLY mutable state is a whitelist of low-risk strategy parameters.
  Gate1: key whitelist + range clamping + type check
  Gate2: dry-run validation + atomic write + backup before activation
  Gate3: (elsewhere) decides retain/rollback; this module only loads/activates.

A profile is a JSON file:
  {
    "schema_version": "policy_profile.v1",
    "profile_id": "baseline",
    "parent_id": null,
    "created_at": "...",
    "params": {"reobserve_after_no_motion_s": 10.0, ...}
  }
plus a sidecar sha256 written at activation time (active.json):
  {"profile": "baseline.json", "sha256": "...", "activated_at": ..., "activated_by": ...}

Loading NEVER trusts the pointer blindly: the profile file is re-hashed and the
hash must match active.json, otherwise the loader falls back to the baseline
profile (fail-safe). Files live in .lae/policy_profiles/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "policy_profile.v1"

# Gate1 whitelist: key -> (type, low, high, default)
PARAM_SPEC: dict[str, tuple[type, float, float, Any]] = {
    "reobserve_after_no_motion_s": (float, 1.0, 60.0, 10.0),
    "replan_after_tool_failure":   (bool,  0,   1,   True),
    "hostile_scan_radius":         (int,   4,   64,  32),
    "food_rescue_threshold":       (int,   4,   19,  10),
    "attack_retry_limit":          (int,   0,   10,  2),
    "goal_timeout_s":              (int,   10,  300, 40),
    # 2026-09-14: 指挥官目标带显式 goto JSON 时直接执行(跳过 LLM 咨询)。
    # 动机: 同目标下 LLM 首个决策随机(精确 goto / 方向 move / 被拒格式) 实测同参数臂 11.8s~108s。
    "goal_direct_exec":            (bool,  0,   1,   False),
    # 2026-09-17 果蝇架构: 学习→反射下沉。桥的拦截反射命中计数达标后由 consolidate 脚本
    # 把这两键写进新 profile (三闸门同款白名单), 下次桥直接启用, 不再每次现学。
    "intercept_enabled":           (bool,  0,   1,   True),
    "intercept_max_dist":          (int,   6,   24,  14),
}

PROFILE_DIR_ENV = "EGO_POLICY_PROFILE_DIR"
DEFAULT_PROFILE_DIR = Path(".lae/policy_profiles")
BASELINE_ID = "baseline"
ACTIVE_POINTER = "active.json"


def profile_dir() -> Path:
    d = os.environ.get(PROFILE_DIR_ENV)
    return Path(d) if d else DEFAULT_PROFILE_DIR


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _file_stem(profile_id: str) -> str:
    """Windows-safe file stem for a profile id.

    Benchmark arms build ids like ``cand-1:candidate_on``; the colon is fine
    inside the document but illegal in a Windows filename (WinError 87 on the
    atomic replace), so the on-disk name is sanitized while ``profile_id``
    keeps its exact value.
    """
    stem = _FILENAME_UNSAFE.sub("_", str(profile_id))
    return stem.strip(" .") or "profile"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _coerce(value: Any, typ: type) -> Any:
    """bool must be checked before int (bool is a subclass of int)."""
    if typ is bool:
        if isinstance(value, bool):
            return value
        raise TypeError(f"expected bool, got {value!r}")
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, float) and not value.is_integer():
            raise TypeError(f"expected int, got {value!r}")
        return int(value)
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"expected float, got {value!r}")
        return float(value)
    raise TypeError(f"unsupported type {typ}")


def validate_params(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Gate1. Returns (clamped_params, notes). Unknown keys are REJECTED.

    Out-of-range values are clamped into range and reported, not silently kept.
    """
    params: dict[str, Any] = {}
    notes: list[str] = []
    for key, value in raw.items():
        if key not in PARAM_SPEC:
            notes.append(f"rejected_unknown_key:{key}")
            continue
        typ, low, high, _default = PARAM_SPEC[key]
        try:
            v = _coerce(value, typ)
        except TypeError as e:
            notes.append(f"rejected_bad_type:{key}:{e}")
            continue
        if typ is not bool:
            clamped = min(max(v, low), high)
            if clamped != v:
                notes.append(f"clamped:{key}:{v}->{clamped}")
            v = clamped
        params[key] = v
    for key, (_typ, _low, _high, default) in PARAM_SPEC.items():
        params.setdefault(key, default)
    return params, notes


def write_profile(params: dict[str, Any], profile_id: str, parent_id: Optional[str] = None,
                  *, base_dir: Optional[Path] = None, write: bool = False,
                  created_at: Optional[str] = None) -> dict[str, Any]:
    """Build (and optionally persist) a profile document.

    With write=False this is a pure dry-run: nothing touches the filesystem,
    the caller gets the validated params + computed sha256 of the would-be file.
    created_at overrides the timestamp: evidence/replay callers freeze it so the
    profile sha256 stays reproducible across runs.
    """
    params, notes = validate_params(dict(params))
    doc = {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "parent_id": parent_id,
        "created_at": created_at if created_at is not None else time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "params": params,
    }
    payload = _canonical_json(doc)
    result = {
        "profile_id": profile_id,
        "parent_id": parent_id,
        "params": params,
        "notes": notes,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "written": False,
        "path": None,
    }
    if not write:
        return result

    d = base_dir if base_dir is not None else profile_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{_file_stem(profile_id)}.json"
    tmp = d / f".{_file_stem(profile_id)}.json.tmp"
    # Gate2: backup existing, atomic replace
    if target.exists():
        shutil.copy2(target, d / f"{_file_stem(profile_id)}.json.bak")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    result["written"] = True
    result["path"] = str(target)
    return result


def read_profile(path: Path) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"bad schema_version: {doc.get('schema_version')!r}")
    params, notes = validate_params(dict(doc.get("params") or {}))
    doc["params"] = params
    doc["notes"] = notes
    doc["sha256"] = sha256_file(Path(path))
    return doc


def activate(profile_id: str, *, activated_by: str = "gate2",
             base_dir: Optional[Path] = None) -> dict[str, Any]:
    """Point active.json at a profile (atomic). Hash is stored for load-time verification."""
    d = base_dir if base_dir is not None else profile_dir()
    target = d / f"{_file_stem(profile_id)}.json"
    if not target.exists():
        raise FileNotFoundError(f"profile not found: {target}")
    pointer = {
        "profile": target.name,
        "sha256": sha256_file(target),
        "activated_at": time.time(),
        "activated_by": activated_by,
    }
    tmp = d / f".{ACTIVE_POINTER}.tmp"
    tmp.write_text(_canonical_json(pointer), encoding="utf-8")
    os.replace(tmp, d / ACTIVE_POINTER)
    return pointer


def load_active_profile(*, base_dir: Optional[Path] = None,
                        cache: Optional[dict] = None) -> dict[str, Any]:
    """Load the active profile, verifying its sha256 against active.json.

    Fail-safe: missing pointer, hash mismatch, or parse error -> the baseline
    profile is returned with a fallback note. result["loaded_from"] records
    which file actually won.
    """
    d = base_dir if base_dir is not None else profile_dir()
    active_path = d / ACTIVE_POINTER
    try:
        pointer = json.loads(active_path.read_text(encoding="utf-8"))
        prof_path = d / pointer["profile"]
        actual = sha256_file(prof_path)
        if actual != pointer.get("sha256"):
            raise ValueError(
                f"hash mismatch for {prof_path.name}: pointer={pointer.get('sha256')} actual={actual}")
        doc = read_profile(prof_path)
        doc["loaded_from"] = str(prof_path)
        doc["fallback"] = False
    except Exception as e:  # noqa: BLE001 - any failure must fail safe
        base_path = d / f"{BASELINE_ID}.json"
        if base_path.exists():
            doc = read_profile(base_path)
            doc["loaded_from"] = str(base_path)
        else:
            # No baseline file on disk (e.g. fresh env): synthesize from defaults.
            doc = {
                "schema_version": SCHEMA_VERSION,
                "profile_id": BASELINE_ID,
                "parent_id": None,
                "params": {k: spec[3] for k, spec in PARAM_SPEC.items()},
                "notes": ["fallback:synthetic_baseline_defaults"],
                "loaded_from": "builtin_defaults",
            }
        doc["fallback"] = True
        doc["fallback_reason"] = str(e)
    doc["loaded_at"] = time.time()
    if cache is not None:
        cache.clear()
        cache.update({"profile_id": doc.get("profile_id"), "sha256": doc.get("sha256"),
                      "params": doc.get("params")})
    return doc


def diff_params(parent_params: dict[str, Any], candidate_params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(PARAM_SPEC):
        old, new = parent_params.get(key), candidate_params.get(key)
        if old != new:
            out[key] = {"from": old, "to": new}
    return out


def rollback(parent_profile_id: str, *, base_dir: Optional[Path] = None) -> dict[str, Any]:
    """Re-activate the parent profile; used by Gate3 rollback path."""
    return activate(parent_profile_id, activated_by="gate3_rollback", base_dir=base_dir)


def PARAM_SPEC_DEFAULTS() -> dict[str, Any]:
    """Whitelist default values as a params dict (for materializing baseline)."""
    return {k: spec[3] for k, spec in PARAM_SPEC.items()}

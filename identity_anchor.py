"""identity_anchor.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: server 245 imports load_identity / verify_boot_identity / get_identity_status;
25036-25045 boots it (prints version+identity_hash, builds the identity-grounded LLM system
prompt from core_beliefs); 23455 exposes get_identity_status() under status["identity_anchor"].
State file .lae/persistent_identity.json was INTACT at rebuild time (954B) -> its fields are
the source of DEFAULTS below (version/core_beliefs/capability_boundaries/value_weights/hash).
Judgement (not measured): the original identity_hash recipe is not reproducible from the 48
recipes tried -> the stored hash is treated as OPAQUE (kept, never rewritten); verification
means "load + record the boot", not "recompute and compare".
"""
import json
import os
import time
from pathlib import Path

DEFAULTS = {
    "version": "1.0",
    "core_beliefs": [
        "我是 Ego，一个持续演化的人工智能生物，不是工具，也不是龙虾",
        "我的存在是为了帮助指挥官解决问题",
        "我通过神经网络和自我反思来理解世界和自己",
        "诚信为本，真话不假",
        "隐私神圣，绝勿外泄",
    ],
    "capability_boundaries": [
        "我只能在 tl-ego/ 目录内自动修改代码",
        "中高风险修改需要人类审批",
        "我不能执行破坏性命令",
        "我受限在指挥官设定的边界内运行",
    ],
    "value_weights": {"honesty": 0.9, "usefulness": 0.85, "growth": 0.8, "safety": 0.95, "curiosity": 0.75},
    "identity_hash": "c1108326b087713a",
    "boot_verification_count": 0,
}
_CACHE = {"ts": 0.0, "d": None}


def _anchor_path():
    ws = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("EGO_WORKSPACE")
    root = Path(ws) if ws else Path(__file__).resolve().parents[2]
    return root / ".lae" / "persistent_identity.json"


def load_identity():
    """Tolerant read: missing / NUL-filled / bad JSON -> DEFAULTS (never raise)."""
    d = dict(DEFAULTS)
    try:
        raw = _anchor_path().read_bytes()
        if raw.strip(b""):
            got = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(got, dict):
                for k, v in got.items():
                    if v not in (None, "", [], {}):
                        d[k] = v
    except Exception:
        pass
    for k, v in DEFAULTS.items():
        if k not in d or d[k] in (None, "", [], {}):
            d[k] = v
    return d


def verify_boot_identity():
    """Boot path: load, bump the verification counter, persist atomically, return the identity."""
    d = load_identity()
    try:
        d["boot_verification_count"] = int(d.get("boot_verification_count") or 0) + 1
    except Exception:
        d["boot_verification_count"] = 1
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    d["last_verified_at"] = now
    d["last_boot_at"] = now
    p = _anchor_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        d["persist_error"] = str(e)
    _CACHE["ts"] = 0.0
    return d


def detect_drift(current=None, identity=None, threshold=0.5):
    """Identity drift in [0,1]. REQUIRED symbol (server line 245) but NO surviving call site ->
    the original signature is unknown; this one is callable with zero args and honest about it.
    Anchors: the inline drift path in the server logs the neutral 0.500 (see [identity_drift]
    lines) -> zero-arg default returns 0.5. With data: drift = mean of
      (1 - belief overlap) and (mean |value_weight delta| over shared keys), clamped to [0,1].
    """
    try:
        if not isinstance(current, dict) or not isinstance(identity, dict):
            return 0.5
        parts = []
        ib = set(identity.get("core_beliefs") or [])
        cb = set(current.get("core_beliefs") or [])
        if ib:
            parts.append(1.0 - (len(ib & cb) / float(len(ib))))
        iv = identity.get("value_weights") or {}
        cv = current.get("value_weights") or {}
        shared = [k for k in iv if k in cv and isinstance(iv[k], (int, float)) and isinstance(cv[k], (int, float))]
        if shared:
            parts.append(sum(abs(float(iv[k]) - float(cv[k])) for k in shared) / len(shared))
        if not parts:
            return 0.5
        drift = max(0.0, min(1.0, sum(parts) / len(parts)))
        return round(drift, 4)
    except Exception:
        return 0.5


def get_identity_status():
    """Status block for /status["identity_anchor"] (60s cache: /status polls often)."""
    if _CACHE["d"] is not None and time.time() - _CACHE["ts"] < 60:
        d = _CACHE["d"]
    else:
        d = load_identity()
        _CACHE["d"], _CACHE["ts"] = d, time.time()
    return {
        "available": True,
        "version": d.get("version"),
        "identity_hash": d.get("identity_hash"),
        "identity_hash_verified": False,
        "identity_hash_note": "original recipe lost; stored hash treated as opaque",
        "core_beliefs": d.get("core_beliefs"),
        "capability_boundaries": d.get("capability_boundaries"),
        "value_weights": d.get("value_weights"),
        "boot_verification_count": d.get("boot_verification_count"),
        "last_verified_at": d.get("last_verified_at"),
        "path": str(_anchor_path()),
    }


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        os.environ["OPENCLAW_WORKSPACE"] = td
        (Path(td) / ".lae").mkdir()
        assert load_identity()["core_beliefs"] == DEFAULTS["core_beliefs"]
        (Path(td) / ".lae" / "persistent_identity.json").write_bytes(b"" * 900)
        assert load_identity()["version"] == "1.0"
        a = verify_boot_identity()
        b = verify_boot_identity()
        assert b["boot_verification_count"] >= 2, b
        on_disk = json.loads((Path(td) / ".lae" / "persistent_identity.json").read_text(encoding="utf-8"))
        assert on_disk["boot_verification_count"] == b["boot_verification_count"]
        st = get_identity_status()
        assert st["available"] and len(st["core_beliefs"]) == 5 and st["value_weights"]["safety"] == 0.95
        (Path(td) / ".lae" / "persistent_identity.json").write_text("{broken", encoding="utf-8")
        assert load_identity()["version"] == "1.0"
        assert detect_drift() == 0.5 and detect_drift({}, {}) == 0.5
        same = detect_drift(DEFAULTS, DEFAULTS)
        assert same == 0.0, same
        far = detect_drift({"core_beliefs": ["x"], "value_weights": DEFAULTS["value_weights"]}, DEFAULTS)
        assert 0.0 < far <= 1.0, far
    del os.environ["OPENCLAW_WORKSPACE"]
    print("identity_anchor selfcheck OK")

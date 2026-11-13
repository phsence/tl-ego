"""value_check.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py imports get_value_vector (currently a dead import) and
get_value_status (served by GET /value); status["value_vector"] in the /status payload
reads RESTING_STATE["_value"]. The real ledger .lae/value_check_log.jsonl holds rows
{ts, patch_id, target, value_score, passed, recommendation, violations} - i.e. value_check
scores L7 patch proposals against the value system. The five value dimensions come from
.lae/persistent_identity.json value_weights {honesty, usefulness, growth, safety, curiosity}.
Judgement (not measured): the vector = those five weights as the current alignment target
and the status shape below; the original mapping is unrecoverable.
"""

import json
import time
from pathlib import Path

DIMS = ("honesty", "usefulness", "growth", "safety", "curiosity")
DEFAULT_WEIGHTS = {"honesty": 0.9, "usefulness": 0.85, "growth": 0.8, "safety": 0.95, "curiosity": 0.75}
PASS_THRESHOLD = 0.5


def _workspace():
    import os
    ws = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("EGO_WORKSPACE")
    return Path(ws) if ws else Path(__file__).resolve().parents[2]


def _lae(name):
    return _workspace() / ".lae" / name


def _load_weights():
    w = dict(DEFAULT_WEIGHTS)
    try:
        raw = _lae("persistent_identity.json").read_bytes()
        if raw.strip(b"\x00"):
            got = json.loads(raw.decode("utf-8", "replace")).get("value_weights") or {}
            for k in DIMS:
                if isinstance(got.get(k), (int, float)):
                    w[k] = round(float(got[k]), 4)
    except Exception:
        pass
    return w


def get_value_vector():
    """Current 5-dim value vector (the gate L7 self-modification is measured against)."""
    return _load_weights()


def get_value_status():
    """Status block for GET /value and status["value_vector"]."""
    checks = violations = 0
    last = None
    try:
        p = _lae("value_check_log.jsonl")
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                checks += 1
                if not row.get("passed", True):
                    violations += 1
                last = row
    except Exception:
        pass
    return {
        "available": True,
        "vector": get_value_vector(),
        "dims": list(DIMS),
        "pass_threshold": PASS_THRESHOLD,
        "checks": checks,
        "violations": violations,
        "last_check": last,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "path": str(_lae("value_check_log.jsonl")),
    }


if __name__ == "__main__":
    import tempfile, os
    os.environ["OPENCLAW_WORKSPACE"] = tempfile.mkdtemp()
    st = get_value_status()
    assert st["available"] is True and len(st["vector"]) == 5 and set(st["vector"]) == set(DIMS)
    assert st["vector"] == DEFAULT_WEIGHTS and st["checks"] == 0
    os.makedirs(os.path.join(os.environ["OPENCLAW_WORKSPACE"], ".lae"), exist_ok=True)
    with open(os.path.join(os.environ["OPENCLAW_WORKSPACE"], ".lae", "value_check_log.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"passed": True}) + "\n{bad json\n" + json.dumps({"passed": False}) + "\n")
    st2 = get_value_status()
    assert st2["checks"] == 2 and st2["violations"] == 1, st2
    with open(os.path.join(os.environ["OPENCLAW_WORKSPACE"], ".lae", "persistent_identity.json"), "wb") as fh:
        fh.write(b"\x00" * 50)
    assert get_value_vector() == DEFAULT_WEIGHTS
    print("value_check selfcheck OK")

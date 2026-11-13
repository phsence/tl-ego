"""l8_sandbox_verifier.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py imports verify_l8_contract_delivery, verify_l8_proposal,
write_l8_promotion_artifact. Call sites (~21712-21781, line numbers shifted after later
patches):
  verify_l8_proposal(candidate_path, candidate_name, {competition, stagnation_streak,
      concentration, dominant, tick}) -> candidate_verification; the server reads
      candidate_verification["ok"] to keep only verified candidates, and its own fallback
      dict is {"ok": False, "stage": "unavailable", "reason": "L8 sandbox verifier unavailable"}
  verify_l8_contract_delivery(sandbox_dir, mech_name, <same state dict>) -> _verification
  write_l8_promotion_artifact(sandbox_dir, manifest, _verification) -> _promotion (stored in
      manifest["promotion"])
Real artifact schema, .lae/l8_promotion_queue.jsonl (last row 09-25): {sandbox_id,
  sandbox_path, mech_id, mech_name, tick, verification_ok, verification_stage
  ("verified_delivery"), proposal_ok, proposal_applied, proposal_confidence,
  proposal_recommendation, hypothesis_kind, ...}
Sandbox base = .lae/l8_sandbox/<l8g-...> ("The selected result remains a review artifact,
never a live import" - so this gate must NOT execute the proposal).
Judgement (not measured): original check list unrecoverable; implemented as static
syntax + import/call scan (no execution - deliberately safer than running candidates) and
an explicit bundle completeness check.
"""

import ast
import hashlib
import json
import os
import time
from pathlib import Path

FORBIDDEN_IMPORTS = {"os", "sys", "subprocess", "socket", "shutil", "ctypes", "importlib", "pty", "winreg"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open"}


def _state_subset(state):
    st = state if isinstance(state, dict) else {}
    return {k: st.get(k) for k in ("competition", "stagnation_streak", "concentration", "dominant", "tick")}


def _scan(path):
    """Return (ok, stage, reason, info). Static only - nothing is executed."""
    p = Path(path)
    if not p.exists():
        return False, "missing_file", "proposal file not found", {}
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, "unreadable", str(e)[:120], {}
    info = {"bytes": len(src.encode("utf-8")),
            "sha256": hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]}
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, "rejected_syntax", "syntax error line %s: %s" % (e.lineno, e.msg), info
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    return False, "rejected_static", "forbidden import: %s" % a.name, info
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
                return False, "rejected_static", "forbidden import: %s" % node.module, info
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                return False, "rejected_static", "forbidden call: %s" % node.func.id, info
    return True, "verified_proposal", "static checks passed", info


def verify_l8_proposal(candidate_path, candidate_name=None, state=None):
    ok, stage, reason, info = _scan(candidate_path)
    return {"ok": ok, "stage": stage, "reason": reason, "candidate": candidate_name,
            "path": str(candidate_path), "checks": info, "state": _state_subset(state),
            "verified_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def verify_l8_contract_delivery(sandbox_dir, mech_name=None, state=None):
    """The bundle must be complete AND every python part must pass the static gate."""
    d = Path(sandbox_dir)
    required = ("module.py", "test_module.py", "integration.md")
    missing = [f for f in required if not (d / f).exists()]
    if missing:
        return {"ok": False, "stage": "rejected_incomplete", "reason": "missing: %s" % ", ".join(missing),
                "mech_name": mech_name, "sandbox_path": str(d), "state": _state_subset(state)}
    for f in ("module.py", "test_module.py"):
        ok, stage, reason, info = _scan(d / f)
        if not ok:
            return {"ok": False, "stage": stage, "reason": "%s: %s" % (f, reason), "mech_name": mech_name,
                    "sandbox_path": str(d), "state": _state_subset(state), "checks": info}
    readme = (d / "integration.md").read_text(encoding="utf-8", errors="replace")
    return {"ok": len(readme.strip()) > 0, "stage": "verified_delivery",
            "reason": "bundle complete; module.py + test_module.py passed static checks",
            "mech_name": mech_name, "sandbox_path": str(d), "files": list(required),
            "state": _state_subset(state), "verified_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def write_l8_promotion_artifact(sandbox_dir, manifest, verification):
    """Write promotion.json inside the sandbox and append a row to the promotion queue."""
    d = Path(sandbox_dir)
    m = manifest if isinstance(manifest, dict) else {}
    v = verification if isinstance(verification, dict) else {}
    sandbox_id = d.name
    promotion = {
        "schema": "l8_promotion.v1",
        "sandbox_id": sandbox_id,
        "sandbox_path": str(d),
        "mech_id": m.get("mech_id"),
        "mech_name": m.get("mech_name"),
        "tick": m.get("proposed_at_tick") or (v.get("state") or {}).get("tick"),
        "verification_ok": bool(v.get("ok")),
        "verification_stage": v.get("stage"),
        "proposal_ok": bool(v.get("ok")) and v.get("stage") == "verified_proposal",
        "proposal_applied": False,          # review artifact only - never a live import
        "hypothesis_kind": (m.get("selected_hypothesis") or {}).get("kind") if isinstance(m.get("selected_hypothesis"), dict) else None,
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "promotion.json").write_text(json.dumps(promotion, ensure_ascii=False, indent=2), encoding="utf-8")
        promotion["artifact"] = str(d / "promotion.json")
        q = Path(os.environ.get("OPENCLAW_WORKSPACE") or Path(__file__).resolve().parents[2]) / ".lae" / "l8_promotion_queue.jsonl"
        with open(q, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(promotion, ensure_ascii=False) + "\n")
        promotion["queued"] = True
    except Exception as e:
        promotion["queued"] = False
        promotion["error"] = str(e)[:160]
    return promotion


if __name__ == "__main__":
    import tempfile
    ws = Path(tempfile.mkdtemp())
    os.environ["OPENCLAW_WORKSPACE"] = str(ws)
    (ws / ".lae").mkdir()
    sb = ws / ".lae" / "l8_sandbox" / "l8g-test"
    sb.mkdir(parents=True)
    (sb / "module.py").write_text("def mechanism(state):\n    return {'ok': True}\n", encoding="utf-8")
    bad = ws / "bad.py"
    bad.write_text("import subprocess\nsubprocess.run(['cmd'])\n", encoding="utf-8")
    assert verify_l8_proposal(bad, "bad", {"tick": 5})["stage"] == "rejected_static"
    bad2 = ws / "bad2.py"
    bad2.write_text("def f(:\n", encoding="utf-8")
    assert verify_l8_proposal(bad2, "bad2")["stage"] == "rejected_syntax"
    assert verify_l8_proposal(ws / "nope.py", "n")["stage"] == "missing_file"
    assert verify_l8_proposal(sb / "module.py", "good")["ok"] is True
    v = verify_l8_contract_delivery(sb, "mech", {"tick": 9})
    assert v["stage"] == "rejected_incomplete", v
    (sb / "test_module.py").write_text("from module import mechanism\nassert mechanism({})\n", encoding="utf-8")
    (sb / "integration.md").write_text("# integration\n", encoding="utf-8")
    v2 = verify_l8_contract_delivery(sb, "mech", {"tick": 9})
    assert v2["ok"] and v2["stage"] == "verified_delivery", v2
    p = write_l8_promotion_artifact(sb, {"mech_id": "m1", "mech_name": "mech", "proposed_at_tick": 9}, v2)
    assert p["queued"] and p["verification_ok"] and p["proposal_applied"] is False
    row = json.loads((ws / ".lae" / "l8_promotion_queue.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["sandbox_id"] == "l8g-test" and row["verification_stage"] == "verified_delivery"
    print("l8_sandbox_verifier selfcheck OK")

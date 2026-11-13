"""Self-check for the residual_defect contract (2026-10-14).

Covers both halves of the loop: the reader/registry the core uses, and the
retirement rule (a defect its own result marked `resolved` stops counting as
"repeating"). Extracts the real functions from tl_ego_server.py — no server
import (importing it would start Flask).

  python tests/test_residual_defect_contract.py     # exit 0 = pass
"""
import json
import pathlib
import tempfile

SRC = pathlib.Path(__file__).resolve().parents[1] / "tl_ego_server.py"
text = SRC.read_text(encoding="utf-8")

def block(start, end):
    i, j = text.index(start), text.index(end)
    assert i < j, f"bad slice {start!r}->{end!r}"
    return text[i:j]

ns = {"json": json, "print": print}
exec(compile(block("def _residual_defect_of(", "def _count_repeated_diagnoses("), "<reader>", "exec"), ns)
exec(compile(block("def _count_repeated_diagnoses(", "def _resolved_defects("), "<detector>", "exec"), ns)
exec(compile(block("def _resolved_defects(", "def _recent_lessons_for_goal("), "<registry>", "exec"), ns)

# --- unit: the field parser accepts every shape -------------------------------
parse = ns["_residual_defect_of"]
assert parse({}) == ("", False), "absent field must read as no-defect"
assert parse({"residual_defect": "pool_gap"}) == ("pool_gap", False), "legacy string = open"
assert parse({"residual_defect": {"primary_defect": "pool_gap", "status": "open"}}) == ("pool_gap", False)
assert parse({"residual_defect": {"primary_defect": "pool_gap", "status": "resolved"}}) == ("pool_gap", True)
assert parse({"snapshot_state": {"residual_defect": {"primary_defect": "x", "status": "fixed"}}}) == ("x", True)

# --- integration: temp sandbox, open vs resolved ------------------------------
def make_sandbox(fixtures):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ego_test_"))
    (tmp / "ego_sandbox").mkdir()
    for i, data in enumerate(fixtures):
        d = tmp / "ego_sandbox" / f"l5g-test-{i:06d}"
        d.mkdir()
        (d / "result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp

def detect(tmp):
    ns["WORKSPACE"] = tmp
    return ns["_count_repeated_diagnoses"]()

open_defect = {"residual_defect": {"primary_defect": "pool_gap", "status": "open", "evidence": "gap still present"}}
fixed_defect = {"residual_defect": {"primary_defect": "pool_gap", "status": "resolved", "evidence": "0/12 gaps after fix"}}

tmp = make_sandbox([open_defect] * 3)
assert detect(tmp) == ("pool_gap", 3), f"open defect x3 must fire, got {detect(tmp)}"

tmp = make_sandbox([fixed_defect] * 3)
assert detect(tmp) == (None, 0), f"resolved defect must NOT count as repeating, got {detect(tmp)}"

# registry + note: resolved surfaces for the next goal, open does not
tmp = make_sandbox([fixed_defect, open_defect])
ns["WORKSPACE"] = tmp
assert ns["_resolved_defects"]() == {"pool_gap": "0/12 gaps after fix"}, ns["_resolved_defects"]()
note = ns["_resolved_defects_note"]()
assert "pool_gap" in note and "0/12 gaps after fix" in note, note

tmp = make_sandbox([open_defect])
ns["WORKSPACE"] = tmp
assert ns["_resolved_defects_note"]() == "", "nothing resolved -> no note"

# legacy lesson-keyword fallback still works when the field is absent
tmp = make_sandbox([{"lesson": "candidate_pool_too_narrow again"}] * 3)
assert detect(tmp) == ("candidate_pool_too_narrow", 3), detect(tmp)

print("residual_defect contract: OK")

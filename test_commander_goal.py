"""Commander goal ingestion tests — pure module, no tl_ego_server import.

TDD for commander_goal.py: build_commander_goal / write_trigger / write_outbox.
Goal-id constraint: must match ^l5g-\\d{8}-\\d{6} prefix (server _record_goal_terminal).
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import commander_goal as cg  # noqa: E402

NOW = "2026-08-29T12:00:00"


def test_build_directive_shape():
    g = cg.build_commander_goal(
        task="验证 commander 链路",
        success_criteria="账本出现 source=user_directive 行",
        kind="directive", priority="high",
        now=NOW, tick=1000,
    )
    assert g["source"] == "user_directive"
    assert g["goal_type"] == "commander_directive"
    assert g["status"] == "sandbox_autonomous_running"
    assert g["live_mutation_authority"] == "denied"
    assert g["goal_id"].startswith("l5g-20260829-"), g["goal_id"]
    assert "execution_policy" in g
    assert g["priority"] >= 0.95


def test_build_feedback_shape():
    g = cg.build_commander_goal(task="MC 建房子总是歪", kind="feedback",
                                now=NOW, tick=1001)
    assert g["goal_type"] == "commander_feedback"
    assert g["feedback_route"] == "sandbox_three_gate"
    assert g["status"] == "sandbox_autonomous_running"
    assert g["source"] == "user_directive"


def test_goal_id_uniqueness():
    a = cg.build_commander_goal("same second task A", now=NOW, tick=1)
    b = cg.build_commander_goal("same second task B", now=NOW, tick=2)
    assert a["goal_id"] != b["goal_id"]


def test_parse_directive_body():
    r = cg.parse_directive_body({"task": "  做X  ", "priority": "high"})
    assert r["task"] == "做X" and r["kind"] == "directive" and r["priority"] == "high"
    r2 = cg.parse_directive_body({"task": "改Y", "kind": "feedback"})
    assert r2["kind"] == "feedback"
    r3 = cg.parse_directive_body({"task": "默认档"})
    assert r3["priority"] == "high" and r3["horizon"] == "medium"
    try:
        cg.parse_directive_body({})
        assert False, "empty task must raise"
    except ValueError:
        pass


def test_trigger_and_outbox_writes():
    td = pathlib.Path(tempfile.mkdtemp())
    g = cg.build_commander_goal("t", kind="directive", now=NOW, tick=2000)
    cg.write_trigger(g, trigger_dir=td)
    trig = td / ".l5_comm_trigger.json"
    assert trig.exists(), "comm trigger file must be written"
    t = json.loads(trig.read_text(encoding="utf-8"))
    for k in ("tick", "goal_type", "goal", "domain", "goal_id",
              "success_criteria", "risk"):
        assert k in t, f"trigger missing field: {k}"
    assert t["goal_type"] == "commander_directive"

    ob = td / "outbox"
    cg.write_outbox(g, "accepted", outbox_dir=ob)
    cg.write_outbox(g, "terminal", outcome="succeeded", report="done", outbox_dir=ob)
    rows = [json.loads(l) for l in
            (ob / "user_reports.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["accepted", "terminal"]
    assert rows[1]["goal_id"] == g["goal_id"]
    assert rows[1]["outcome"] == "succeeded"


def test_terminal_event_only_for_commander_not_here():
    # outbox writes are explicit; no implicit terminal from non-commander paths.
    # (This module only writes when called — the server-side filter lives in
    #  _record_goal_terminal, tested in Task 5.)
    pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        if fn.__name__ == "test_terminal_event_only_for_commander_not_here":
            continue
        fn()
        print(f"PASS {fn.__name__}")
    print("ALL PASS")

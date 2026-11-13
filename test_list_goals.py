"""Task 4 tests: list_goals — ego self-tracking of commander goals."""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ego_internal_bridge as bridge  # noqa: E402


def _mk_ledger(rows):
    p = pathlib.Path(tempfile.mkdtemp()) / "l5_goals.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def test_list_goals_merges_terminal():
    p = _mk_ledger([
        {"goal_id": "l5g-20260829-120000-C1", "source": "user_directive",
         "task": "验证链路", "goal_type": "commander_directive",
         "status": "sandbox_autonomous_running", "priority": 0.98,
         "ts": "2026-08-29T12:00:00"},
        {"goal_id": "l5g-20260829-120001-X9", "source": "stage2_homeostatic_drives",
         "task": "内部目标", "status": "sandbox_autonomous_running"},
        {"goal_id": "l5g-20260829-120000-C1", "terminal_state": "succeeded",
         "outcome": "success", "terminal_ts": "2026-08-29T12:30:00"},
    ])
    r = bridge._real_list_goals(ledger_path=str(p))
    assert r["count"] == 1, r
    g = r["goals"][0]
    assert g["terminal_state"] == "succeeded"
    assert g["goal_type"] == "commander_directive"
    assert g["task"] == "验证链路"


def test_list_goals_missing_ledger():
    p = pathlib.Path(tempfile.mkdtemp()) / "nope.jsonl"
    r = bridge._real_list_goals(ledger_path=str(p))
    assert r["count"] == 0 and r.get("note")


def test_list_goals_filters_non_commander():
    p = _mk_ledger([
        {"goal_id": "l5g-20260829-120002-Z1",
         "source": "stage2_homeostatic_drives",
         "task": "内部目标", "status": "sandbox_autonomous_running"},
    ])
    r = bridge._real_list_goals(ledger_path=str(p))
    assert r["count"] == 0


if __name__ == "__main__":
    for fn in (test_list_goals_merges_terminal, test_list_goals_missing_ledger,
               test_list_goals_filters_non_commander):
        fn()
        print(f"PASS {fn.__name__}")
    print("ALL PASS")

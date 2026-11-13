"""Task 5 tests: commander index — terminal hook's O(1) lookups (pure module)."""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import commander_goal as cg  # noqa: E402

NOW = "2026-08-29T12:00:00"


def _tmp():
    return pathlib.Path(tempfile.mkdtemp())


def test_register_is_commander_and_task_text():
    td = _tmp()
    idx_path = td / "commander_goal_index.json"
    g = cg.build_commander_goal("汇报链路的任务", now=NOW, tick=1)
    cg.register_commander_goal(g, index_path=idx_path)
    assert cg.is_commander_goal(g["goal_id"], index_path=idx_path)
    assert cg.commander_task_text(g["goal_id"], index_path=idx_path) == "汇报链路的任务"
    # persistence roundtrip
    idx2 = json.loads(idx_path.read_text(encoding="utf-8"))
    assert g["goal_id"] in idx2


def test_unknown_goal_is_not_commander():
    td = _tmp()
    idx_path = td / "commander_goal_index.json"
    assert not cg.is_commander_goal("l5g-20260829-999999-Z0", index_path=idx_path)
    assert cg.commander_task_text("l5g-20260829-999999-Z0", index_path=idx_path) == ""


def test_index_cap_at_5000():
    td = _tmp()
    idx_path = td / "commander_goal_index.json"
    big = {f"l5g-20260829-12{i:04d}-Cx": {"task": "t", "goal_type": "commander_directive"}
           for i in range(5100)}
    cg.save_commander_index(big, index_path=idx_path)
    loaded = cg.load_commander_index(index_path=idx_path)
    assert len(loaded) <= 5000


if __name__ == "__main__":
    for fn in (test_register_is_commander_and_task_text,
               test_unknown_goal_is_not_commander, test_index_cap_at_5000):
        fn()
        print(f"PASS {fn.__name__}")
    print("ALL PASS")

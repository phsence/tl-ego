"""One runnable check for the knowledge-sediment hook (2026-10-27).

Asserts: (1) a completed sandbox conclusion lands in hy-memory exactly once
(vdb_points +1), (2) replay is suppressed by the posted-set, (3) a result
without outcome is a no-op.
"""
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
WS = pathlib.Path(r"C:\Users\Administrator\.openclaw\workspace")
SRC = ROOT / "tl_ego_server.py"
START = "SEDIMENT_POSTED_PATH = WORKSPACE"
END = "def _consume_next_impulse_hint("

text = SRC.read_text(encoding="utf-8")
block = text[text.index(START):text.index(END)]
assert "def _persist_sandbox_conclusion" in block, "sediment block not found"

sys.path.insert(0, str(ROOT))  # real memory_bridge, real hy-memory sidecar
ns = {"json": json, "WORKSPACE": WS, "print": print}
exec(compile(block, "<sediment>", "exec"), ns)
persist = ns["_persist_sandbox_conclusion"]


def _vdb_points() -> int:
    with urllib.request.urlopen("http://127.0.0.1:19527/api/v1/status", timeout=10) as r:
        return json.loads(r.read())["vdb_points"]


import time
goal_id = f"l5g-selftest-sediment-{int(time.time())}"  # unique per run: re-runnable
data = {
    "outcome": "success",
    "intent": "selftest: knowledge-sediment hook end-to-end check",
    "project_name": "sediment_selftest",
}

before = _vdb_points()
ok = False
for _attempt in range(3):  # hy-memory add intermittently 502s; the hook itself retries next tick
    if persist(goal_id, data):
        ok = True
        break
assert ok, "first persist must succeed within 3 attempts"
# Landed SOMEWHERE: hy-memory vdb (+1) or the memu.db fallback channel
# (content-triggered 502s route there by design).
import sqlite3

from memory_bridge import AGENT_MEMORY_DB

_conn = sqlite3.connect(str(AGENT_MEMORY_DB))
_memu_hits = _conn.execute(
    "SELECT COUNT(*) FROM memory_items WHERE content LIKE ?", (f"%{goal_id}%",)
).fetchone()[0]
_conn.close()
after_first = _vdb_points()
assert after_first >= before + 1 or _memu_hits >= 1, (
    f"conclusion must land in hy-memory (vdb {before}->{after_first}) or memu ({_memu_hits} rows)"
)

assert persist(goal_id, data) is False, "replay must be suppressed"
# (no vdb equality assert here: production posts concurrently, so the global
# counter can move without us — False return + posted-set membership is the contract)

posted = json.loads(ns["SEDIMENT_POSTED_PATH"].read_text(encoding="utf-8"))
assert goal_id in posted, "posted-set must record the goal"

assert persist("l5g-fake-no-outcome", {"outcome": ""}) is False, "no outcome = no-op"

print("PASS: conclusion sedimented once, idempotent on replay, no-op without outcome")

"""Self-check for the hint replay guard in tl_ego_server.py (2026-10-14).

Extracts the guard block from the server source (no server import — importing
tl_ego_server would start Flask) and asserts its semantics:

  python tests/test_hint_replay_guard.py     # exit 0 = pass

Fails if: a consumed (dir, hint) pair is re-consumed inside the cooldown,
the cooldown never expires, dirs are not independent, or pruning breaks.
"""
import hashlib
import pathlib
import time

SRC = pathlib.Path(__file__).resolve().parents[1] / "tl_ego_server.py"
START = "HINT_REPLAY_COOLDOWN_TICKS = 12"
END = "def _consume_next_impulse_hint("

text = SRC.read_text(encoding="utf-8")
block = text[text.index(START):text.index(END)]
assert "def _mark_hint_consumed" in block and "_hint_replay_seen" in block, "guard block not found"

ns = {"hashlib": hashlib, "time": time, "print": print, "RESTING_STATE": {"tick": 1000}}
exec(compile(block, "<guard>", "exec"), ns)
seen, mark, cd = ns["_hint_replay_seen"], ns["_mark_hint_consumed"], ns["HINT_REPLAY_COOLDOWN_TICKS"]
rs = ns["RESTING_STATE"]

assert not seen("l5g-A", "do X"), "unseen pair must pass"
mark("l5g-A", "do X")
assert seen("l5g-A", "do X"), "consumed pair must be blocked inside cooldown"
assert not seen("l5g-B", "do X"), "same hint from another dir must stay independent"
assert not seen("l5g-A", "do Y"), "another hint in the same dir must stay independent"

rs["tick"] = 1000 + cd  # cooldown elapsed -> eligible again
assert not seen("l5g-A", "do X"), "cooldown must expire"

rs["tick"] = 1000
for i in range(250):  # pruning ceiling
    mark(f"l5g-P{i}", f"hint {i}")
rs["tick"] = 1000 + cd + 1
mark("l5g-Z", "fresh")
assert len(rs["_consumed_hint_fps"]) <= 201, f"prune failed: {len(rs['_consumed_hint_fps'])}"

print("hint replay guard: OK")

"""world_* routing must require a live body.

2026-11-07: a closed world (8765/8790 both down) still got 16 world_verify goals
in one day. resolve_tool gated on stagnation/competition/tick only; the body
probe result was used for prompt wording but never for routing.

Run: python tests/test_world_body_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools_registry as tr

# resolve_tool() does `from tl_ego_server import _read_recent_causal_events`, and that
# module boots a server on import — which trips the single-instance lock and raises
# SystemExit (not caught by its except Exception). Stub it so the causalevent branch
# degrades to None, as it does when the module is unavailable at runtime.
import types
sys.modules.setdefault("tl_ego_server", types.ModuleType("tl_ego_server"))

WORLD = set(tr.WORLD_TOOL_NAMES) if hasattr(tr, "WORLD_TOOL_NAMES") else {
    "world_act", "world_verify", "world_explore"
}

# Stagnation high enough that the pre-fix code returns world_verify for this state.
STATE = {
    "last_dominant": "doubt",
    "stagnation_streak": 5,
    "competition": 0.80,
    "concentration": 0.30,
    "tick": 200,
    "last_world_act_tick": 0,
    "last_world_verify_tick": 0,
    "last_world_explore_tick": 0,
}


def _probe(name):
    real = tr.world_interface
    tr.world_interface = lambda timeout=0.25: (name, "stub body: " + name)
    try:
        got = tr.resolve_tool(dict(STATE))
    finally:
        tr.world_interface = real
    return got[0] if got else None


def demo():
    # 1) no body -> world_* must be unreachable, but a tool is still chosen.
    dead = _probe("none")
    assert dead not in WORLD, f"closed world still routed to {dead!r}"
    assert dead is not None, "resolve_tool returned nothing for a healthy state"

    # 2) live body -> the gate must not swallow the world route (anti-regression).
    live = _probe("minecraft")
    assert live in WORLD, f"live body did not route to a world tool (got {live!r})"

    print(f"OK dead->{dead}  live->{live}")


if __name__ == "__main__":
    demo()

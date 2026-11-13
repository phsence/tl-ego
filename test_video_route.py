#!/usr/bin/env python3
"""Check for ego's creative video route: TOOLS entry + resolve_tool selection + cooldown.

Run: python test_video_route.py   (exit 0 = pass)
"""
import os
import sys
import tempfile
import time
import types

# tools_registry imports the live server module for causal-event reads at import time;
# stub it so this check can run while ego is serving on :8766 (single-instance guard).
_stub = types.ModuleType("tl_ego_server")
_stub._read_recent_causal_events = lambda n=1: []
sys.modules.setdefault("tl_ego_server", _stub)

import tools_registry as tr


def healthy_state(**over):
    s = {"last_dominant": "action", "stagnation_streak": 0, "competition": 0.70,
         "concentration": 0.30, "stability": 0.7, "tick": 5000,
         "noise_schedule": "explore", "world_actions_enabled": False}
    s.update(over)
    return s


def main():
    cfg = tr.TOOLS.get("create_video")
    assert cfg, "create_video missing from TOOLS"
    assert cfg["goal_type"] == "create_video"
    assert "make_video" in cfg["prompt_template"], "prompt must name the make_video tool"

    stamp = os.path.join(tempfile.mkdtemp(prefix="vidroute_"), "stamp")
    tr._VIDEO_STAMP_PATH = stamp
    tr._VIDEO_ROUTE_ENABLED = True

    # no stamp yet -> ready -> routes
    name, _ = tr.resolve_tool(healthy_state())
    assert name == "create_video", f"healthy state should route to create_video, got {name}"

    # selecting twice must not consume the slot: resolve_tool stays pure
    name2, _ = tr.resolve_tool(healthy_state())
    assert name2 == "create_video", "resolve_tool must not stamp the cooldown"

    # only a real dispatch starts the cooldown
    tr.mark_video_dispatched()
    name3, _ = tr.resolve_tool(healthy_state())
    assert name3 != "create_video", "cooldown failed: routed again after a dispatch"

    # cooldown expires
    old = time.time() - 3660
    os.utime(stamp, (old, old))
    name4, _ = tr.resolve_tool(healthy_state())
    assert name4 == "create_video", "route must reopen once the cooldown elapsed"

    tr._VIDEO_ROUTE_ENABLED = False
    name5, _ = tr.resolve_tool(healthy_state())
    assert name5 != "create_video", "EGO_VIDEO_ROUTE=0 must disable the route"
    tr._VIDEO_ROUTE_ENABLED = True

    os.remove(stamp)
    name6, _ = tr.resolve_tool(healthy_state(stagnation_streak=5, competition=0.40))
    assert name6 != "create_video", "unhealthy state must not take the render slot"

    print("ok: registered, routes when healthy, pure selection, file cooldown, flag + guard hold")


if __name__ == "__main__":
    main()

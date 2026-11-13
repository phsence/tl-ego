"""One check for _apply_goal_focus: extract it from tl_ego_server.py and run it
against stubs, so the reroute logic is verified without booting the server.

python test_goal_focus.py   -> exit 0 = ok
"""
import ast
import os
import sys
from pathlib import Path

SRC = Path(__file__).with_name("tl_ego_server.py")
src = SRC.read_text(encoding="utf-8", errors="replace")

fn = next(n for n in ast.parse(src).body
          if isinstance(n, ast.FunctionDef) and n.name == "_apply_goal_focus")
code = ast.get_source_segment(src, fn)

TOOLS = {
    "create_video": {"goal_type": "create_video"},
    "neuron_rule_modification_repair": {"goal_type": "rule_modification_repair"},
    "causal_lens": {"goal_type": "causal_lens"},
}
RESTING_STATE = {"last_goal_timestamps": {"causal_lens": 500, "rule_modification_repair": 100}}
os.environ["EGO_GOAL_DEMOTE"] = "create_video,memory_recall"
os.environ["EGO_GOAL_FOCUS"] = "rule_modification_repair,causal_lens"

ns = {"os": os, "TOOLS": TOOLS, "RESTING_STATE": RESTING_STATE}
exec(code, ns)
f = ns["_apply_goal_focus"]

# demoted -> rerouted to the stalest focus type (rule_modification_repair, ts=100)
gt, tn, tc = f("create_video", "create_video", TOOLS["create_video"])
assert gt == "rule_modification_repair", gt
assert tn == "neuron_rule_modification_repair", tn
assert tc is TOOLS[tn]

# not demoted -> untouched
gt, tn, tc = f("causal_lens", "causal_lens", TOOLS["causal_lens"])
assert (gt, tn) == ("causal_lens", "causal_lens"), (gt, tn)

# commander request -> never rerouted
gt, tn, tc = f("create_video", "create_video", TOOLS["create_video"], custom_task=True)
assert gt == "create_video", gt

# empty focus list -> feature off, no crash
os.environ["EGO_GOAL_FOCUS"] = ""
gt, tn, tc = f("create_video", "create_video", TOOLS["create_video"])
assert gt == "create_video", gt

print("OK: reroute / pass-through / custom_task / disabled all correct")

# ── selector guards (measured leak: the impulse path wrote create_video and
# memory_recall straight from tools_registry, bypassing the writer reroute) ──
import tools_registry as tr

os.environ["EGO_GOAL_DEMOTE"] = "create_video,memory_recall"
assert tr._goal_demoted("create_video") is True
assert tr._goal_demoted("causal_lens") is False
os.environ["EGO_GOAL_DEMOTE"] = ""
assert tr._goal_demoted("create_video") is False

os.environ["EGO_GOAL_DEMOTE"] = "create_video,memory_recall"
_eligible = next(iter(tr._COGNITIVE_DIVERSITY_ELIGIBLE))
sel = tr.select_cognitive_diversity_tool({"last_goal_timestamps": {}}, _eligible)
assert sel is not None, "rotation emptied out"
_sel_type = str((sel[1] or {}).get("goal_type") or sel[0])
assert not tr._goal_demoted(_sel_type), f"rotation picked demoted {_sel_type}"
print(f"OK: selectors never return a demoted type (rotation picked {_sel_type})")

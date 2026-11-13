"""Prove the LIVE server path emits real families now, without waiting out the
50-tick cooldown.

Runs the exact production entry point the server calls each resting tick
(`l10_generator_bridge.l10_gen_trigger`) against the real observer profile and
the real logs. The only thing skipped is the cooldown wall-clock wait: the tick
passed is the tick the cooldown expires at (last_trigger + 50), so the inputs
are identical to the fire the server would log 1.5h from now.
"""
import importlib.util
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(r"C:/Users/Administrator/.openclaw/workspace/projects/tl-ego")
LAE = ROOT.parent.parent / ".lae"


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


bridge = load("l10_generator_bridge", "l10/l10_generator_bridge.py")

# 1. Does the per-tick loader resolve the edited .py (not the stale .pyc)?
gen_mod = bridge._load_py_module("l10_meta_algorithm_generator", "l10_meta_algorithm_generator.py")
print("loader resolved:", Path(gen_mod.__file__).name,
      "| source available:", bool(inspect.getsource(gen_mod.qd_saturation)) ,
      "| derived prefix:", gen_mod.current_suite_prefix(),
      "| saturated:", gen_mod.qd_saturation()["saturated"])

# 2. Fire the production trigger at the cooldown-expiry tick.
last_tick = bridge._read_last_trigger_tick()
fire_tick = int(last_tick or 0) + bridge._COOLDOWN_TICKS
state = {"tick": fire_tick}
print("last_trigger_tick:", last_tick, "-> firing at tick", fire_tick)
fired = bridge.l10_gen_trigger(state, {}, "probe-no-wait")
res = state.get(bridge._RESULT_KEY) or {}
print("fired:", fired, "| reason:", res.get("trigger_reason"),
      "| candidates:", [ (c.get("strategy") or {}).get("type") for c in res.get("algorithm_candidates") or [] ])
if res.get("error"):
    print("error:", res["error"])

# 3. Disk truth: the loop's own audit log must carry this tick with a real family.
row = json.loads(LAE.joinpath("l10_gen_audit.jsonl").read_text(encoding="utf-8", errors="replace")
                 .rstrip().rsplit("\n", 1)[-1])
print("audit tail:", {k: row.get(k) for k in ("tick", "candidate_count", "meta_rationale")})
assert row.get("tick") == fire_tick, row
assert "algorithm_selection" not in str(row.get("meta_rationale")), row
print("LIVE_OK: production path emits real families")

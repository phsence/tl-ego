"""Probe: what does the live generator actually emit for the current profile?

Reads the real observer/l10 profile through the production bridge, builds the
same BottleneckProfile, and prints every dissensus pair and candidate. No
writes: generate() is called directly, write_result() is never invoked.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(r"C:/Users/Administrator/.openclaw/workspace/projects/tl-ego")


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


bridge = load("l10_generator_bridge", "l10/l10_generator_bridge.py")
gen_mod = load("l10_meta_algorithm_generator", "l10/l10_meta_algorithm_generator.py")

live = bridge._read_latest_l10_profile()
profile = bridge._build_profile(live, None, False, "", bridge._read_latest_injection())
g = gen_mod.MetaAlgorithmGenerator()
tick = g._current_tick()
pairs = g.build_dissensus_pairs(profile)
pairs = gen_mod.apply_qd_pressure(pairs, gen_mod.qd_saturation())
print("tick", tick, "| dead_type_count", profile.dead_type_count,
      "| curiosity dead:", "curiosity" in set(profile.dead_types))
print("qd_saturation:", gen_mod.qd_saturation())
print("min_dissensus_score:", g.config.min_dissensus_score)
for p in pairs:
    ok = p.score >= g.config.min_dissensus_score
    cands = gen_mod.MetaAlgorithmGenerator._draft_algorithm_candidates(g, p, profile, {})
    print(f"  pair {p.s1_label:24s} score={p.score:.2f} pass={ok} candidates={[c['strategy']['type'] for c in cands]}")

res = g.generate(profile, {}, tick)
print("generate.triggered:", res.get("triggered"), "reason:", res.get("trigger_reason") or res.get("reason"))
for c in res.get("algorithm_candidates") or []:
    st = c.get("strategy") or {}
    print("  emitted:", st.get("type"), "| shadow:", st.get("shadow_only"), "| len:", len(c.get("proposed_code") or c.get("dsl") or ""))

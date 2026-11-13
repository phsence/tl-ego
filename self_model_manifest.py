"""self_model_manifest.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py (line ~406) imports build_manifest / render_manifest_for_prompt /
fallback_response. build_manifest is called with keyword sections runtime=, identity=,
persistent_identity=, l7_status=, active_goal=, runtime_id=, counterfactual_competition=;
its result is stored as log["self_model_manifest"] and rendered into the LLM prompt
(`resp_prompt += render_manifest_for_prompt(manifest)`) with fallback_response(manifest)
used when the model returns nothing. The source comment there is the spec:
"Versioned, provenance-aware facts injected into Ego's natural-language self-model."
Judgement (not measured): exact original fact keys / text are unrecoverable; the shape
below keeps every input fact, tags each with its source, and renders a compact block.
"""

import json
import time

MANIFEST_VERSION = "self_model_manifest.v1"
_SKIP = (None, {}, [], "")


def _flatten(d, prefix=""):
    out = []
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        key = prefix + str(k)
        if isinstance(v, dict):
            out.extend(_flatten(v, key + "."))
        elif v not in _SKIP:
            out.append((key, v))
    return out


def build_manifest(runtime=None, identity=None, persistent_identity=None, l7_status=None,
                   active_goal=None, runtime_id=None, counterfactual_competition=None):
    rt = runtime if isinstance(runtime, dict) else {}
    ident = identity if isinstance(identity, dict) else {}
    pid = persistent_identity if isinstance(persistent_identity, dict) else {}
    beliefs = ident.get("core_beliefs") or pid.get("beliefs") or []
    values = ident.get("value_weights") or pid.get("value_weights") or {}
    facts = [{"key": k, "value": v, "source": "runtime", "tick": rt.get("tick")}
             for k, v in _flatten(rt)]
    for k, v in _flatten(ident, "identity."):
        facts.append({"key": k, "value": v, "source": ".lae/persistent_identity.json", "tick": rt.get("tick")})
    if active_goal:
        for k, v in _flatten(active_goal if isinstance(active_goal, dict) else {"goal": active_goal}, "goal."):
            facts.append({"key": k, "value": v, "source": "l5_goal", "tick": rt.get("tick")})
    return {
        "schema": MANIFEST_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runtime_id": runtime_id,
        "tick": rt.get("tick"),
        "facts": facts,
        "sections": {
            "runtime": rt,
            "identity": {"version": ident.get("version") or pid.get("version"),
                         "identity_hash": ident.get("identity_hash") or pid.get("identity_hash"),
                         "core_beliefs": beliefs,
                         "value_weights": values},
            "l7_status": l7_status if isinstance(l7_status, (dict, list)) else None,
            "active_goal": active_goal,
            "counterfactual_competition": counterfactual_competition,
        },
        "provenance": {"runtime": "server RESTING_STATE (live)",
                       "identity": ".lae/persistent_identity.json",
                       "goal": ".lae/l5_goals.jsonl",
                       "generated_by": "self_model_manifest.build_manifest"},
    }


def render_manifest_for_prompt(manifest):
    """Compact prompt block: versioned facts, each line carrying its source."""
    if not isinstance(manifest, dict):
        return ""
    lines = ["[self-model manifest %s | tick=%s | runtime=%s]" % (
        manifest.get("schema", MANIFEST_VERSION), manifest.get("tick"), manifest.get("runtime_id"))]
    for f in (manifest.get("facts") or [])[:28]:
        lines.append("- %s: %s (source: %s)" % (f.get("key"), str(f.get("value"))[:120], f.get("source")))
    sec = manifest.get("sections") or {}
    ident = sec.get("identity") or {}
    if ident.get("core_beliefs"):
        lines.append("- identity.beliefs: " + " | ".join(str(b)[:80] for b in ident["core_beliefs"][:5]))
    if ident.get("value_weights"):
        lines.append("- identity.values: " + json.dumps(ident["value_weights"], ensure_ascii=False))
    if isinstance(sec.get("active_goal"), (dict, str)) and sec["active_goal"]:
        lines.append("- active_goal: " + str(sec["active_goal"])[:160])
    return "\n".join(lines)


def fallback_response(manifest):
    """Used when the LLM reply is empty: answer from the manifest, never invent."""
    if not isinstance(manifest, dict):
        return "我现在拿不到自己的自我模型数据，无法给出可信的自我描述。"
    rt = (manifest.get("sections") or {}).get("runtime") or {}
    ident = (manifest.get("sections") or {}).get("identity") or {}
    bits = ["我是 Ego（龙虾），TL-Ego 神经符号系统的核心。"]
    if manifest.get("tick") is not None:
        bits.append("当前 tick %s。" % manifest["tick"])
    if rt.get("last_dominant"):
        bits.append("主导类型 %s（浓度 %s，竞争度 %s）。" % (
            rt.get("last_dominant"), rt.get("last_concentration"), rt.get("last_competition")))
    if rt.get("stagnation_streak") is not None:
        bits.append("停滞计数 %s。" % rt.get("stagnation_streak"))
    if ident.get("core_beliefs"):
        bits.append("核心信念：" + "；".join(str(b) for b in ident["core_beliefs"][:3]) + "。")
    if isinstance((manifest.get("sections") or {}).get("active_goal"), (dict, str)):
        bits.append("当前自主目标：" + str((manifest["sections"]).get("active_goal"))[:120] + "。")
    bits.append("（本条为自我模型清单的直读回答，非模型生成）")
    return "".join(bits)


if __name__ == "__main__":
    m = build_manifest(
        runtime={"tick": 28370, "last_dominant": "integrator", "last_concentration": 0.12,
                 "last_competition": 0.72, "stagnation_streak": 2, "empty": {}, "none": None},
        identity={"version": "1.0", "identity_hash": "c1108326b087713a",
                  "core_beliefs": ["诚信为本"], "value_weights": {"safety": 0.95}},
        active_goal={"goal_id": "l5g-1", "text": "observe workspace"},
        runtime_id="ego-test", counterfactual_competition={"pairs": 3})
    assert m["schema"] == MANIFEST_VERSION and m["tick"] == 28370
    keys = [f["key"] for f in m["facts"]]
    assert "last_dominant" in keys and "identity.identity_hash" in keys and "goal.goal_id" in keys
    assert not any(k.endswith("empty") or k.endswith("none") for k in keys)
    txt = render_manifest_for_prompt(m)
    assert "self-model manifest" in txt and "integrator" in txt and "诚信为本" in txt
    assert "goal.goal_id" in txt or "l5g-1" in txt
    fb = fallback_response(m)
    assert "Ego" in fb and "integrator" in fb and "非模型生成" in fb
    assert render_manifest_for_prompt(None) == "" and fallback_response(3).startswith("我现在拿不到")
    bare = build_manifest()
    assert bare["facts"] == [] and render_manifest_for_prompt(bare).startswith("[self-model manifest")
    print("self_model_manifest selfcheck OK | facts=%d | prompt_chars=%d" % (len(m["facts"]), len(txt)))

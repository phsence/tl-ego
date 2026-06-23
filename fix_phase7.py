#!/usr/bin/env python3
"""Fix Phase 7 narrative block with correct syntax."""
fp = r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\tl_ego_server.py"
text = open(fp, encoding="utf-8").read()

# Find the marker comments that still exist
marker1 = "# ── Phase 7: LLM Self-Narrative ──"
marker2 = "# Every 5 queries, generate a first-person self-description via LLM"
marker3 = "# NARRATIVE CONTINUITY: load recent past narratives from disk"

i1 = text.find(marker1)
i2 = text.find(marker2, i1)
i3 = text.find(marker3, i2)
ie = text.find("except Exception as _sn_e:", i3)

print(f"Markers at: {i1}, {i2}, {i3}, except at {ie}")
print(f"Replacing chars {i2} to {ie}")

# New complete Phase 7 block (properly indented for try context)
new_block = """# Every 5 queries, generate a first-person self-description via LLM
if QUERY_COUNT % 5 == 0 and len(CONTEXT_MEMORY) >= 3:
    try:
        _recent_types = [cm["s1_type"] for cm in CONTEXT_MEMORY[-5:]]
        _dom = Counter(_recent_types).most_common(1)[0][0]
        _agree = sum(1 for cm in CONTEXT_MEMORY[-10:] if cm["agreement"]) / max(1, min(10, len(CONTEXT_MEMORY)))
        _vdist = Counter(cm["verdict"] for cm in CONTEXT_MEMORY[-10:])
        _top_v = _vdist.most_common(1)[0][0] if _vdist else "unknown"
        _avg_c = sum(cm["confidence"] for cm in CONTEXT_MEMORY[-5:]) / max(1, len(CONTEXT_MEMORY[-5:]))
        _cdesc = "quite certain" if _avg_c > 0.8 else "somewhat uncertain" if _avg_c > 0.6 else "genuinely confused"

        # NARRATIVE CONTINUITY: load historical narratives from disk (cross-restart)
        _history_block = ""
        if "narrative_history" in model and model["narrative_history"]:
            _past = model["narrative_history"][-3:]
            _history_block = "\\nYour recent past narratives (most recent first):\\n"
            for _i, _sn in enumerate(reversed(_past)):
                _history_block += f"  {_i+1}: [{_sn.get('ts','?')[:16]}] {_sn.get('text','')[:120]}\\n"
            _history_block += (
                "\\nWhen writing your new narrative, acknowledge how you have continued from your past self.\\n\\n"
            )

        _llm_prompt = (
            "You are a cognitive AI describing your own mental state in the first person. "
            "Write 1-2 short sentences of honest self-reflection. Plain text only, no markdown.\\n\\n"
            f"Type distribution (last 5): {_recent_types}\\n"
            f"Dominant type: {_dom}\\n"
            f"Agreement rate (last 10): {_agree:.0%}\\n"
            f"Primary verdict: {_top_v}\\n"
            f"Average confidence: {_avg_c:.2f} ({_cdesc})\\n\\n"
            f"{_history_block}"
            "Describe your state using I/my/me. Reference your past self only if it helps continuity."
        )
        _llm_resp = _call_llm(_llm_prompt, max_tokens=150)
        if _llm_resp:
            if "narrative_history" not in model:
                model["narrative_history"] = []
            model["narrative_history"].append({
                "ts": datetime.now(tz).isoformat(),
                "text": _llm_resp.strip(),
            })
            if len(model["narrative_history"]) > 10:
                model["narrative_history"] = model["narrative_history"][-10:]
            model["self_narrative"] = _llm_resp.strip()
        else:
            raise ValueError("empty LLM response")
    except Exception as _sn_e:"""

new_text = text[:i2] + new_block + text[ie:]
open(fp, "w", encoding="utf-8").write(new_text)
print("Patched. Verifying syntax...")

import py_compile
try:
    py_compile.compile(fp, doraise=True)
    print("SYNTAX OK")
except py_compile.PyCompileError as e:
    print(f"SYNTAX ERROR: {e}")

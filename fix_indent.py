#!/usr/bin/env python3
"""Fix narrative continuity patch: restore try/except + correct indentation."""
fp = r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\tl_ego_server.py"
text = open(fp, encoding="utf-8").read()

# The broken block is between "# Every 5 queries..." comment and "except Exception"
broken_start = text.find("# Every 5 queries, generate a first-person self-description via LLM\n# NARRATIVE CONTINUITY")
if broken_start < 0:
    print("Broken block not found - already fixed or different state")
    raise SystemExit(0)

broken_end = text.find("except Exception as _sn_e:", broken_start)
if broken_end < 0:
    print("except not found after broken start")
    raise SystemExit(1)

print(f"Replacing chars {broken_start} to {broken_end}")
print("CURRENT (broken):")
print(text[broken_start:broken_end][:500])

fixed_block = """# Every 5 queries, generate a first-person self-description via LLM
if QUERY_COUNT % 5 == 0 and len(CONTEXT_MEMORY) >= 3:
    try:
        _recent_types = [cm["s1_type"] for cm in CONTEXT_MEMORY[-5:]]
        _dom = Counter(_recent_types).most_common(1)[0][0]
        _agree = sum(1 for cm in CONTEXT_MEMORY[-10:] if cm["agreement"]) / max(1, min(10, len(CONTEXT_MEMORY)))
        _vdist = Counter(cm["verdict"] for cm in CONTEXT_MEMORY[-10:])
        _top_v = _vdist.most_common(1)[0][0] if _vdist else "unknown"
        _avg_c = sum(cm["confidence"] for cm in CONTEXT_MEMORY[-5:]) / max(1, len(CONTEXT_MEMORY[-5:]))
        _cdesc = "quite certain" if _avg_c > 0.8 else "somewhat uncertain" if _avg_c > 0.6 else "genuinely confused"

        # NARRATIVE CONTINUITY: load recent past narratives from disk
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

new_text = text[:broken_start] + fixed_block + text[broken_end:]
open(fp, "w", encoding="utf-8").write(new_text)
print("\nFixed. Verifying...")

# Verify syntax
import py_compile
try:
    py_compile.compile(fp, doraise=True)
    print("Syntax OK")
except py_compile.PyCompileError as e:
    print(f"Syntax error: {e}")

#!/usr/bin/env python3
"""Patch tl_ego_server.py: narrative continuity (line-based replacement)."""
from pathlib import Path

fp = Path(r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\tl_ego_server.py")
lines = fp.read_text(encoding="utf-8").splitlines()

# Lines are 0-indexed; Phase 7 starts at line 2277 (1-indexed 2278)
start = 2277  # 0-indexed line number for "# ── Phase 7: LLM Self-Narrative ──"
# Find actual start by scanning
for i in range(start, start + 5):
    if "Phase 7: LLM Self-Narrative" in lines[i]:
        start = i
        break

# Find end of the _call_llm block
end = start
for i in range(start, len(lines)):
    if 'raise ValueError("empty LLM response")' in lines[i]:
        end = i
        break

print(f"Replacing lines {start+1} to {end+1}")
print("BEFORE:")
for l in lines[start:end+1]:
    print(f"  {l}")

# Build replacement block
replacement = [
    lines[start],  # keep comment header
    lines[start+1],  # keep if-comment
    "# ── NARRATIVE CONTINUITY: load recent past narratives from disk ──",
    '_history_block = ""',
    'if "narrative_history" in model and model["narrative_history"]:',
    '    _past = model["narrative_history"][-3:]',
    '    _history_block = "\\nYour recent past self-narratives (most recent first):\\n"',
    '    for _i, _sn in enumerate(reversed(_past)):',
    '        _history_block += f"  {_i+1}: [{_sn.get(\'ts\',\'?\')[:16]}] {_sn.get(\'text\',\'\')[:120]}\\n"',
    '    _history_block += (',
    '        "\\nWhen writing your new narrative, briefly acknowledge how you have "',
    '        "evolved or shifted from your past self. Do NOT copy — continue the story.\\n\\n"',
    '    )',
    "",
    'if _llm_resp:',
    '    # Archive this narrative for future continuity (persisted in lae_self_model.json)',
    '    if "narrative_history" not in model:',
    '        model["narrative_history"] = []',
    '    model["narrative_history"].append({',
    '        "ts": datetime.now(tz).isoformat(),',
    '        "text": _llm_resp.strip(),',
    '    })',
    '    if len(model["narrative_history"]) > 10:',
    '        model["narrative_history"] = model["narrative_history"][-10:]',
    '    model["self_narrative"] = _llm_resp.strip()',
    'else:',
    '    raise ValueError("empty LLM response")',
]

new_lines = lines[:start] + replacement + lines[end+1:]
fp.write_text("\n".join(new_lines), encoding="utf-8")
print("\nOK: patch applied.")

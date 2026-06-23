"""Insert self-activation bridge code before first return entry"""
with open(r'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\tl_ego_server.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find first "return entry" that's at module top level (not in if/for)
target = None
for i, l in enumerate(lines):
    stripped = l.strip()
    if stripped == 'return entry' and i > 1900:
        target = i
        break

if target is None:
    print("ERROR: Could not find return entry")
    exit(1)

print(f"Inserting bridge before line {target+1}")

bridge_block = [
    '    # ── Phase 9: Self-Activation → Action Bridge ──\n',
    '    global SELF_ACTIVATION_COUNT, SELF_ACTIVATION_LAST_DRAIN_TICK\n',
    '    if (SELF_ACTIVATION_COUNT >= SELF_ACTIVATION_DRAIN_THRESHOLD\n',
    '            and SELF_ACTIVATION_LAST_DRAIN_TICK != RESTING_STATE["tick"]):\n',
    '        signal_dir = WORKSPACE / ".lae"\n',
    '        signal_dir.mkdir(parents=True, exist_ok=True)\n',
    '        signal = {\n',
    '            "source": "tl_ego_self_activation",\n',
    '            "count": SELF_ACTIVATION_COUNT,\n',
    '            "tick": RESTING_STATE["tick"],\n',
    '            "dominant": entry.get("dominant", "unknown"),\n',
    '            "concentration": entry.get("concentration", 0),\n',
    '            "final_state_top10": entry.get("final_state_top10", []),\n',
    '            "timestamp": time.time()\n',
    '        }\n',
    '        sig_path = signal_dir / "self_activation_signal.json"\n',
    '        try:\n',
    '            with open(sig_path, "w", encoding="utf-8") as sf:\n',
    '                json.dump(signal, sf, ensure_ascii=False)\n',
    '            print(f"[SELF-ACT] Signal written to {sig_path} '\
    f'count={{SELF_ACTIVATION_COUNT}}, dominant={{signal[\"dominant\"]}})", flush=True)\n',
    '            SELF_ACTIVATION_COUNT = 0\n',
    '            SELF_ACTIVATION_LAST_DRAIN_TICK = RESTING_STATE["tick"]\n',
    '            # Also emit to stdout for easy grep\n',
    '            print(f"LAUNCH_SIGNAL: {{\\"task\\": \\"self_activation\\", '
    '\\"count\\": {SELF_ACTIVATION_COUNT}, '
    '\\"dominant\\": \\"{signal[\'dominant\']}\\"}}", flush=True)\n',
    '        except Exception as _e:\n',
    '            print(f"[SELF-ACT] Signal write FAILED: {_e}", flush=True)\n',
    '\n',
]

new_lines = lines[:target] + bridge_block + lines[target:]
with open(r'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\tl_ego_server.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print(f"DONE: Inserted {len(bridge_block)} lines before line {target+1}")

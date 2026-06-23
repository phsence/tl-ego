"""Fix bridge code in tl_ego_server.py"""
with open(r'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\tl_ego_server.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Replace lines 2013-2021 with clean version using .format() or string concat
start = 2012  # 0-indexed
end = 2021    # exclusive

# Build replacement block (no f-string nesting issues)
replace = [
    '        sig_path = signal_dir / "self_activation_signal.json"\n',
    '        try:\n',
    '            with open(sig_path, "w", encoding="utf-8") as sf:\n',
    '                json.dump(signal, sf, ensure_ascii=False)\n',
    '            dom = signal["dominant"]\n',
    '            print("[SELF-ACT] Signal written, count=" + str(SELF_ACTIVATION_COUNT) +\n',
    '                  ", dominant=" + dom, flush=True)\n',
    '            SELF_ACTIVATION_COUNT = 0\n',
    '            SELF_ACTIVATION_LAST_DRAIN_TICK = RESTING_STATE["tick"]\n',
    '            # Emit LAUNCH_SIGNAL for heartbeat cron\n',
    '            ls = json.dumps({"task": "self_activation", "count": signal["count"],\n',
    '                             "dominant": signal["dominant"]})\n',
    '            print("LAUNCH_SIGNAL: " + ls, flush=True)\n',
]

new_lines = lines[:start] + replace + lines[end:]
with open(r'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\tl_ego_server.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print("Bridge code fixed")

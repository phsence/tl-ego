"""Check FNO integration state in tl_ego_server.py"""
import re

with open(r'projects/tl-ego/tl_ego_server.py', encoding='utf-8') as f:
    raw = f.read()

clean = re.sub(r'\x1b\[[0-9;]*m', '', raw)

# Find key sections
patterns = {
    'trajectory_append': 'trajectory"].append',
    'fno_predict_call': '_fno_predict(',
    'fno_choose_call': '_fno_choose_noise(',
    'fno_comment': 'FNO: run prediction',
    'rotate_mode_main': '_rotate_noise_mode()',
}

for name, pat in patterns.items():
    count = clean.count(pat)
    positions = [i for i in range(len(clean)) if clean.startswith(pat, i)]
    print(f"{name}: {count} occurrences at positions {positions[:5]}")

# Show the FNO prediction section
idx = clean.find('FNO: run prediction')
if idx >= 0:
    print("\n=== FNO prediction section ===")
    print(clean[idx:idx+700])
else:
    print("\n=== WARNING: FNO prediction section NOT FOUND ===")
    # Show what follows trajectory.append
    tidx = clean.find('trajectory"].append')
    if tidx >= 0:
        print(clean[tidx:tidx+500])

"""Merge old and live trajectory data for FNO training."""
import json

entries = []

# Old data (with noise fields only)
with open(r'projects/tl-ego/resting_trajectory.jsonl') as f:
    for line in f:
        e = json.loads(line.strip())
        if 'noise_mode' in e:
            entries.append(e)

# Live data
with open(r'projects/tl-ego/resting_trajectory_live.jsonl') as f:
    for line in f:
        entries.append(json.loads(line.strip()))

with open(r'projects/tl-ego/resting_trajectory_merged.jsonl', 'w') as f:
    for e in entries:
        f.write(json.dumps(e) + '\n')

print(f"Merged: {len(entries)} entries")
print(f"Tick range: {entries[0]['tick']} to {entries[-1]['tick']}")

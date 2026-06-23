import json
with open('projects/tl-ego/resting_check.json') as f:
    d = json.load(f)
print('Total ticks:', d['tick'], '| Last dominant:', d['last_dominant'])
print()
for t in d['trajectory']:
    types = sorted(t['type_counts'].items(), key=lambda x: -x[1])
    ts = ', '.join(f'{k}={v}' for k,v in types)
    print(f"  tick#{t['tick']} [{t['timestamp']}] {t['dominant']:12s} conc={t['concentration']:.2f} comp={t['competition']:.2f} | {ts}")

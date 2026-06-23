import urllib.request, json
with urllib.request.urlopen('http://127.0.0.1:8766/status', timeout=10) as r:
    s = json.loads(r.read().decode())
print('status ok:', s.get('status'), 'neurons=', s.get('neurons'), 'queries=', s.get('queries_served'))
print(json.dumps({k: s.get(k) for k in ['concentration_lock','type_stdp_breakthrough']}, ensure_ascii=False, indent=2))

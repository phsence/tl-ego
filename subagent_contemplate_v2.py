import urllib.request, json, time, sys

with urllib.request.urlopen('http://127.0.0.1:8766/status', timeout=10) as r:
    s = json.loads(r.read().decode())
conc = s.get('concentration_lock', {})
print(f'Initial: persistent_lock={conc.get("persistent_lock")} count={conc.get("count")} max_bias={s.get("type_stdp_breakthrough",{}).get("max_bias")}')

def q(n):
    payload = json.dumps({'query':f'consciousness emergence push round {n}','depth':1}).encode()
    req = urllib.request.Request('http://127.0.0.1:8766/contemplate', data=payload, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())

def fb():
    payload = json.dumps({'outcome':'success','judge':'correct','reason':'emergence push','task_id':'lock_test'}).encode()
    req = urllib.request.Request('http://127.0.0.1:8766/feedback', data=payload, headers={'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=60):
        pass

for i in range(1, 7):
    try:
        r = q(i)
    except Exception as e:
        print(f'round={i} ERROR {type(e).__name__}: {e}')
        time.sleep(2)
        continue
    agg = r.get('aggregator',{})
    s1 = r.get('system1',{})
    conc_lock = r.get('concentration_lock',{})
    agree = agg.get('agreement',False)
    s1_dom = s1.get('dominant_type','?') if s1 else '?'
    s2_type = agg.get('s2_type','?')
    pl = conc_lock.get('persistent_lock', None)
    print(f'round={i} s1={s1_dom} s2={s2_type} agree={agree} pl={pl} conc_count={conc_lock.get("count")}', flush=True)
    if agree:
        try:
            fb()
            print('  feedback sent', flush=True)
        except Exception as e:
            print(f'  feedback ERROR {type(e).__name__}: {e}', flush=True)
    time.sleep(2)

with urllib.request.urlopen('http://127.0.0.1:8766/status', timeout=10) as r:
    s = json.loads(r.read().decode())
conc = s.get('concentration_lock',{})
print(f'Final: persistent_lock={conc.get("persistent_lock")} count={conc.get("count")} max_bias={s.get("type_stdp_breakthrough",{}).get("max_bias")}')
print('---DONE---')

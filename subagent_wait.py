import urllib.request, json, time
start = time.time()
while time.time() - start < 300:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8766/status', timeout=10) as r:
            s = json.loads(r.read().decode())
            if s.get('status') == 'ok':
                print(f'READY after {time.time()-start:.0f}s: neurons={s.get("neurons")} queries={s.get("queries_served")}')
                break
    except Exception:
        pass
    time.sleep(15)
else:
    print('TIMEOUT - TL-Ego did not start')

import time, urllib.request, json, sys

start = time.time()
while time.time() - start < 300:
    try:
        r = urllib.request.urlopen('http://127.0.0.1:8766/status', timeout=5)
        data = json.loads(r.read())
        nc = data.get('neuron_count', 0)
        ic = data.get('init_complete', False)
        td = time.time() - start
        print(f'{td:.0f}s | neurons={nc} init={ic}', flush=True)
        if ic or nc > 90000:
            print(f'READY ({td:.0f}s)', flush=True)
            sys.exit(0)
    except Exception as e:
        td = time.time() - start
        if td > 5:
            print(f'{td:.0f}s | waiting... {e}', flush=True)
    time.sleep(10)

print('TIMEOUT', flush=True)
sys.exit(1)

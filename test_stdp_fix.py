"""Quick test: verify STDP fix works under concurrent load."""
import subprocess, json, threading, sys, time, os

# Force UTF-8 output
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:8766"
errors = []
lock = threading.Lock()

def query(qid, text):
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{BASE}/contemplate",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"query": text, "depth": 1})],
            capture_output=True, text=True, timeout=20)
        d = json.loads(r.stdout)
        if "error" in d and d["error"] is not None:
            with lock:
                errors.append(f"Q{qid}: ERROR {d['error']}")
        else:
            updates = d.get("stdp", {}).get("updates", 0)
            verdict = d.get("verdict", "?")
            print(f"  Q{qid}: OK verdict={verdict} stdp={updates}")
    except Exception as e:
        with lock:
            errors.append(f"Q{qid}: EXCEPTION {e}")

# Test 1: Single query (was crashing before)
print("[test1] Single query...")
query(1, "你的 resting_tick 有消费配置文件吗？")

# Test 2: Concurrent queries to stress-test the lock
print("[test2] 3 concurrent queries...")
threads = []
for i in range(3):
    t = threading.Thread(target=query, args=(i+2, f"concurrent test {i+1}"))
    threads.append(t)
for t in threads:
    t.start()
for t in threads:
    t.join()

# Test 3: Check server still healthy
r = subprocess.run(["curl", "-s", f"{BASE}/status"], capture_output=True, text=True, timeout=5)
d = json.loads(r.stdout)
print(f"\n[test3] Status: {d['status']} queries={d['queries_served']} stdp_deltas={d['stdp_deltas']}")

if errors:
    print(f"\nFAIL: {len(errors)} errors:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
else:
    print("\nPASS: STDP race condition fixed - 0 errors under concurrent load")

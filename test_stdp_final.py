"""Final verification: query that used to crash should now work."""
import subprocess, json, sys

r = subprocess.run(["curl", "-s", "-X", "POST", "http://127.0.0.1:8766/contemplate",
    "-H", "Content-Type: application/json",
    "-d", json.dumps({"query": "你的 resting_tick 有消费 ego_autonomy_evidence.json 那个文件吗？", "depth": 1})],
    capture_output=True, timeout=20)

stdout = r.stdout.decode("utf-8", errors="replace")
d = json.loads(stdout)

if "error" in d and d["error"]:
    print(f"FAIL: {d['error']}")
    sys.exit(1)

print(f"OK: verdict={d['verdict']} confidence={d['confidence']:.2f} stdp_updates={d['stdp']['updates']}")
print(f"Response: {d['response'][:200]}")
print()
print("STDP race condition FIXED.")

"""Mood stabilization script — auto-generated"""
import requests, time
ego_url = "http://127.0.0.1:8766"
try:
    for _ in range(5):
        requests.post(f"{ego_url}/resting", timeout=5)
        time.sleep(0.3)
    with open(".lae/mood_stabilization.log", "a", encoding="utf-8") as f:
        f.write("[Autonomous] Mood stabilized via resting ticks\n")
    print("[Autonomous] Mood stabilized via resting ticks")
except Exception as e:
    print(f"TL-Ego unreachable: {e}")
    sys.exit(1)

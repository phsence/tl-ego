"""Generic ego script — auto-generated"""
import requests
ego_url = "http://127.0.0.1:8766"
try:
    requests.post(f"{ego_url}/resting", timeout=5)
    with open(".lae/ego_gen.log", "a", encoding="utf-8") as f:
        f.write("[Autonomous] ego_gen: Initiate Safe Exploration Protocol executed\n")
    print("[Autonomous] ego_gen: Initiate Safe Exploration Protocol executed")
except Exception as e:
    print(f"TL-Ego unreachable: {e}")
    sys.exit(1)

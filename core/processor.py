"""Sensory uncertainty logger — auto-generated creative task"""
import requests, json, time, csv, os
from datetime import datetime
ego_url = "http://127.0.0.1:8766"
log_path = ".lae/sensory_uncertainty.csv"
try:
    r = requests.get(f"{ego_url}/status", timeout=5)
    s = r.json()
    now = datetime.now().isoformat()
    bias_count = s.get("bias_seed_count", 0)
    max_bias = s.get("type_stdp_breakthrough", {}).get("max_bias", 0)
    comp = s.get("competition", 0)
    exists = os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "bias_seed", "max_bias", "competition", "uncertainty_score"])
        uncertainty = max(0, 1 - (bias_count / max(1, bias_count + 10)))
        w.writerow([now, bias_count, max_bias, comp, round(uncertainty, 4)])
    print(f"Sensory uncertainty logged: bias_seed={bias_count} max_bias={max_bias} uncertainty={uncertainty:.4f}")
except Exception as e:
    print(f"TL-Ego unreachable: {e}")
    sys.exit(1)

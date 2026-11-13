"""观察参数改动后 conc/comp 是否落到新稳态。

用途单一：RCT 开始前确认 live 系统的动力学已经反映新参数值。不做这一步，
对照臂的头几格会带着上一个参数值的残留，把效应量冲掉一半 —— 这正是
2026-11-10 那次 "alpha_seed_mix 戏剧性效应" 实为漂移的原因。

    python watch_settle.py --ticks 3 --timeout 2400
"""
import argparse
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8766"


def status(timeout=25):
    with urllib.request.urlopen(BASE + "/status", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=2400)
    a = ap.parse_args()

    t0 = time.time()
    seen = []
    last_tick = None
    while len(seen) < a.ticks and time.time() - t0 < a.timeout:
        try:
            st = status()
        except Exception as exc:                       # noqa: BLE001
            print(f"[wait] status 读取失败: {exc}", flush=True)
            time.sleep(20)
            continue
        tick = st.get("resting_ticks")
        if tick != last_tick:
            conc = st.get("resting_concentration")
            comp = st.get("resting_competition")
            cap = st.get("resting_state_type_dominance_cap")
            seen.append(conc)
            print(f"[settle] tick={tick} conc={conc} comp={comp} "
                  f"elapsed={time.time() - t0:.0f}s", flush=True)
            last_tick = tick
        time.sleep(10)

    vals = [v for v in seen if isinstance(v, (int, float))]
    if vals:
        print(f"[settle] n={len(vals)} conc mean={sum(vals)/len(vals):.4f} "
              f"min={min(vals):.4f} max={max(vals):.4f}", flush=True)
    else:
        print("[settle] 未采到任何 tick", flush=True)


if __name__ == "__main__":
    main()

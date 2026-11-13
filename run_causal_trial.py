"""First REAL causal A/B trial: diversity_n on next-tick self-prediction fit.

Wiring (all pre-existing, nothing invented):
  apply  -> POST /apply-parameter   (diversity_n is in PARAM_WHITELIST, user-unlocked 09-17)
  tick   -> POST /resting {}        (runs one resting tick synchronously)
  read   -> GET  /status .proprioceptive_loop.last_error  (binary: predicted==actual dominant)
  outcome = 1.0 - last_error        (same sign convention as _causal_lagged_observations)

Hygiene: the param is RESTORED to its pre-trial value in a finally block. A trial
must not leave the live system perturbed.
Run from projects/tl-ego/:  python run_causal_trial.py
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, r"C:\Users\Administrator\.openclaw\workspace\projects\tl-ego")
import causal_ab_arm as C

BASE = "http://127.0.0.1:8766"
WS = r"C:\Users\Administrator\.openclaw\workspace"
PARAM = sys.argv[1] if len(sys.argv) > 1 else "diversity_n"
DELTA = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
N_ARM = int(sys.argv[3]) if len(sys.argv) > 3 else 15


def post(path, body, timeout=120):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get_status(timeout=30):
    with urllib.request.urlopen(BASE + "/status", timeout=timeout) as r:
        return json.loads(r.read().decode())


def tick_and_read():
    """Force one resting tick, then read the transition's prediction fit.

    /resting blocks on RESTING_LOCK, which the natural tick loop also holds
    (~25s/cell). The first n=15 trial burned ~95 of its 120 attempts on lock
    timeouts (final: 12/13 samples) — retry instead of abstaining.
    """
    last_err = None
    for _ in range(3):
        try:
            post("/resting", {}, timeout=240)
            break
        except Exception as e:
            last_err = e
    else:
        print(f"  [abstain] /resting failed 3x: {last_err}", flush=True)
        return None, None
    st = get_status()
    pl = st.get("proprioceptive_loop") or {}
    err = pl.get("last_error")
    if not isinstance(err, (int, float)):
        return None, st
    return 1.0 - float(err), st


def main():
    st = get_status()
    gen = st.get("runtime_generation_id")
    tick0 = st.get("resting_ticks")
    print(f"[trial] gen={gen} start_tick={tick0} param={PARAM} delta=+{DELTA} n_arm={N_ARM}", flush=True)

    # Discover the CURRENT runtime value via a no-op apply (same value back).
    probe = None
    for guess in (8.0, 8, 0.3, 0.5, 1.0, 10, 0.0):
        r = post("/apply-parameter", {"key": PARAM, "value": guess,
                                      "reason": "ab-trial-probe", "source": "causal_ab_arm"})
        if r.get("status") == "ok":
            probe = r.get("old_value")
            break
    if probe is None:
        print("[trial] cannot discover current value; abort", flush=True)
        return
    control = float(probe)
    print(f"[trial] runtime {PARAM} = {control} (restoring to this after)", flush=True)

    orig = control
    try:
        def arm_runner(arm, value, tick):
            r = post("/apply-parameter", {"key": PARAM, "value": float(value),
                                          "reason": f"causal_ab_arm {arm}",
                                          "source": "causal_ab_arm"})
            if r.get("status") != "ok":
                print(f"[trial] apply failed: {r}", flush=True)
                return None
            out, st = tick_and_read()
            if out is None:
                return None
            print(f"  {arm:12s} value={value:<6} tick={st.get('resting_ticks')} "
                  f"predicted={ (st.get('proprioceptive_loop') or {}).get('predicted') } "
                  f"actual={ (st.get('proprioceptive_loop') or {}).get('actual') } "
                  f"outcome={out}", flush=True)
            return out

        res = C.run_ab(WS, PARAM, delta=DELTA, n_arm=N_ARM, seed=None,
                       runtime_generation_id=gen, control_value=control,
                       outcome_metric="proprioceptive_next_tick_fit",
                       arm_runner=arm_runner)
        print("\n[trial] RESULT:", json.dumps(
            {k: res.get(k) for k in ("n_control", "n_intervention", "control_mean",
                                     "intervention_mean", "effect_size", "effect_ci95",
                                     "eligible", "reason")}, ensure_ascii=False), flush=True)
        ev = res.get("intervention_evidence")
        print("[trial] gate-eligible evidence:", "YES " + json.dumps(ev)[:220] if ev else "no (honest null/underpowered)", flush=True)
    finally:
        r = post("/apply-parameter", {"key": PARAM, "value": orig,
                                      "reason": "causal_ab_trial restore", "source": "causal_ab_arm"})
        print(f"[trial] restored {PARAM}={orig} -> {r.get('status')}", flush=True)


if __name__ == "__main__":
    main()

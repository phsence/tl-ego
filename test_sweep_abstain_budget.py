"""Check: ABSTAIN must not consume sweep budget (2026-11-11 bug).

The auto loop printed "不消耗预算" on abstain but did done += 1 — on a slow
ego night (8 min/tick, settle=2 -> sample windows longer than the wait),
3 abstains burned the whole budget and the sweep exited having measured
nothing. Abstain is infrastructure starvation, not a completed round.

Test: run_one always returns None (pure abstain), budget=1. Correct behavior
= loop runs to max_attempts (4), policy decayed 4 times, exits 0 with
"提前收工: 0/1". The bug = exits after 1 attempt.
"""
import io
import sys
import contextlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import causal_param_sweep as S  # noqa: E402


def run():
    calls = {"n": 0}

    def fake_run_one(param, delta=None, n_arm=10, metric=None, settle_ticks=2):
        calls["n"] += 1
        return None  # pure abstain, every time

    orig_sleep = S.time.sleep
    orig_argv = sys.argv
    orig_run_one, orig_choose = S.run_one, S.choose
    orig_load, orig_save = S.load_policy, S.save_policy
    sys.argv = ["causal_param_sweep.py", "--auto", "--budget", "1"]
    S.run_one = fake_run_one
    S.choose = lambda pol, rng, pool: (pool[0], "exploit")
    S.load_policy = lambda: {"version": 99, "params": {p: {"prior": 1.0, "trials": 0,
                                                           "eligible": 0, "hits": 0,
                                                           "direction": 1}
                                                      for p in S.CANDIDATES},
                             "delta_scale": 1.0}
    saved = []
    S.save_policy = lambda pol: saved.append(pol.get("version"))
    S.time.sleep = lambda s: None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = S.main()
        out = buf.getvalue()
    finally:
        S.run_one, S.choose = orig_run_one, orig_choose
        S.load_policy, S.save_policy = orig_load, orig_save
        S.time.sleep = orig_sleep
        sys.argv = orig_argv

    attempts = calls["n"]
    assert attempts == 4, f"expected max_attempts=4 attempts, got {attempts} (budget burned by abstain?)"
    assert "0/1" in out, out[-300:]
    assert rc == 0
    assert len(saved) >= 4, f"policy should decay once per abstain, got {len(saved)} saves"
    print(f"OK: abstain does not consume budget — {attempts} attempts (max_attempts), "
          f"0 rounds done, policy decayed {len(saved)}x, clean exit")


if __name__ == "__main__":
    run()

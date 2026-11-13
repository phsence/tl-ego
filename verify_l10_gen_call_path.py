"""Is the server's l10_gen_trigger call path live, or just quiet?

The log shows 0 "generator bridge ERROR" after the rebuild, which is ambiguous:
it is equally the count for "bridge raised every tick" before the fix and for
"bridge never ran at all". Only an explicit run distinguishes those.

This drives the REAL server call path the same way tl_ego_server does --
    from l10.l10_generator_bridge import l10_gen_trigger
    l10_gen_trigger(RESTING_STATE, result, runtime_generation_id=...)
-- on a redirected scratch _LAE, so the live cooldown/audit stay untouched, and
asserts the three outcomes the server depends on:
  1. it does not raise;
  2. it returns a real bool (None would silently read as "no trigger");
  3. it parks a usable result dict for the next tick's kwta consumer.

Two traps this script exists to keep honest:
  - "l10.l10_generator_bridge" and "l10_generator_bridge" are SEPARATE module
    instances with separate globals, so redirecting only the one imported first
    still writes a real audit line. Both are redirected, and the live audit line
    count is asserted afterwards.
  - git-bash exports TMPDIR=H:/Temp, which native Python cannot create under,
    so the scratch dir is pinned next to this script rather than via tempfile.

Run: python verify_l10_gen_call_path.py
"""
import json
import os
import shutil
import sys
from pathlib import Path
from tempfile import mkdtemp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "l10"))

import l10_generator_bridge as B  # noqa: E402

# live invariants that must not move
LIVE_AUDIT = r"C:\Users\Administrator\.openclaw\workspace\.lae\l10_gen_audit.jsonl"
LIVE_STATE = r"C:\Users\Administrator\.openclaw\workspace\.lae\l10_gen_state.json"
AUDIT_BASE_LINES = 457
STATE_BASE_TICK = 28225

_BRIDGE_PATHS = {"_LAE": None, "_STATE_FILE": "s.json", "_AUDIT_LOG": "a.jsonl"}
_GEN_PATHS = {"_LAE": None,
              "_GENERATOR_LOG": "l10_meta_algorithm_generator.jsonl",
              "QD_ARCHIVE_SQLITE": "qd_archive.sqlite"}


class _Redirect:
    """Point every live-path constant of every involved module at `td`."""

    def __init__(self, mods, gen_mod, td, observer_line):
        self._mods, self._gen, self._td = mods, gen_mod, td
        self._saved = {}
        (td / "l10_observer.jsonl").write_text(json.dumps(observer_line), encoding="utf-8")

    def __enter__(self):
        for mod in self._mods:
            for k, fn in _BRIDGE_PATHS.items():
                self._saved[(id(mod), k)] = mod.__dict__.get(k)
                mod.__dict__[k] = self._td if fn is None else self._td / fn
        for k, fn in _GEN_PATHS.items():
            self._saved[(id(self._gen), k)] = getattr(self._gen, k)
            setattr(self._gen, k, self._td if fn is None else self._td / fn)
        return self

    def __exit__(self, *exc):
        for (oid, k), v in self._saved.items():
            mod = self._gen if oid == id(self._gen) else \
                next(m for m in self._mods if id(m) == oid)
            if k in _GEN_PATHS:
                setattr(mod, k, v)
            else:
                mod.__dict__[k] = v
        return False


def main():
    # the exact import form tl_ego_server.py:17345 uses
    import inspect
    import l10.l10_generator_bridge as B_dotted

    l10_gen_trigger = B_dotted.l10_gen_trigger
    params = inspect.signature(l10_gen_trigger).parameters
    assert "runtime_generation_id" in params, \
        "server passes runtime_generation_id but the bridge does not accept it: %s" % list(params)
    print("server import form OK: l10_gen_trigger(%s)" % ", ".join(params))

    raw = B._read_latest_l10_profile()
    assert raw is not None, "no L10 observer profile to drive the trigger"
    prof = raw.get("profile") or {}
    print("live L10 profile: dead_types=%d avg_diversity=%s"
          % (len(prof.get("dead_types") or []), prof.get("avg_diversity")))

    gen_mod = B._load_py_module("l10_meta_algorithm_generator",
                                "l10_meta_algorithm_generator.py")

    td = Path(mkdtemp(prefix="_verify_l10gen_", dir=HERE))
    try:
        with _Redirect((B, B_dotted), gen_mod, td, raw):
            RESTING_STATE = {"tick": 10}
            result = {"_dq_forced_type": "emotional", "_dq_adjusted": True,
                      "_dq_reason": "self_loop:integrator:7"}
            fired = l10_gen_trigger(RESTING_STATE, result,
                                    runtime_generation_id="ego-verify")
            out = RESTING_STATE.get("_l10_gen_result")
            print("fired: %r (type %s)" % (fired, type(fired).__name__))
            assert isinstance(fired, bool), "returned %r, not a bool" % (fired,)
            assert isinstance(out, dict), "no result parked for the next tick"
            assert "error" not in out, "bridge failed: %s" % out["error"]
            assert "trigger_reason" in out and "signals" in out, sorted(out)

            cands = out.get("algorithm_candidates") or []
            print("trigger_reason=%s candidates=%d" % (out["trigger_reason"], len(cands)))
            if fired and cands:
                strat = cands[0].get("strategy") or {}
                print("candidate[0]: strategy.type=%s dsl=%s"
                      % (strat.get("type"), bool(cands[0].get("dsl"))))
                assert isinstance(strat, dict) and strat, cands[0]

            # cooldown: the server calls this EVERY tick, so firing every tick
            # would flood the audit log; the 50-tick cooldown must hold
            assert l10_gen_trigger({"tick": 11}, {}) is False, "cooldown did not hold"
            n = len((td / "a.jsonl").read_text(encoding="utf-8").splitlines()) \
                if (td / "a.jsonl").exists() else 0
            print("audit records written by 2 ticks: %d (cooldown held)" % n)
            assert n <= 1, n
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # The whole point: NOTHING may reach the live paths.
    n_now = len(open(LIVE_AUDIT, encoding="utf-8").read().splitlines())
    assert n_now == AUDIT_BASE_LINES, \
        "live audit grew to %d (base %d) — a path escaped the redirect" % (n_now, AUDIT_BASE_LINES)
    st = json.load(open(LIVE_STATE, encoding="utf-8"))
    assert st["last_tick"] == STATE_BASE_TICK, "live cooldown moved to %r" % st

    print("\nOK: server call path imports, runs, returns a real bool, parks a "
          "consumer-ready result, cooldown holds, live state untouched")


if __name__ == "__main__":
    main()
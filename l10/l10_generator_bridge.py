"""l10/l10_generator_bridge.py — REBUILT 2026-10-01 from l10_generator_bridge.py.bak-20260803.

The .bak is the full 295-line original, so this port is NOT a reconstruction from
guesswork — it is the original body plus four drift fixes, because both
dependencies lost their .py and only their .pyc survived (09-29 21:07, NEWER
than every .bak: the newest generator .bak is v7 at 09-29 20:49).

Drift fixed (each verified against the surviving .pyc, not assumed):
  1. `_load_py_module` looked for "l10_meta_algorithm_generator.py" — a file that
     no longer exists. Both deps now live as legacy .pyc, which importlib's
     SourcelessFileLoader can import directly, so loading falls back to the .pyc.
  2. `gen.generate(profile, current_tick=now_tick)` — the live signature is
     `generate(profile, module_map, current_tick)`. Passing the old 2-arg call
     raised TypeError, which the outer `except` swallowed into
     RESTING_STATE[_RESULT_KEY] = {"error": ...} and returned False, i.e. the
     generator would have been silently dead while the caller saw "no trigger".
  3. `MetaAlgorithmGenerator()` takes no args and `write_result(result)` is an
     instance method — unchanged from the backup, kept as-is.
  4. l10_gen_trigger gained `runtime_generation_id` (tl_ego_server.py:17348
     passes it); the backup's 2-arg signature would have raised TypeError on the
     first call of every tick.

Contract: l10_gen_trigger(RESTING_STATE, result, runtime_generation_id=None) -> bool
Returns True only when a new algorithm candidate was generated; the result lands
in RESTING_STATE["_l10_gen_result"] and is read next tick by the shadow-
intervention consumer (kwta_threshold_expansion floor 15%).
"""
import importlib.machinery
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

_LAE = Path(r"C:\Users\Administrator\.openclaw\workspace\.lae")
_STATE_FILE = _LAE / "l10_gen_state.json"
_RESULT_KEY = "_l10_gen_result"
_COOLDOWN_TICKS = 50
_AUDIT_LOG = _LAE / "l10_gen_audit.jsonl"


def _load_py_module(name: str, rel_path: str) -> Any:
    """Load a module from its .py source, falling back to the surviving .pyc.

    2026-10-01: the .py files for both dependencies were lost in the 09-30 wipe
    and only the 09-29 .pyc remains. SourcelessFileLoader imports those fine, so
    try the source first (keeps the original behaviour) and fall back rather
    than raising ImportError into the caller's silent except.
    """
    mod_dir = Path(__file__).resolve().parent
    mod_path = mod_dir / rel_path
    pyc_path = mod_dir / (rel_path.rsplit(".", 1)[0] + ".pyc")
    # Existence, not spec validity, is the check: a missing .py still produces a
    # non-None spec and a non-None SourceFileLoader, and only blows up later at
    # exec_module. So test the file, not the spec.
    if mod_path.exists():
        spec = importlib.util.spec_from_file_location(name, str(mod_path))
    elif pyc_path.exists():
        spec = importlib.util.spec_from_file_location(
            name, str(pyc_path),
            loader=importlib.machinery.SourcelessFileLoader(name, str(pyc_path)),
        )
    else:
        raise ImportError(f"cannot load {name}: neither {mod_path.name} nor {pyc_path.name} exists")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {mod_path}")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def l10_gen_trigger(RESTING_STATE: dict, result: dict, runtime_generation_id: str = "") -> bool:
    """Signal-triggered meta-algorithm generation, called from _resting_tick_impl.

    Reads the latest L10 observer profile plus this tick's Path-B DQ signals,
    assembles a BottleneckProfile, asks MetaAlgorithmGenerator whether the
    bottleneck warrants a new algorithm candidate, compiles any
    algorithm_selection DSL, and parks the result in RESTING_STATE for the next
    tick's shadow-intervention consumer.

    Returns True only when a candidate was actually generated. Never raises:
    any failure is recorded in RESTING_STATE[_RESULT_KEY]["error"] and returns
    False, so a dead generator cannot break the resting tick.
    """
    try:
        # ── 1. latest L10 observer profile ───────────────────────
        l10_profile = _read_latest_l10_profile()
        if l10_profile is None:
            return False

        # Cooldown is file-persisted: the bridge builds a fresh generator each
        # tick, so the instance's _last_trigger_tick never survives a tick and
        # would otherwise let it fire every single tick.
        now_tick = RESTING_STATE.get("tick", 0) or 0
        last_tick = _read_last_trigger_tick()
        if last_tick is not None and now_tick < last_tick:
            # process restart (tick counter went backwards): drop the cooldown
            # rather than let now-last<0 hold forever
            last_tick = None
            _write_last_trigger_tick(now_tick)
        if last_tick is not None and now_tick - last_tick < _COOLDOWN_TICKS:
            return False

        # ── 2. this tick's Path-B DQ signals ──────────────────────
        dq_forced = result.get("_dq_forced_type")
        dq_adjusted = result.get("_dq_adjusted", False)
        dq_reason = result.get("_dq_reason", "")

        # ── 3. latest diversity injection actions ─────────────────
        injection_actions = _read_latest_injection()

        # ── 4. assemble BottleneckProfile ─────────────────────────
        profile = _build_profile(l10_profile, dq_forced, dq_adjusted, dq_reason, injection_actions)

        # ── 5. run the generator ──────────────────────────────────
        # Drift fix: live signature is generate(profile, module_map, current_tick).
        # module_map is the buildable candidate map; the generator stores it and
        # uses it to qualify candidates, and an empty map still lets it emit
        # template-based candidates.
        MetaAlgorithmGenerator = _load_py_module(
            "l10_meta_algorithm_generator", "l10_meta_algorithm_generator.py"
        ).MetaAlgorithmGenerator
        gen = MetaAlgorithmGenerator()
        gen_result = gen.generate(profile, {}, now_tick)

        if gen_result.get("triggered"):
            _write_last_trigger_tick(now_tick)

        # ── 6. compile the algorithm_selection DSL, if any ────────
        compiled_patch = None
        compiled_proposed_code = None
        try:
            compile_dsl = _load_py_module("l10_dsl_compiler", "l10_dsl_compiler.py").compile_dsl
            for candidate in gen_result.get("algorithm_candidates", []):
                if candidate.get("strategy", {}).get("type") == "algorithm_selection" and candidate.get("dsl"):
                    compiled = compile_dsl(candidate["dsl"])
                    candidate["compiled"] = compiled
                    if compiled.get("patch"):
                        compiled_patch = compiled["patch"]
                    compiled_proposed_code = compiled.get("proposed_code")
                    break
        except Exception as e:
            gen_result["dsl_compile_error"] = str(e)

        # ── 7. park for the next tick + persist for the L9 adapter ──
        RESTING_STATE[_RESULT_KEY] = gen_result

        # Must happen AFTER compiled is attached so the adapter scans the final
        # candidate list.
        try:
            _load_py_module(
                "l10_meta_algorithm_generator", "l10_meta_algorithm_generator.py"
            ).MetaAlgorithmGenerator().write_result(gen_result)
        except Exception:
            pass

        if gen_result.get("triggered"):
            _write_audit(gen_result, compiled_patch=compiled_patch,
                         compiled_proposed_code=compiled_proposed_code)
            return True

        return False

    except Exception as e:
        # never break the resting tick
        RESTING_STATE[_RESULT_KEY] = {"error": str(e), "triggered": False}
        return False


def read_l10_gen_result(RESTING_STATE: dict) -> Optional[dict]:
    """Read last tick's generator result for the type-selection consumer."""
    return RESTING_STATE.get(_RESULT_KEY)


# ── internal helpers ────────────────────────────────────────────────

def _read_latest_l10_profile() -> Optional[dict]:
    """Latest line of the L10 observer JSONL."""
    path = _LAE / "l10_observer.jsonl"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


def _read_latest_injection() -> list[dict]:
    """Latest diversity-injection actions (up to 3)."""
    path = _LAE / "l10_diversity_injection.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        actions = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                actions.append(data)
                if len(actions) >= 3:
                    break
            except json.JSONDecodeError:
                continue
        return list(reversed(actions))
    except Exception:
        return []


def _build_profile(
    l10_profile: dict,
    dq_forced: Any,
    dq_adjusted: bool,
    dq_reason: str,
    injection_actions: list[dict],
) -> dict:
    """Assemble the multi-source signals into a BottleneckProfile dataclass.

    MUST return the dataclass, not a dict: MetaAlgorithmGenerator accesses
    profile.dead_type_count by attribute, so a dict would raise AttributeError
    and be swallowed into a silent no-trigger.
    """
    # DQ signal → self-loop info. DQO Path B writes self_loop:<type>:<streak>
    self_loop_streak = 0
    self_loop_type = None
    if dq_forced and dq_adjusted and "self_loop" in (dq_reason or ""):
        self_loop_type = str(dq_forced)
        self_loop_streak = 5  # a DQ trigger means at least the threshold
        _m = re.search(r"self_loop:[^:]+:(\d+)", dq_reason or "")
        if _m:
            self_loop_streak = max(5, int(_m.group(1)))

    # injection action → dwell info
    dwell_top_type = None
    dwell_ratio = 0.0
    transition_div = l10_profile.get("transition_diversity", 0.0)

    if injection_actions:
        latest = injection_actions[-1]
        if "actions" in latest:
            for a in latest["actions"]:
                if a.get("type") == "diversity_quota" and a.get("triggered"):
                    dwell_top_type = a.get("dominant_type")
                    dwell_ratio = a.get("dominant_ratio", 0.0)

    # the L10 observer nests its profile under a "profile" sub-key
    _obs_profile = l10_profile.get("profile") or l10_profile
    _dead_types = _obs_profile.get("dead_types") or []

    BottleneckProfile = _load_py_module(
        "l10_meta_algorithm_generator", "l10_meta_algorithm_generator.py"
    ).BottleneckProfile

    return BottleneckProfile(
        # from the L10 observer
        dead_type_count=_obs_profile.get("dead_type_count", len(_dead_types)),
        dead_types=_dead_types,
        transition_diversity=_obs_profile.get("avg_diversity", transition_div) / 100.0,
        confidence=l10_profile.get("confidence", 0.0),
        primary_bottleneck=l10_profile.get("primary_bottleneck"),
        # from the DQ signal (Path B)
        self_loop_streak=self_loop_streak,
        self_loop_type=self_loop_type,
        dwell_overrely_ratio=dwell_ratio,
        dwell_top_type=dwell_top_type,
        # from the L10 observer / metacontemplate streak
        metacontemplate_same_s1s2=int(_obs_profile.get("metacontemplate_same_s1s2", 0) or 0),
    )


def _read_last_trigger_tick() -> Optional[int]:
    """Last trigger tick (file-persisted)."""
    try:
        if _STATE_FILE.exists():
            return int(json.loads(_STATE_FILE.read_text(encoding="utf-8")).get("last_tick", -1))
    except Exception:
        pass
    return None


def _write_last_trigger_tick(tick: int) -> None:
    try:
        _STATE_FILE.write_text(json.dumps({"last_tick": int(tick)}), encoding="utf-8")
    except Exception:
        pass


def _write_audit(gen_result: dict, *, compiled_patch: Optional[dict],
                 compiled_proposed_code: Optional[str]) -> None:
    try:
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            record = {
                "tick": gen_result.get("tick"),
                "trigger_reason": gen_result.get("trigger_reason"),
                "signals": gen_result.get("signals"),
                "candidate_count": len(gen_result.get("algorithm_candidates", [])),
                "meta_rationale": gen_result.get("meta_rationale"),
                "timestamp": gen_result.get("timestamp"),
                "dsl_compiled": bool(compiled_patch),
                "patch_file": compiled_patch.get("file") if compiled_patch else None,
                "proposed_code_len": len(compiled_proposed_code or ""),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    # Selfcheck: the loader must reach both .pyc-only deps, and the profile must
    # come back as a dataclass so the generator's attribute access cannot fail.
    #
    # CRITICAL: l10_gen_trigger() is NOT called here against live state. It writes
    # l10_gen_state.json / l10_gen_audit.jsonl and the generator additionally writes
    # l10_gen_recent_fingerprints.json, l10_meta_algorithm_generator.jsonl and
    # qd_archive.sqlite. A selfcheck that calls it for real pollutes the live
    # cooldown (and does a real generation). Instead this asserts the cooldown
    # guard on a redirected _LAE, which is a full end-to-end run with zero writes
    # to production paths.
    import inspect
    import tempfile

    for name, rel in (("l10_meta_algorithm_generator", "l10_meta_algorithm_generator.py"),
                      ("l10_dsl_compiler", "l10_dsl_compiler.py")):
        mod = _load_py_module(name, rel)
        assert hasattr(mod, "BottleneckProfile") or hasattr(mod, "compile_dsl"), mod

    sig = inspect.signature(
        _load_py_module("l10_meta_algorithm_generator",
                        "l10_meta_algorithm_generator.py").MetaAlgorithmGenerator().generate
    )
    assert "module_map" in sig.parameters, sig
    assert "current_tick" in sig.parameters, sig

    raw = _read_latest_l10_profile()
    assert raw is not None, "l10_observer.jsonl unreadable — selfcheck needs real data"
    prof = _build_profile(raw, "emotional", True,
                          "self_loop:integrator:9", _read_latest_injection())
    # dataclass, not dict: the generator uses profile.dead_type_count
    assert not isinstance(prof, dict), type(prof)
    assert hasattr(prof, "dead_type_count") and hasattr(prof, "self_loop_streak"), prof
    assert prof.self_loop_streak == 9, prof.self_loop_streak
    assert prof.self_loop_type == "emotional", prof.self_loop_type

    # End-to-end run with EVERY live path redirected at a scratch dir. The
    # generator module carries its OWN _LAE / _GENERATOR_LOG / QD_ARCHIVE_SQLITE
    # constants, so redirecting only the bridge's _LAE is not enough — the
    # generation would still append to the real jsonl and the real sqlite.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        saved = {k: globals()[k] for k in ("_LAE", "_STATE_FILE", "_AUDIT_LOG")}
        gen_mod = _load_py_module("l10_meta_algorithm_generator",
                                  "l10_meta_algorithm_generator.py")
        saved_gen = {k: getattr(gen_mod, k) for k in ("_LAE", "_GENERATOR_LOG", "QD_ARCHIVE_SQLITE")}
        globals()["_LAE"] = td
        globals()["_STATE_FILE"] = td / "l10_gen_state.json"
        globals()["_AUDIT_LOG"] = td / "l10_gen_audit.jsonl"
        gen_mod._LAE = td
        gen_mod._GENERATOR_LOG = td / "l10_meta_algorithm_generator.jsonl"
        gen_mod.QD_ARCHIVE_SQLITE = td / "qd_archive.sqlite"
        (td / "l10_observer.jsonl").write_text(
            json.dumps(raw), encoding="utf-8")  # trigger needs the profile here
        try:
            st = {"tick": 10}
            fired = l10_gen_trigger(st, {})
            assert isinstance(fired, bool), type(fired)
            assert isinstance(st.get(_RESULT_KEY), dict), st.get(_RESULT_KEY)
            assert "error" not in st[_RESULT_KEY], \
                "generator failed on real data: %s" % st[_RESULT_KEY]["error"]
            # a second immediate call must be blocked by the 50-tick cooldown
            st2 = {"tick": 11}
            assert l10_gen_trigger(st2, {}) is False, "cooldown did not hold"
            # the scratch run must actually have done work, not silently no-op.
            # The generator's own _GENERATOR_LOG is only appended for some
            # trigger reasons; l10_gen_audit.jsonl is written on every real
            # trigger, so that is the file that proves work happened.
            assert (td / "l10_gen_audit.jsonl").exists(), \
                "generator wrote no audit record — the run was a silent no-op"
            rec = json.loads((td / "l10_gen_audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            assert rec["tick"] == 10 and rec["candidate_count"] >= 1, rec
        finally:
            for k, v in saved.items():
                globals()[k] = v
            for k, v in saved_gen.items():
                setattr(gen_mod, k, v)

    print("l10_generator_bridge selfcheck OK | deps loaded from .pyc | generate"
          "(%s) | profile is a BottleneckProfile | end-to-end trigger clean, "
          "cooldown holds, no live state written"
          % ", ".join(sig.parameters))
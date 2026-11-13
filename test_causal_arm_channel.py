"""Contract tests for the 2026-11-10 L2 intervention channel.

Why this file exists: the closure needs exactly ONE restart to land, and a restart
costs ~2 minutes of a 10M-neuron boot. So the two pieces that cannot be exercised
while the old generation is still serving get pinned down here instead:

  1. `_apply_evolved_params()` must publish `type_dominance_cap` under the key the
     live consumers actually read (RESTING_STATE["type_dominance_cap"], no
     `evolved_` prefix -- see tl_ego_server.py:4376 / 12127 / 12167). If it wrote
     `evolved_type_dominance_cap` the A/B would again measure an unperturbed
     system, which is the exact failure this channel exists to remove.
  2. The legacy evolved_* mirrors must keep working (self-model evolution relies
     on them).
  3. `/causal-arm` must refuse keys outside the self-model table: the RCT gate
     only accepts evidence for parameters it can roll back and roll forward, so
     an off-table write would produce unimpeachable-looking evidence that can
     never be adopted.
  4. `dynamics_health()` must be the fitness the sweep optimises, so its shape is
     a contract too: competition past 0.85 is an attractor collapse and has to
     cost score, and concentration is a target band at 0.25, not a maximum.

Run: H:/unsloth_env/Scripts/python.exe test_causal_arm_channel.py
"""
import ast
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "tl_ego_server.py"
SRC = SERVER.read_text(encoding="utf-8")


def load_function(name, namespace=None):
    """Same AST extraction the other server tests use: one function, no import."""
    tree = ast.parse(SRC)
    function = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    scope = dict(namespace or {})
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SERVER), "exec"), scope)
    return scope[name]


def table():
    """Pull the literal SELF_MODEL_EVOLUTION dict out of the source."""
    tree = ast.parse(SRC)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(t, "id", None) == "SELF_MODEL_EVOLUTION" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("SELF_MODEL_EVOLUTION literal not found")


def _params(current=None):
    """A minimal self-model snapshot; only the keys _apply_evolved_params reads."""
    return {
        "diversity_n": {"current": 8},
        "noise_amp_boost": {"current": 0.0},
        "evidence_persist_ticks": {"current": 10},
        "alpha_seed_mix": {"current": 0.5},
        "newborn_inject_n": {"current": 1},
        "action_heater_ratio": {"current": 1.0},
        "type_dominance_cap": {"current": 0.35 if current is None else current},
    }


class ApplyEvolvedParamsTests(unittest.TestCase):
    def _run(self, current):
        state = {}
        apply_ = load_function("_apply_evolved_params", {
            "SELF_MODEL_EVOLUTION": {"parameters": _params(current)},
            "RESTING_STATE": state,
            "print": lambda *a, **k: None,
        })
        apply_()
        return state

    def test_dominance_cap_lands_on_the_key_the_consumers_read(self):
        state = self._run(0.42)
        self.assertEqual(state.get("type_dominance_cap"), 0.42)
        self.assertNotIn("evolved_type_dominance_cap", state,
                         "the live consumers do not read an evolved_ prefix for this key")

    def test_legacy_evolved_mirrors_still_work(self):
        state = self._run(0.35)
        self.assertEqual(state["evolved_diversity_n"], 8)
        self.assertEqual(state["evolved_noise_amp_boost"], 0.0)
        self.assertEqual(state["evidence_persist_ticks"], 10)
        self.assertEqual(state["evolved_alpha"], 0.5)
        self.assertEqual(state["evolved_newborn_inject_n"], 1)
        self.assertEqual(state["evolved_action_heater_ratio"], 1.0)

    def test_cap_is_rounded_like_every_other_published_value(self):
        state = self._run(0.123456789)
        self.assertEqual(state["type_dominance_cap"], 0.1235)

    def test_missing_cap_entry_does_not_break_the_other_mirrors(self):
        p = _params()
        del p["type_dominance_cap"]
        state = {}
        apply_ = load_function("_apply_evolved_params", {
            "SELF_MODEL_EVOLUTION": {"parameters": p},
            "RESTING_STATE": state,
            "print": lambda *a, **k: None,
        })
        apply_()
        self.assertEqual(state.get("type_dominance_cap"), 0.0)
        self.assertEqual(state["evolved_diversity_n"], 8)


class CausalArmContractTests(unittest.TestCase):
    def setUp(self):
        start = SRC.index('elif path == "/causal-arm":')
        end = SRC.index('elif path == "/save-checkpoint":')
        self.block = SRC[start:end]

    def test_refuses_parameters_outside_the_self_model_table(self):
        self.assertIn("not_in_self_model_table", self.block)
        self.assertIn('if _k not in _sp:', self.block)

    def test_read_probe_never_writes(self):
        # a same-value write would clamp a guessed out-of-range probe onto a bound
        i_read = self.block.index('if body.get("read"):')
        i_write = self.block.index("_p[\"current\"] = _v")
        self.assertLess(i_read, i_write)
        self.assertIn("return", self.block[i_read:i_write])

    def test_value_is_clamped_to_the_declared_range(self):
        self.assertIn('_v = max(float(_p["min"]), min(float(_p["max"]), _v))', self.block)

    def test_write_is_published_immediately_not_on_the_next_evolve_tick(self):
        i_write = self.block.index("_p[\"current\"] = _v")
        i_apply = self.block.index("_apply_evolved_params()", i_write)
        self.assertLess(i_write, i_apply)

    def test_cap_is_in_the_table_so_the_gate_can_adopt_it(self):
        cap = table()["parameters"]["type_dominance_cap"]
        self.assertEqual((cap["min"], cap["max"]), (0.0, 0.6))
        # the gate only keeps effects it could also roll back
        self.assertLessEqual(cap["default"], cap["max"])
        self.assertGreaterEqual(cap["default"], cap["min"])


class DynamicsHealthTests(unittest.TestCase):
    """The fitness the sweep maximises. Bad shapes must cost score."""

    def setUp(self):
        sys.path.insert(0, str(HERE))
        import causal_param_sweep as sweep
        self.h = sweep.dynamics_health

    @staticmethod
    def st(conc, comp, stagn=0, trans=0.5):
        return {
            "resting_concentration": conc,
            "resting_competition": comp,
            "stagnation_streak": stagn,
            "last_neural_snapshot": {"transition_rate_8": trans},
        }

    def test_attractor_collapse_scores_worse_than_a_healthy_mix(self):
        self.assertGreater(self.h(self.st(0.25, 0.80)), self.h(self.st(0.25, 0.99)))

    def test_concentration_is_a_band_not_a_maximum(self):
        self.assertGreater(self.h(self.st(0.25, 0.80)), self.h(self.st(0.55, 0.80)))

    def test_locked_switching_costs_score(self):
        self.assertGreater(self.h(self.st(0.25, 0.80, trans=0.5)),
                           self.h(self.st(0.25, 0.80, trans=0.0)))

    def test_missing_fields_abstain_instead_of_scoring(self):
        self.assertIsNone(self.h({"resting_concentration": None, "resting_competition": 0.8}))
        self.assertIsNone(self.h({"resting_concentration": 0.25}))


class SweepPolicyTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HERE))
        import causal_param_sweep as sweep
        self.sweep = sweep

    def test_every_candidate_is_a_real_table_parameter(self):
        params = table()["parameters"]
        for name in self.sweep.CANDIDATES:
            self.assertIn(name, params,
                          f"{name} is not in SELF_MODEL_EVOLUTION -> the gate would drop its evidence")

    def test_allocation_shrinks_on_evidence_and_widens_on_abstention(self):
        pol = self._fresh()
        self.sweep.update_policy(pol, "type_dominance_cap", {"eligible": True, "effect_size": 0.2})
        shrunk = pol["delta_scale"]
        self.assertLess(shrunk, 1.0)
        for _ in range(4):
            self.sweep.update_policy(pol, "alpha_seed_mix", {"eligible": False, "effect_size": 0.01})
        self.assertGreater(pol["delta_scale"], shrunk)

    def _fresh(self):
        pol = {"version": 1, "delta_scale": 1.0, "params": {}}
        for name in self.sweep.CANDIDATES:
            pol["params"][name] = {"trials": 0, "eligible": 0, "prior": 1.0, "direction": 1}
        return pol

    def test_an_unperturbable_key_is_not_mistaken_for_a_useless_one(self):
        """abstain 只轻降权；'测了但不显著'才重降权。混淆两者会让元层永久
        放弃一个 apply 打不进去、但其实有效的参数。"""
        pol = self._fresh()
        pol["params"]["noise_amp_boost"]["prior"] = 1.0
        self.sweep.update_policy(pol, "noise_amp_boost", None)
        abstained = pol["params"]["noise_amp_boost"]["prior"]
        self.assertGreater(abstained, 0.8)

        pol2 = self._fresh()
        pol2["params"]["noise_amp_boost"]["prior"] = 1.0
        self.sweep.update_policy(pol2, "noise_amp_boost", {"eligible": False, "effect_size": 0.0})
        self.assertLess(pol2["params"]["noise_amp_boost"]["prior"], abstained)

    def test_abstention_does_not_move_the_step_size(self):
        pol = self._fresh()
        self.sweep.update_policy(pol, "alpha_seed_mix", None)
        self.assertEqual(pol["delta_scale"], 1.0)

    def test_a_refused_run_is_not_charged_to_the_parameter(self):
        pol = self._fresh()
        before = dict(pol["params"]["type_dominance_cap"])
        self.sweep.update_policy(pol, "type_dominance_cap",
                                 {"ok": False, "reason": "gate_observing"})
        p = pol["params"]["type_dominance_cap"]
        self.assertEqual(p["prior"], before["prior"])
        self.assertEqual(p.get("trials", 0), 0)
        self.assertEqual(p["refused"], 1)
        self.assertEqual(pol["delta_scale"], 1.0)


class ArmApplyRoutingTests(unittest.TestCase):
    """Which endpoint actually receives the intervention. Getting this wrong is
    how the earlier A/Bs ended up measuring a system nobody had perturbed."""

    def setUp(self):
        sys.path.insert(0, str(HERE))
        import causal_param_sweep as sweep
        self.sweep = sweep
        self.calls = []
        self._orig = sweep._post

    def tearDown(self):
        self.sweep._post = self._orig

    def _install(self, behaviour):
        def fake_post(path, body, **kw):
            self.calls.append(path)
            return behaviour(path, body)
        self.sweep._post = fake_post

    def test_missing_endpoint_falls_back_to_the_legacy_channel(self):
        import urllib.error

        def behave(path, body):
            if path == "/causal-arm":
                raise urllib.error.HTTPError(path, 404, "not found", None, None)
            return {"status": "ok"}
        self._install(behave)
        r = self.sweep._arm_apply("type_dominance_cap", 0.4, "t", "operator_causal_sweep")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.calls, ["/causal-arm", "/apply-parameter"])

    def test_off_table_refusal_does_not_silently_use_the_legacy_channel(self):
        import urllib.error

        def behave(path, body):
            if path == "/causal-arm":
                raise urllib.error.HTTPError(path, 403, "not_in_self_model_table", None, None)
            return {"status": "ok"}
        self._install(behave)
        with self.assertRaises(urllib.error.HTTPError):
            self.sweep._arm_apply("something_else", 0.4, "t", "operator_causal_sweep")
        self.assertEqual(self.calls, ["/causal-arm"],
                         "a 403 must not fall through to /apply-parameter")

    def test_native_channel_is_used_when_available(self):
        self._install(lambda path, body: {"status": "ok", "new_value": body["value"]})
        self.sweep._arm_apply("alpha_seed_mix", 0.65, "t", "operator_causal_sweep")
        self.assertEqual(self.calls, ["/causal-arm"])


class ArmRunnerGuardTests(unittest.TestCase):
    """The clamp guard must not abstain the control arm. It did once: every
    control cell returned None, the trial finished with n_control=0, and the
    meta-policy then widened delta_scale on the strength of a measurement that
    never happened."""

    def setUp(self):
        sys.path.insert(0, str(HERE))
        import causal_param_sweep as sweep
        self.sweep = sweep
        self._saved = {k: getattr(sweep, k) for k in
                       ("C", "_gate_busy", "_arm_apply", "_get_status", "_wait_next_tick", "_runtime_value", "_log")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.sweep, k, v)

    def _run(self, intervention_value):
        sweep = self.sweep
        outcomes = []

        class FakeAb:
            @staticmethod
            def run_ab(workspace, param, delta, n_arm, seed, runtime_generation_id,
                       control_value, outcome_metric, arm_runner):
                for arm in ("control", "intervention"):
                    value = control_value if arm == "control" else intervention_value
                    outcomes.append((arm, arm_runner(arm, value, 0)))
                return {"n_control": 1, "n_intervention": 1, "effect_size": 0.1,
                        "effect_ci95": [0.0, 0.2], "eligible": False}

            @staticmethod
            def emit_intervention_evidence(res):
                return None

        st = {"resting_ticks": 1, "resting_concentration": 0.25, "uptime_s": 600,
              "runtime_generation_id": "test-gen",
              "resting_competition": 0.80, "stagnation_streak": 0,
              "last_neural_snapshot": {"transition_rate_8": 0.5}}
        sweep.C = FakeAb
        sweep._gate_busy = lambda: None
        sweep._runtime_value = lambda key, allow_probe=True: 10.0
        # run_one writes to the real .lae ledger; a unit test must not.
        sweep._log = lambda row: None
        sweep._arm_apply = lambda key, value, reason, source: {"status": "ok", "new_value": value}
        sweep._get_status = lambda **kw: dict(st)
        sweep._wait_next_tick = lambda tick0, timeout=180, poll=3: dict(st, resting_ticks=2)
        sweep.run_one("evidence_persist_ticks", delta=intervention_value - 10.0, n_arm=1)
        return outcomes

    def test_control_arm_is_measured_not_abstained(self):
        outcomes = dict(self._run(20.0))
        self.assertIsNotNone(outcomes["control"], "control arm must never abstain for equalling control")
        self.assertIsNotNone(outcomes["intervention"])

    def test_a_clamped_back_intervention_does_abstain(self):
        outcomes = dict(self._run(10.0))
        self.assertIsNotNone(outcomes["control"])
        self.assertIsNone(outcomes["intervention"],
                          "intervention == control is not an intervention")


class SelfModelRestoreTests(unittest.TestCase):
    """A parameter declared in source must survive boot. Both restore paths had
    to be fixed: lae_self_model.json and ego_extras.json each replaced the whole
    "parameters" table, and the second one ran last."""

    def _merge(self, restored, declared):
        scope = {"SELF_MODEL_EVOLUTION": {"parameters": declared},
                 "print": lambda *a, **k: None}
        fn = load_function("_merge_self_model_evolution", scope)
        kept = fn(restored, "")
        return scope["SELF_MODEL_EVOLUTION"], kept

    def test_declared_only_parameter_survives_a_checkpoint_restore(self):
        declared = {"a": {"current": 1}, "new_key": {"current": 0.35, "min": 0.0, "max": 0.6}}
        restored = {"parameters": {"a": {"current": 7}, "b": {"current": 2}},
                    "evolution_log": [{"x": 1}]}
        state, kept = self._merge(restored, declared)
        self.assertIn("new_key", state["parameters"])
        self.assertEqual(kept, ["new_key"])
        self.assertEqual(state["parameters"]["new_key"]["current"], 0.35)
        self.assertEqual(state["parameters"]["a"]["current"], 7, "checkpoint still wins for shared keys")
        self.assertEqual(state["parameters"]["b"]["current"], 2, "checkpoint-only keys are kept")
        self.assertEqual(state["evolution_log"], [{"x": 1}])

    def test_extra_restore_pass_is_idempotent_and_does_not_drop_the_key(self):
        declared = {"new_key": {"current": 0.35}}
        restored = {"parameters": {"a": {"current": 7}}}
        state, _ = self._merge(restored, declared)
        # second (ego_extras) pass over the already-merged table
        scope = {"SELF_MODEL_EVOLUTION": state, "print": lambda *a, **k: None}
        fn = load_function("_merge_self_model_evolution", scope)
        fn({"parameters": {"a": {"current": 9}}}, "")
        self.assertIn("new_key", scope["SELF_MODEL_EVOLUTION"]["parameters"])
        self.assertEqual(scope["SELF_MODEL_EVOLUTION"]["parameters"]["a"]["current"], 9)

    def test_non_dict_checkpoint_is_ignored(self):
        declared = {"new_key": {"current": 0.35}}
        state, kept = self._merge(None, declared)
        self.assertEqual(kept, [])
        self.assertIn("new_key", state["parameters"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

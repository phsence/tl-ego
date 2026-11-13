import ast
import math
import unittest
from pathlib import Path

SERVER = Path(__file__).with_name("tl_ego_server.py")


def load_function(name, namespace=None):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    scope = {"math": math, **(namespace or {})}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SERVER), "exec"), scope)
    return scope[name]


class CausalAttributionTests(unittest.TestCase):
    def test_only_pairs_consecutive_same_generation_ticks_after_resume(self):
        build = load_function("_causal_lagged_observations")
        trajectory = [
            {"tick": 9, "runtime_generation_id": "g", "dominant": "old"},
            {"tick": 10, "runtime_generation_id": "g", "dominant": "doubt", "concentration": 0.2, "competition": 0.8},
            {"tick": 11, "runtime_generation_id": "g", "predictive_plasticity": {"last_outcome": {"predicted": "doubt", "actual": "doubt", "prediction_error": 0.0}}},
            {"tick": 12, "runtime_generation_id": "other", "dominant": "stale"},
            {"tick": 13, "runtime_generation_id": "g", "dominant": "gap"},
            {"tick": 15, "runtime_generation_id": "g", "surprise": {"surprise_score": 0.9}},
        ]
        rows = build(trajectory, "g", resumed_tick=10, last_tick=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tick"], 10)
        self.assertEqual(rows[0]["factors"]["dominant"], "doubt")
        self.assertEqual(rows[0]["outcome"], 1.0)
        self.assertEqual(rows[0]["evidence_type"], "lagged_observational_association")
        malformed = [trajectory[1], {"tick": 11, "runtime_generation_id": "g", "predictive_plasticity": {"last_outcome": {"predicted": "doubt", "actual": "architect", "prediction_error": 0.0}}}]
        self.assertEqual(build(malformed, "g"), [])

    def test_builder_discards_restored_other_generation_observations(self):
        build_pairs = load_function("_causal_lagged_observations")
        state = {
            "tick": 20,
            "trajectory": [
                {"tick": 19, "runtime_generation_id": "g-new", "dominant": "doubt", "noise_mode": "pink"},
                {"tick": 20, "runtime_generation_id": "g-new", "dominant": "architect",
                 "predictive_plasticity": {"last_outcome": {"predicted": "doubt", "actual": "architect", "prediction_error": 1.0}}},
            ],
            "continuity": {"resumed_tick": 19},
        }
        attribution = {
            "runtime_generation_id": "g-old", "last_tick": 800,
            "observations": [{"tick": 800, "runtime_generation_id": "g-old", "outcome": 1.0, "factors": {}}],
            "total_observations": 5000, "attribution_confidence": 1.0,
        }
        scope = {
            "RESTING_STATE": state, "RUNTIME_GENERATION_ID": "g-new",
            "CAUSAL_ATTRIBUTION": attribution, "_causal_lagged_observations": build_pairs,
            "math": math,
        }
        load_function("_build_causal_attribution", scope)()
        self.assertEqual(attribution["runtime_generation_id"], "g-new")
        self.assertEqual(attribution["total_observations"], 1)
        self.assertEqual(attribution["association_n"], 1)
        self.assertEqual(attribution["attribution_confidence"], 0.0)
        self.assertEqual(attribution["observations"][0]["tick"], 19)

    def test_verified_parameter_effect_flows_through_evolution(self):
        params = {"diversity_n": {"current": 5.0, "default": 5.0, "min": 1.0, "max": 10.0, "history": []}}
        evolution = {
            "enabled": True, "consecutive_worse": 0, "rollback_window": 3,
            "parameters": params, "evolution_log": [], "last_tick": 0, "narrative": "",
            "param_delta_max": 0.05, "param_delta_min": 0.005,
        }
        applied = []
        scope = {
            "SELF_MODEL_EVOLUTION": evolution,
            "RESTING_STATE": {"tick": 100},
            "_validated_causal_parameter_effects": lambda *args: [{
                "parameter": "diversity_n", "recommended_value": 6.0, "ledger_ref": "R1",
                "effect_size": 0.2, "effect_ci95": [0.05, 0.3],
            }],
            "_apply_evolved_params": lambda: applied.append(True),
            "CAUSAL_ATTRIBUTION": {"evidence_type": "verified_randomized_intervention"},
            "RUNTIME_GENERATION_ID": "g",
        }
        load_function("_evolve_self_model", scope)()
        self.assertEqual(params["diversity_n"]["current"], 6.0)
        self.assertIn("R1", params["diversity_n"]["history"][-1]["reason"])
        self.assertEqual(applied, [True])

    def test_intervention_evidence_requires_real_parameter_effect(self):
        load_effects = load_function("_validated_causal_parameter_effects")
        parameters = {"diversity_n": {"current": 5.0, "min": 1.0, "max": 10.0}}
        causal = {"intervention_evidence": {
            "status": "verified", "design": "randomized_matched_control",
            "runtime_generation_id": "g", "preregistered": True, "outcome_readback": True,
            "higher_is_better": True, "ledger_ref": "test-ledger",
            "intervention_n": 10, "control_n": 10,
            "parameter_effects": [{"parameter": "diversity_n", "control_value": 5,
                "intervention_value": 6, "effect_size": 0.2, "effect_ci95": [0.05, 0.3]}],
        }}
        effects = load_effects(causal, "g", parameters)
        self.assertEqual(effects[0]["recommended_value"], 6.0)
        causal["intervention_evidence"]["parameter_effects"][0]["effect_ci95"] = [-0.1, 0.3]
        self.assertEqual(load_effects(causal, "g", parameters), [])

    def test_self_model_evolution_does_not_apply_observational_associations(self):
        params = {"diversity_n": {"current": 5.0, "default": 5.0, "min": 1.0, "max": 10.0, "history": []}}
        evolution = {
            "enabled": True, "consecutive_worse": 0, "rollback_window": 3,
            "parameters": params, "top_positive_factors": [], "top_negative_factors": [],
            "evolution_log": [], "last_tick": 0, "narrative": "",
            "param_delta_max": 0.05, "param_delta_min": 0.005,
        }
        scope = {
            "SELF_MODEL_EVOLUTION": evolution,
            "RESTING_STATE": {"tick": 100},
            "INTRINSIC_MOTIVATION": {},
            "_validated_causal_parameter_effects": lambda *args: [],
            "_apply_evolved_params": lambda: None,
            "CAUSAL_ATTRIBUTION": {
                "evidence_type": "lagged_observational_association",
                "attribution_confidence": 1.0,
                "top_negative_factors": [{"factor": "competition", "correlation": -0.9}],
            },
            "RUNTIME_GENERATION_ID": "g",
        }

        load_function("_evolve_self_model", scope)()
        self.assertEqual(params["diversity_n"]["current"], 5.0)
        self.assertEqual(params["diversity_n"]["history"], [])
        self.assertEqual(evolution["last_tick"], 0)


if __name__ == "__main__":
    unittest.main()

import unittest

from causal_lens_bridge import build_ego_payload, extract_causal_gap, format_causal_context


class CausalLensBridgeTests(unittest.TestCase):
    def test_payload_uses_live_ego_adapter_shape(self):
        payload = build_ego_payload(
            self_model={"energy_level": 0.4, "mood": "focused"},
            state={
                "dominant": "doubt",
                "raw_attractor_dominant": "architect",
                "competition": 0.81,
                "concentration": 0.32,
                "stagnation_streak": 2,
                "prediction_error": 0.75,
                "persistent_identity": {"dominant_type": "action"},
            },
            trajectory=[{"tick": 42, "dominant": "doubt"}],
            tick=42,
            runtime_generation_id="gen-test",
            timestamp="2026-10-14T20:00:00+08:00",
        )
        self.assertEqual(payload["adapter"], "ego")
        self.assertEqual(payload["self_model"]["dominant"], "doubt")
        self.assertEqual(payload["self_model"]["raw_attractor_dominant"], "architect")
        self.assertEqual(payload["self_model"]["competition"], 0.81)
        self.assertEqual(payload["self_model"]["prediction_error"], 0.75)
        self.assertEqual(payload["raw_trajectory"], [{"tick": 42, "dominant": "doubt"}])
        self.assertEqual(payload["meta"]["runtime_generation_id"], "gen-test")
        self.assertEqual(payload["meta"]["tick"], 42)

    def test_extracts_current_v2_response_fields(self):
        response = {
            "schema": "causal_lens.v2",
            "request_id": "req-1",
            "support_analysis": {"critical_nodes": [
                {"id": "ego:competition", "label": "competition=0.81", "vulnerability": 0.72}
            ]},
            "causal_chains": [
                {"trigger_label": "competition=0.81", "effect": "dominant", "polarity": "negative", "risk_score": 0.84, "mechanism": "support weakens"}
            ],
            "interventions": [{"target": "ego:competition", "strategy": "reinforce", "priority": 0.84}],
            "risk_score": 0.84,
        }
        gap = extract_causal_gap(response, runtime_generation_id="gen-test", tick=42)
        self.assertEqual(gap["critical_node"]["id"], "ego:competition")
        self.assertEqual(gap["structural_vulnerability_proxy"], 0.72)
        self.assertEqual(gap["threat_count"], 1)
        self.assertEqual(gap["chain_summary"], "support weakens")
        self.assertEqual(gap["intervention"]["strategy"], "reinforce")
        self.assertEqual(gap["risk_score"], 0.84)
        self.assertEqual(gap["runtime_generation_id"], "gen-test")
        self.assertEqual(gap["tick"], 42)

    def test_extracts_legacy_response_and_returns_empty_for_no_findings(self):
        response = {"causal_lens": {
            "support_analysis": {"critical_nodes": []},
            "chains": {"chains": [
                {"trigger": "x", "effect": "y", "polarity": "negative", "risk_score": 0.6}
            ]},
            "interventions": {"top_intervention": {"target": "x", "strategy": "stabilize"}},
        }}
        gap = extract_causal_gap(response, runtime_generation_id="gen-old", tick=9)
        self.assertEqual(gap["structural_vulnerability_proxy"], 0.6)
        self.assertEqual(gap["critical_node"]["label"], "x")
        self.assertEqual(gap["intervention"]["strategy"], "stabilize")
        self.assertEqual(extract_causal_gap({}, runtime_generation_id="gen-old", tick=9), {})

    def test_context_labels_analysis_as_a_hypothesis_and_rejects_stale_generation(self):
        gap = {
            "runtime_generation_id": "gen-test", "tick": 42,
            "critical_node": {"label": "competition"},
            "threat_count": 1, "risk_score": 0.84, "structural_vulnerability_proxy": 0.72,
            "intervention": {"strategy": "reinforce"},
        }
        context = format_causal_context(gap, "gen-test", tick=44)
        self.assertIn("hypothesis", context.lower())
        self.assertIn("structural_vulnerability_proxy", context)
        self.assertIn("competition", context)
        self.assertEqual(format_causal_context(gap, "other-generation", tick=44), "")
        self.assertEqual(format_causal_context(gap, "gen-test", tick=48), "")


if __name__ == "__main__":
    unittest.main()

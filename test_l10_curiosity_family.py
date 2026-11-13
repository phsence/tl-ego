"""Curiosity recovery is proposed only when live observer evidence marks it dead."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "l10"))
import l10_meta_algorithm_generator as G  # noqa: E402


def run():
    gen = G.MetaAlgorithmGenerator()
    pair = G.DissensusPair(
        s1_label="deepening_existing", s1_approach="current pool family",
        s2_label="dead-type rescue", s2_approach="target a measured dead type",
        score=0.8, rationale="observer identifies a dead type",
    )
    dead = G.BottleneckProfile(dead_type_count=10, dead_types=["curiosity", "meta"])
    alive = G.BottleneckProfile(dead_type_count=2, dead_types=["meta"])

    dead_candidates = gen._draft_algorithm_candidates(pair, dead)
    recovery = [c for c in dead_candidates
                if (c.get("strategy") or {}).get("type") == "curiosity_activation_recovery"]
    assert len(recovery) == 1, dead_candidates
    s = recovery[0]["strategy"]
    assert s["target"] == "curiosity_activation_boost", s
    assert s["shadow_only"] is True, s
    assert s["shadow_param"] == {"key": "curiosity_activation_boost", "value": 2.5}, s
    assert "curiosity_activation_boost" in s["s2_value"] and "2.5" in s["s2_value"], s
    assert s["evidence"]["dead_type"] == "curiosity", s

    alive_candidates = gen._draft_algorithm_candidates(pair, alive)
    assert all((c.get("strategy") or {}).get("type") != "curiosity_activation_recovery"
               for c in alive_candidates), alive_candidates
    print("OK: curiosity family is emitted iff observer marks curiosity dead; value and evidence are explicit")


if __name__ == "__main__":
    run()

"""knowledge_upgrade wiring (2026-10-17): menu entry + rotation LRU + prompt.

Run: python tests/test_knowledge_upgrade_wired.py
Fails if: the entry is missing, the rotation stops picking it when least-recently
issued (or monopolizes), or the prompt placeholders drift.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tools_registry as trg


def main():
    cfg = trg.TOOLS["knowledge_upgrade"]
    assert cfg["goal_type"] == "knowledge_upgrade"
    assert ("knowledge_upgrade", "knowledge_upgrade") in trg.COGNITIVE_DISPATCH_ROTATION
    assert "knowledge_upgrade" in trg._COGNITIVE_DIVERSITY_ELIGIBLE

    # rotation must pick it when it is the only never-issued goal type
    state = {"last_goal_timestamps": {gt: 100 for _, gt in trg.COGNITIVE_DISPATCH_ROTATION
                                      if gt != "knowledge_upgrade"}}
    pick = trg.select_cognitive_diversity_tool(state, "neuron_memory_recall")
    assert pick and pick[0] == "knowledge_upgrade", pick

    # ... and must move on once it has a newer timestamp (no monopolizing)
    state["last_goal_timestamps"]["knowledge_upgrade"] = 900
    state["last_goal_timestamps"]["sensory_explore"] = 50
    pick2 = trg.select_cognitive_diversity_tool(state, "neuron_memory_recall")
    assert pick2 and pick2[0] == "neuron_sensory_explore", pick2

    # prompt placeholders must resolve with the standard format kwargs
    txt = cfg["prompt_template"].format(dominant="action", competition=0.9, concentration=0.1,
                                        stagnation=0, tick=1)
    assert "apply-parameter" in txt and "next_impulse_hint" in txt and "LIVE-CONSUMER" in txt
    assert "knowledge_upgrade:" in txt

    print("OK: knowledge_upgrade wired (entry + rotation LRU + prompt)")


if __name__ == "__main__":
    main()

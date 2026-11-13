"""Contract check for the L8 repeat gate (l8_cognitive_lab.repeat_of_previous).

Run: python test_l8_repeat_guard.py
"""
import json
import tempfile
from pathlib import Path

from l8_cognitive_lab import repeat_of_previous, record_l8_signature


def _sig(**kw):
    base = {"label": "action_dominant_stag_0", "dominant": "action",
            "competition": 0.7729, "concentration": 0.1406, "stagnation": 0,
            "tick": 65409, "ts": "2026-11-03 08:16:40"}
    base.update(kw)
    return base


def demo() -> dict:
    tmp = Path(tempfile.mkdtemp()) / "l8_last_proposal.json"

    # 1. no history -> never a repeat
    assert repeat_of_previous(tmp, _sig()) is False
    assert not tmp.exists(), "read path must not create the file"

    # 2. first real proposal is recorded
    record_l8_signature(tmp, _sig())
    assert json.loads(tmp.read_text("utf-8"))["label"] == "action_dominant_stag_0"

    # 3. same state, new tick (the reboot replay case) -> repeat
    assert repeat_of_previous(tmp, _sig(tick=65410, ts="2026-11-03 09:00:00")) is True

    # 4. the state that actually repeated on 2026-11-03 (comp .7836 conc .1230) -> repeat
    assert repeat_of_previous(tmp, _sig(competition=0.7836, concentration=0.1230)) is True

    # 5. real drift -> allowed through
    assert repeat_of_previous(tmp, _sig(competition=0.95)) is False
    assert repeat_of_previous(tmp, _sig(concentration=0.40)) is False
    assert repeat_of_previous(tmp, _sig(stagnation=3)) is False
    assert repeat_of_previous(tmp, _sig(dominant="doubt")) is False
    assert repeat_of_previous(tmp, _sig(label="doubt_dominant_stag_2")) is False

    # 6. a skip bumps the counter but keeps the real baseline for later comparison
    record_l8_signature(tmp, _sig(tick=65411), skipped=True)
    after_skip = json.loads(tmp.read_text("utf-8"))
    assert after_skip["skipped_n"] == 1
    assert after_skip["tick"] == 65409, "skip must not move the baseline"
    assert repeat_of_previous(tmp, _sig()) is True

    # 7. a real proposal resets the baseline, keeps the counter
    record_l8_signature(tmp, _sig(competition=0.95, tick=65420))
    fresh = json.loads(tmp.read_text("utf-8"))
    assert fresh["tick"] == 65420 and fresh["skipped_n"] == 1
    assert repeat_of_previous(tmp, _sig()) is False

    # 8. corrupt file degrades to "not a repeat" instead of raising
    tmp.write_text("{truncated", "utf-8")
    assert repeat_of_previous(tmp, _sig()) is False

    return {"checks": 12}


if __name__ == "__main__":
    print("L8 repeat guard OK:", demo())

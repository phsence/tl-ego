"""Check for the saturation-gate repair (2026-11-10).

window_eligible() must fire a new-family window ONLY on real change:
  (A) archive converged (frozen since last window) AND the candidate's decision
      (budget = max(6, 10-dead_type_count)) would differ -> eligible
  otherwise -> cooldown with the reason spelled out (no guessing).
The 72h backstop stays in main() (untouched) and is not re-implemented here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "l10"))
import l10_draft_algorithm_candidate as drafter  # noqa: E402

FROZEN = (33, 0.999, "2026-10-07 16:31:21")


def _patch(draft_rows, stats, dead_now):
    drafter._iter_records_orig = drafter._iter_records

    def fake_iter(path):
        if path == drafter.DRAFT_LOG:
            yield from draft_rows
        elif path == drafter.GEN_LOG:
            yield {"triggered": True, "profile": {"dead_type_count": dead_now}}

    drafter._iter_records = fake_iter
    drafter._archive_stats = lambda: stats


def run():
    # 1. no previous window -> eligible (first-window behaviour preserved)
    _patch([], FROZEN, 14)
    ok, why = drafter.window_eligible()
    assert ok and "no_previous_window" in why, (ok, why)

    # 2. last window has no baseline (old audit format) -> cooldown, backstop only
    _patch([{"ts": 1.0, "fired": True}], FROZEN, 14)
    ok, why = drafter.window_eligible()
    assert not ok and "no convergence baseline" in why, (ok, why)

    # 3. archive still moving (new cell or better fitness) -> cooldown
    _patch([{"ts": 1.0, "fired": True, "archive_stats": [33, 0.9, "2026-10-07 16:31:21"],
             "dead_type_count": 15}], FROZEN, 14)
    ok, why = drafter.window_eligible()
    assert not ok and "still moving" in why, (ok, why)

    # 4. converged, but budget identical (dead 15 -> 14, both clamp to 6) -> cooldown
    _patch([{"ts": 1.0, "fired": True, "archive_stats": list(FROZEN),
             "dead_type_count": 15}], FROZEN, 14)
    ok, why = drafter.window_eligible()
    assert not ok and "candidate unchanged" in why, (ok, why)

    # 5. converged AND decision moves (dead drops below 4 -> budget 6 -> 7) -> eligible
    _patch([{"ts": 1.0, "fired": True, "archive_stats": list(FROZEN),
             "dead_type_count": 14}], FROZEN, 3)
    ok, why = drafter.window_eligible()
    assert ok and "decision changed" in why and "budget -> 7" in why, (ok, why)

    # 6. archive unreadable -> cooldown (never guess)
    _patch([{"ts": 1.0, "fired": True, "archive_stats": list(FROZEN),
             "dead_type_count": 14}], None, 3)
    ok, why = drafter.window_eligible()
    assert not ok and "no convergence baseline" in why, (ok, why)

    print("OK: window fires only on real change; cooldown reasons are explicit")


if __name__ == "__main__":
    run()

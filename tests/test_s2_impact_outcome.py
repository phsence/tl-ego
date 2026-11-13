#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks for the 2026-11-08 P1 fix: close S2 teacher rows from measured impacts.

Measured before the fix: 160/160 of today's teacher decision_ids had no
counterpart in action_impact.jsonl (the single-slot _last_p5_job went stale), and
no producer posted /feedback for goal results, so 88% of rows expired.

Run: python tests/test_s2_impact_outcome.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    # Import the server module's helper without booting the whole service: it is a
    # pure function, but importing tl_ego_server starts the service, so copy the
    # contract check against the source instead.
    src = (Path(__file__).resolve().parents[1] / "tl_ego_server.py").read_text(encoding="utf-8", errors="replace")
    assert "def _s2_outcome_from_impact(score):" in src, "helper missing"
    assert '_outcome = _s2_outcome_from_impact(_scf)' in src, "impact loop must use the helper"
    assert "RESTING_STATE[\"_last_p5_job\"] = {" in src, "teacher writer must refresh _last_p5_job"

    ns = {}
    start = src.index("def _s2_outcome_from_impact(score):")
    end = src.index("def _complete_s2_teacher_sample(", start)
    exec(compile(src[start:end], "<helper>", "exec"), ns)
    f = ns["_s2_outcome_from_impact"]

    # thresholds measured over 23,474 impacts: median 0.20, p75 0.50
    assert f(1.0) == "success"
    assert f(0.5) == "success"
    assert f(0.49) == "partial"
    assert f(0.2) == "partial"
    assert f(0.19) == "failed"
    assert f(0.0) == "failed"
    assert f(None) == "failed"
    assert f("0.7") == "success"
    assert f("junk") == "failed"
    print("OK: s2 impact outcome checks passed")


if __name__ == "__main__":
    main()

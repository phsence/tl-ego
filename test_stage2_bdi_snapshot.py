#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for Titan Stage 2 BDI snapshot."""

import tempfile
from pathlib import Path

from stage2_homeostatic_drives import build_bdi_snapshot, build_l5_priority_ranking, save_drives


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        save_drives(workspace, {
            "drives": {
                "exploration": 0.2,
                "integration": 0.3,
                "novelty": 0.8,
                "stability": 0.1,
            },
            "history": [],
            "last_updated": "2026-06-18 15:01:00",
        })
        snapshot = build_bdi_snapshot(workspace, {
            "last_dominant": "action",
            "concentration": 0.62,
            "competition": 0.82,
            "stagnation_streak": 1,
            "prediction_error": 0.71,
        })
        ranking = build_l5_priority_ranking(workspace, {
            "last_dominant": "action",
            "concentration": 0.62,
            "competition": 0.82,
            "stagnation_streak": 1,
        }, "novelty")

    assert snapshot["status"] == "ok"
    assert snapshot["model"] == "titan_stage2_bdi_snapshot_v1"
    assert snapshot["beliefs"]["dominant_type"] == "action"
    assert snapshot["beliefs"]["dominant_pressure"] == "novelty"
    assert snapshot["pressure_rank"][0] == "novelty"
    assert "prediction error" in snapshot["desire"]
    assert "world-model mismatch" in snapshot["intention"]
    assert ranking["model"] == "titan_stage2_bdi_l5_priority_v1"
    assert ranking["dominant_pressure"] == "novelty"
    assert ranking["goal_pressure_match"] is True
    assert ranking["priority_adjustment"] == 0.144
    print("STAGE2_BDI_SNAPSHOT_OK")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks for the 2026-11-08 jepa_feature_missing P0 fix.

The defect: predicted.next_dominant is the counterfactual TARGET (measured 15%
hit rate), yet it was scored as the world model's forecast. The dominant type is
strongly autocorrelated (2-gram 45.6% over 12,359 rows), so the sequence itself
is the missing feature.

Run: python -m pytest tests/test_jepa_dominant_forecast.py -q
     (or plain: python tests/test_jepa_dominant_forecast.py)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jepa_residual_attributor as ja
import residual_to_defect_classifier as rc


def _write_history(tmp_path: Path):
    """doubt->doubt->doubt x3, sensory->action x2 — table must learn both."""
    rows = []
    seq = ["doubt", "doubt", "doubt", "sensory", "action", "doubt", "doubt", "doubt", "sensory", "action"]
    for i, dom in enumerate(seq):
        rows.append({"tick": i, "actual": {"dominant": dom}, "predicted": {"next_dominant": "integrator"}})
    p = tmp_path / "hist.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def main(tmp_path: Path = None):
    tmp_path = Path(tmp_path or "C:/Users/Administrator/AppData/Local/hermes/cache/scratch")
    hist = _write_history(tmp_path)

    # 1) table learns the transition; forecast follows the 2-gram majority
    ja._2GRAM = None
    assert ja.two_gram_forecast(["doubt", "doubt"], path=hist) == "doubt", "2-gram should repeat doubt"
    ja._2GRAM = None
    assert ja.two_gram_forecast(["doubt", "sensory"], path=hist) == "action", "doubt->sensory should predict action"
    ja._2GRAM = None
    assert ja.two_gram_forecast(["doubt"], path=hist) is not None, "1st-order fallback must answer"

    # 2) empty sequence never invents a type
    ja._2GRAM = None
    assert ja.two_gram_forecast([], path=hist) is None

    # 3) _dominant prefers the forecast over the counterfactual target (both modules)
    rec = {"predicted": {"next_dominant": "integrator", "next_dominant_forecast": "doubt"}, "actual": {"dominant": "doubt"}}
    assert ja._dominant(rec["predicted"]) == "doubt"
    assert rc._dominant(rec, "predicted") == "doubt"
    # old records (no forecast) keep the old behaviour
    old = {"predicted": {"next_dominant": "integrator"}, "actual": {"dominant": "doubt"}}
    assert ja._dominant(old["predicted"]) == "integrator"
    assert rc._dominant(old, "predicted") == "integrator"

    # 4) classify_residual scores the forecast, keeps the aspiration signal
    out = ja.classify_residual(rec["predicted"], rec["actual"], {"recent_dominants": ["doubt", "integrator", "doubt"]})
    assert "type_mismatch" not in out["errors"], f"forecast hit must not be a mismatch: {out['errors']}"
    assert out["errors"].get("aspiration_mismatch", {}).get("predicted") == "integrator", out["errors"]

    # 5) a wrong forecast IS a mismatch and drops the error estimate
    bad = {"next_dominant": "integrator", "next_dominant_forecast": "sensory"}
    out_bad = ja.classify_residual(bad, {"dominant": "doubt"}, {})
    assert out_bad["errors"].get("type_mismatch", {}).get("predicted") == "sensory", out_bad["errors"]
    assert out_bad["prediction_error"] >= 0.35

    ja._2GRAM = None
    print("OK: jepa dominant forecast checks passed")


if __name__ == "__main__":
    main()

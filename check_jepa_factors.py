#!/usr/bin/env python3
"""Guard for l6p-jepa-factor-fix: concentration/competition must not be dead zeros.

Fails if (a) the server stops forwarding persistence predictions into
predicted_state_t1, or (b) the attributor stops scoring those factors.
Run: python check_jepa_factors.py
"""
import re
import sys
from pathlib import Path

from jepa_residual_attributor import classify_residual

HERE = Path(__file__).resolve().parent
ctx = {"tick": 1, "competition": 0.90, "concentration": 0.10, "stagnation": 0,
       "recent_dominants": ["action", "curiosity"]}
actual = {"dominant": "action", "concentration": 0.10, "competition": 0.90,
          "stagnation": 0, "external_novelty": 0.24}

no_pred = {"next_dominant": "integrator", "stagnation_delta": -0.1, "external_novelty": 0.3}
with_pred = dict(no_pred, concentration=0.40, competition=0.60)

a = classify_residual(no_pred, actual, ctx)["errors"]
b = classify_residual(with_pred, actual, ctx)["errors"]

assert a["concentration_error"] == 0.0 and a["competition_error"] == 0.0, a
assert b["concentration_error"] == 0.30, b           # 0.40 - 0.10
assert b["competition_error"] == 0.30, b            # 0.90 - 0.60 (actual - predicted)
assert classify_residual(with_pred, actual, ctx)["prediction_error"] > \
       classify_residual(no_pred, actual, ctx)["prediction_error"]

src = (HERE / "tl_ego_server.py").read_text(encoding="utf-8", errors="replace")
wired = re.search(r"predicted=_pred_for_attribution", src) and \
        re.search(r"_pred_for_attribution\[_factor\] = _prev_v", src)
assert wired, "server no longer feeds persistence predictions into attribution"

print("PASS: factors live; server wiring present")

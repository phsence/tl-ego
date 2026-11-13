#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks for the 2026-11-09 success-rate dispatch gate.

The gate exists because the goal ledger's own recent window predicts the next
outcome 32x better than any (dominant_type, tool_name) feature: P(success |
last-20 rate < 0.20) = 0.023 vs 0.746 above 0.80. It is a STOP rule, so a
wrong threshold silently either stalls the body or lets a dead world burn
thousands of goals -- both need a check.

Run: python tests/test_success_gate.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_gate(tmpdir: Path):
    src = (ROOT / "tl_ego_server.py").read_text(encoding="utf-8", errors="replace")
    start = src.index("SUCCESS_GATE_ENABLED = ")
    end = src.index("def _externalize_autonomy_impulse():")
    ns = {"os": os, "json": json, "time": time, "WORKSPACE": tmpdir}
    exec(compile(src[start:end], "<success-gate>", "exec"), ns)
    return ns


def _write_rows(path: Path, outcomes):
    with open(path, "w", encoding="utf-8") as fh:
        for i, o in enumerate(outcomes):
            fh.write(json.dumps({"goal_id": f"g{i}", "tick": i, "outcome": o}) + "\n")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="gate-"))
    (tmp / ".lae").mkdir()
    ledger = tmp / ".lae" / "l5_goal_results.jsonl"
    ns = _load_gate(tmp)
    rate_fn = ns["_recent_goal_success_rate"]
    ok_fn = ns["_recent_goal_success_ok"]
    state = ns["_SUCCESS_GATE_STATE"]

    def reset():
        state.update({"ts": 0.0, "rate": None, "n": 0, "blocked": 0, "checks": 0,
                      "last_allow": time.time()})

    # 1) all-success window opens the gate
    _write_rows(ledger, ["success"] * 20)
    reset()
    assert rate_fn() == 1.0, rate_fn()
    assert ok_fn() is True

    # 2) all-failed window closes it (last_allow just refreshed -> no probe)
    _write_rows(ledger, ["failed"] * 20)
    reset()
    assert rate_fn() == 0.0, rate_fn()
    assert ok_fn() is False
    assert state["blocked"] == 1

    # 3) probe window re-opens exactly one dispatch
    state["last_allow"] = time.time() - 901
    assert ok_fn() is True
    assert state["blocked"] == 1, "probe must not count as blocked"
    assert ok_fn() is False, "only one probe per window"

    # 4) rate counts success/landed/completed/partial_success, ignores the rest
    _write_rows(ledger, ["success"] * 5 + ["landed"] * 5 + ["partial_success"] * 5 + ["failed"] * 5)
    reset()
    assert abs(rate_fn() - 0.75) < 1e-9, rate_fn()

    # 5) short tail (< window rows) = no opinion, gate stays open
    _write_rows(ledger, ["failed"] * 3)
    reset()
    assert rate_fn() is None
    assert ok_fn() is True

    # 6) missing ledger = no opinion, never a crash
    ledger.unlink()
    reset()
    assert rate_fn() is None
    assert ok_fn() is True

    print("test_success_gate: 6/6 OK")


if __name__ == "__main__":
    sys.exit(main())

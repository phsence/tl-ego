#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks for the 2026-11-08 route fix: text-only P5 answers must not bypass full_pipeline.

Measured before the fix: 9 of 11 contemplates in a 3h window routed "text",
full_pipeline never ran, so step3's external S2 classify never ran and no
teacher row was written for 5.5 hours (file mtime frozen at 15:44).

Run: python tests/test_p5_short_circuit.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    src = (ROOT / "tl_ego_server.py").read_text(encoding="utf-8", errors="replace")
    start = src.index("def _p5_short_circuits(p5):")
    end = src.index("def _bg_run_contemplate_impl(", start)
    ns = {}
    exec(compile(src[start:end], "<helper>", "exec"), ns)
    f = ns["_p5_short_circuits"]

    # text-only answer: pipeline must take over
    assert f({"response": "a lobster is a crustacean", "tool_executions": [], "llm_route": "text"}) is False
    assert f({"response": "", "tool_executions": []}) is False
    # actually did work: keep the bypass
    assert f({"tool_executions": [{"tool": "move_head", "success": True}]}) is True
    assert f({"tool_executions": [], "decision_prompt": True}) is True
    # defensive
    assert f(None) is False
    assert f({}) is False

    assert "_p5_short_circuits(_p5)" in src, "call site missing"
    print("OK: p5 short-circuit checks passed")


if __name__ == "__main__":
    main()

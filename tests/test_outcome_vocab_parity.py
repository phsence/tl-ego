"""Outcome-vocabulary parity check (plugin vs core).

The plugin (extensions/tl-ego/index.js) and the core (projects/tl-ego/tl_ego_server.py)
each keep their own copy of the outcome tier sets. They drift: an unmapped word lands
_unclassified and the run vanishes from the learning signal (2026-10-27: executors wrote
no_write / no-write / honest_no_write / pending_measurement, all unknown to both sides).

Run:  python tests/test_outcome_vocab_parity.py
Exits non-zero on drift. Reads the sources as text — importing tl_ego_server.py starts Flask.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[3] / "extensions" / "tl-ego" / "index.js"
CORE = HERE.parent / "tl_ego_server.py"

# The words that MUST be understood as "did real work, not the full target" on both sides.
REQUIRED_PARTIAL = {"no_write", "no-write", "honest_no_write", "pending_measurement"}
REQUIRED_SUCCESS = {"success", "completed", "ok", "verified"}
REQUIRED_FAIL = {"failed", "blocked", "error", "timeout"}


def _js_set(src: str, name: str) -> set[str]:
    m = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", src, re.S)
    assert m, f"{name} not found in plugin source"
    body = re.sub(r"//.*", "", m.group(1))
    return set(re.findall(r'"([^"]+)"', body))


def _py_set(src: str, name: str) -> set[str]:
    m = re.search(rf"^{name} = \{{(.*?)\}}", src, re.S | re.M)
    assert m, f"{name} not found in core source"
    body = re.sub(r"#[^\n]*", "", m.group(1))
    return set(re.findall(r'"([^"]+)"', body))


def main() -> int:
    js = PLUGIN.read_text(encoding="utf-8", errors="replace")
    py = CORE.read_text(encoding="utf-8", errors="replace")

    tiers = {
        ("PLUGIN", "success"): _js_set(js, "FB_SUCCESS_OUTCOMES"),
        ("PLUGIN", "fail"): _js_set(js, "FB_FAIL_OUTCOMES"),
        ("PLUGIN", "partial"): _js_set(js, "FB_PARTIAL_OUTCOMES"),
        ("CORE", "success"): _py_set(py, "_GOAL_TERMINAL_SUCCESS"),
        ("CORE", "fail"): _py_set(py, "_GOAL_TERMINAL_FAIL"),
        ("CORE", "partial"): _py_set(py, "_GOAL_TERMINAL_PARTIAL"),
    }
    required = {"success": REQUIRED_SUCCESS, "fail": REQUIRED_FAIL, "partial": REQUIRED_PARTIAL}
    bad = []
    for (side, tier), words in tiers.items():
        missing = required[tier] - words
        if missing:
            bad.append(f"{side} {tier} missing: {sorted(missing)}")

    # Same word must never sit in two tiers of one side (that set wins arbitrarily).
    for side in ("PLUGIN", "CORE"):
        seen: dict[str, str] = {}
        for tier in ("success", "fail", "partial"):
            for w in tiers[(side, tier)]:
                if w in seen:
                    bad.append(f"{side}: {w!r} in both {seen[w]} and {tier}")
                seen[w] = tier

    for line in bad:
        print("DRIFT:", line)
    print("ok" if not bad else f"FAILED ({len(bad)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

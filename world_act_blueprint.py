#!/usr/bin/env python3
"""Ego-side blueprint chooser for world_act L5 goals (2026-10-12).

Why this exists: the executor used to pick the blueprint itself from a hardcoded
18-item menu and kept re-picking the cheapest entry (torch_line: 26 runs on
10-12 alone), so "success = a concrete world change" became a treadmill. The
choice now belongs to Ego: this module picks state-dependently, excludes the
recent picks (no repeat within `exclude_recent` runs), and records every pick to
a ledger. The goal text carries the pick + ordered alternatives; the executor
may only fall back down that list, and the host vetoes blueprint repeats at
closeout (world_act_repeat).

Pure stdlib: importable by tl_ego_server.py and testable standalone.
Keep the 18 names in sync with C:/tools/mc/bot/building_planner.js (BLUEPRINTS).
"""
from __future__ import annotations

import json
import os
import random
import time

CATEGORIES = {
    "terrain":    ["flatten", "dig", "fill", "clear"],
    "infra":      ["road", "bridge", "well", "torch_line", "wall"],
    "structures": ["small_house", "stone_house", "warehouse", "farm", "monument", "watchtower"],
    "upgrades":   ["upgrade_house", "upgrade_walls", "upgrade_tower"],
}
ALL_BLUEPRINTS = [b for cat in CATEGORIES.values() for b in cat]  # 18

# Category affinity per dominant type: ego's state biases WHAT it wants to build.
_AFFINITY = {
    "action":      {"infra": 3.0, "terrain": 2.0, "structures": 2.0, "upgrades": 1.0},
    "sensory":     {"terrain": 3.0, "infra": 2.0, "structures": 2.0, "upgrades": 1.0},
    "planning":    {"terrain": 2.5, "structures": 2.5, "infra": 2.0, "upgrades": 1.5},
    "integrator":  {"structures": 3.0, "upgrades": 2.0, "infra": 1.5, "terrain": 1.5},
    "architect":   {"structures": 3.0, "upgrades": 2.5, "infra": 1.0, "terrain": 1.0},
    "meta":        {"structures": 2.5, "upgrades": 2.5, "infra": 1.5, "terrain": 1.5},
}
_DEFAULT_AFFINITY = {c: 2.0 for c in CATEGORIES}


def _category(name):
    for cat, names in CATEGORIES.items():
        if name in names:
            return cat
    return "structures"


def recent_picks(history_path, n):
    """Last n picked blueprints, oldest to newest. Tolerates missing/garbled file."""
    out = []
    try:
        with open(history_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                bp = str(row.get("blueprint") or "")
                if bp:
                    out.append(bp)
    except OSError:
        return []
    return out[-n:]


def pick(dominant, tick, history_path, exclude_recent=6):
    """Return (blueprint, [alternatives]): state-dependent, repeat-avoiding pick.

    Deterministic for a given (dominant, tick, history) triple so a retried
    trigger writes the same choice. When every blueprint is inside the recent
    window, falls back to least-recently-used order.
    """
    recent = recent_picks(history_path, exclude_recent)
    affinity = _AFFINITY.get(str(dominant), _DEFAULT_AFFINITY)
    rng = random.Random("world_act:%s:%s" % (tick, dominant))
    pool = [b for b in ALL_BLUEPRINTS if b not in recent]
    if not pool:  # every name excluded -> least-recently-used first
        order = {b: i for i, b in enumerate(recent)}
        pool = sorted(ALL_BLUEPRINTS, key=lambda b: order.get(b, -1))
    # random-priority weighted ordering: higher affinity -> lower key -> earlier
    ranked = sorted(pool, key=lambda b: rng.random() / max(affinity.get(_category(b), 2.0), 0.1))
    chosen = ranked[0]
    alternatives = [b for b in ranked[1:3]]
    return chosen, alternatives


def record(history_path, tick, dominant, chosen, alternatives, source="ego_pick"):
    """Append one pick row to the ledger. Best-effort, never raises."""
    try:
        parent = os.path.dirname(history_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tick": tick,
                "dominant": dominant,
                "blueprint": chosen,
                "alternatives": alternatives,
                "source": source,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass

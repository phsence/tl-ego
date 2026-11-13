"""
shadow_arena_with_judge.py
==========================
Provides three callables imported by tl_ego_server.py:22944:
  fetch_status()                    -> dict   (GET /status)
  build_snapshot_from_status(s, n)  -> SimSnapshot
  run_arena(snapshot, n_ticks)      -> {"trajectories": [...], "verdict": ...}

All heavy simulation work delegated to arena_simulator (stdlib only).
"""
from __future__ import annotations

import urllib.request

from arena_simulator import (  # arena_simulator.py lives in ego_sandbox/l5g-<id>/
    BRANCH_PROFILES,
    SimSnapshot,
    TickState,
    Trajectory,
    Verdict,
    build_snapshot_from_status,
    fetch_status as _fetch,
    select_with_control,
    _simulate_branch,
    TYPE_EMBERS,
)


# ── public API ────────────────────────────────────────────────────────────────

def fetch_status() -> dict:
    """Thin wrapper so tl_ego_server.py can import this name directly."""
    return _fetch()


def build_snapshot_from_status(status: dict, n_neurons: int = 20000) -> SimSnapshot:
    """Build a SimSnapshot from a /status dict (forwarded to arena_simulator)."""
    return _simulate_branch.__globals__["build_snapshot_from_status"](status, n_neurons)


def run_arena(snapshot: SimSnapshot, n_ticks: int = 10) -> dict:
    """Run the arena: simulate all profiles, score, return verdict."""
    profiles = list(BRANCH_PROFILES.keys())
    trajectories = [_simulate_branch(snapshot, p, n_ticks) for p in profiles]
    verdict = select_with_control(trajectories)
    return {"trajectories": trajectories, "verdict": verdict}

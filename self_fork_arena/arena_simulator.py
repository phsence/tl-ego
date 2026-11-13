"""
Shadow Ego Arena — light simulation + selection gate.

API (matches tl_ego_server.py:22944-22956 inline imports):
  fetch_status() -> dict           GET http://127.0.0.1:8766/status
  build_snapshot_from_status(status_dict, n_neurons=20000) -> SimSnapshot
  run_arena(snapshot, n_ticks=10)  -> {"trajectories": [...], "verdict": ...}
  select_with_control(trajectories) -> Verdict

Selection criteria weights (stable across runs):
  stability  0.30
  diversity  0.30
  predictability 0.20
  progress   0.20

Pressure profiles: baseline, explorer, opposer, mutator.
Each simulates n_ticks=10 virtual ticks from the current state snapshot.
"""

from __future__ import annotations

import json
import math
import random
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# ─── domain types ─────────────────────────────────────────────────────────────

BRANCH_PROFILES = {
    "baseline": {
        "exploration": 0.5,
        "stability_drive": 0.5,
        "noise_scale": 0.05,
        "transition_bias": {},
    },
    "explorer": {
        "exploration": 0.85,
        "stability_drive": 0.2,
        "noise_scale": 0.15,
        "transition_bias": {"curiosity": 1.5, "sensory": 1.3},
    },
    "opposer": {
        "exploration": 0.4,
        "stability_drive": 0.75,
        "noise_scale": 0.03,
        "transition_bias": {"veto": 1.4, "doubt": 1.3},
    },
    "mutator": {
        "exploration": 0.7,
        "stability_drive": 0.3,
        "noise_scale": 0.2,
        "transition_bias": {"rule_modification": 1.5, "meta": 1.4},
    },
}

TYPE_EMBERS = [
    "architect", "doubt", "decider", "action", "integrator",
    "sensory", "curiosity", "veto", "cultural", "planning",
    "memory", "meta", "emotional", "social", "ethical",
    "aesthetic", "temporal", "spatial", "rule_modification",
]


@dataclass
class TickState:
    """One virtual tick of a branch."""
    dominant: str
    concentration: float
    competition: float
    stagnation: float
    dominant_types_sequence: list[str] = field(default_factory=list)


@dataclass
class Trajectory:
    branch_id: str
    ticks: list[TickState] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class CriterionResult:
    name: str
    normalized: float
    weight: float
    contribution: float


@dataclass
class Verdict:
    winner_id: str
    winner_score: float
    all_scores: dict[str, float]
    criteria: list[list[CriterionResult]]  # per-branch criterion lists
    reasoning: str = ""


# ─── live status fetch ─────────────────────────────────────────────────────────

def fetch_status() -> dict:
    """GET /status from the running Ego instance."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        print(f"[arena] fetch_status failed ({exc}); using defaults")
        return {}


# ─── snapshot builder ──────────────────────────────────────────────────────────

@dataclass
class SimSnapshot:
    """Minimal neural state needed for the simulation."""
    tick: int = 0
    last_dominant: str = "architect"
    last_concentration: float = 0.43
    last_competition: float = 0.86
    stagnation_streak: int = 0
    # extracted from /status fractal_attractor level2:
    type_summary: dict[str, dict] = field(default_factory=dict)
    active_types: list[str] = field(default_factory=list)


def build_snapshot_from_status(status: dict, n_neurons: int = 20000) -> SimSnapshot:
    fa = (status or {}).get("fractal_attractor", {})
    l2 = fa.get("level2", {})
    snap = SimSnapshot(
        tick=status.get("resting_ticks", 0),
        last_dominant=l2.get("dominant", "architect"),
        last_concentration=l2.get("concentration", 0.43),
        last_competition=0.86,   # not in level2, keep from parent status
        stagnation_streak=status.get("stagnation_streak", 0),
        type_summary=l2.get("type_summary", {}),
        active_types=l2.get("active_types", []),
    )
    return snap


# ─── simulation core ───────────────────────────────────────────────────────────

def _transition_prob(current: str, target: str, profile: dict, active_types: list) -> float:
    """Heuristic transition probability current -> target under this profile."""
    base = 0.05
    if target == current:
        return 0.65  # inertia

    bias = profile.get("transition_bias", {}).get(target, 1.0)
    active = 1.3 if target in active_types else 0.8

    # Exploration drives type switches; stability resists them.
    exploration = profile.get("exploration", 0.5)
    stability = profile.get("stability_drive", 0.5)
    switch_drive = exploration * 1.2 - stability * 0.5

    prob = base * bias * active * (0.3 + switch_drive)
    return min(max(prob, 0.01), 0.85)


def _simulate_branch(snap: SimSnapshot, profile_name: str, n_ticks: int) -> Trajectory:
    profile = BRANCH_PROFILES[profile_name]
    traj = Trajectory(branch_id=profile_name, profile=dict(profile))

    dom = snap.last_dominant
    conc = snap.last_concentration
    comp = snap.last_competition
    stag = snap.stagnation_streak
    active = snap.active_types or TYPE_EMBERS[:5]
    rng = random.Random(int.from_bytes(profile_name.encode(), "big") & 0xFFFF)  # stable across processes (str hash() is per-process randomized)

    noise = profile.get("noise_scale", 0.05)

    for _ in range(n_ticks):
        # Choose next dominant by weighted draw
        weights = {}
        for t in TYPE_EMBERS:
            p = _transition_prob(dom, t, profile, active)
            weights[t] = p

        # Normalise and draw
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}
        r = rng.random()
        cumul = 0.0
        next_dom = dom  # fallback
        for t, w in weights.items():
            cumul += w
            if r <= cumul:
                next_dom = t
                break

        # Update metrics with profile-dependent noise
        conc = max(0.05, min(0.95, conc + rng.gauss(0, noise) + (0.05 if next_dom != dom else 0.02)))
        comp = max(0.3, min(0.99, comp + rng.gauss(0, noise * 0.5)))
        stag = max(0, stag - 1 if next_dom != dom else stag)

        tick_state = TickState(
            dominant=next_dom,
            concentration=round(conc, 4),
            competition=round(comp, 4),
            stagnation=stag,
            dominant_types_sequence=[t for t in traj.ticks[-9:] + [next_dom]],
        )
        traj.ticks.append(tick_state)
        dom = next_dom

    return traj


# ─── scoring ───────────────────────────────────────────────────────────────────

def _score_stability(traj: Trajectory) -> float:
    """Low stagnation streak + smooth concentration."""
    end_stag = traj.ticks[-1].stagnation if traj.ticks else 0
    concs = [t.concentration for t in traj.ticks]
    mean_conc = sum(concs) / len(concs) if concs else 0.5
    conc_var = sum((c - mean_conc) ** 2 for c in concs) / len(concs) if concs else 0
    return round(1.0 - min(end_stag / 10, 1.0) - min(conc_var * 4, 0.5), 4)


def _score_diversity(traj: Trajectory) -> float:
    """Fraction of distinct dominant types visited."""
    if not traj.ticks:
        return 0.0
    types_seen = {t.dominant for t in traj.ticks}
    return round(len(types_seen) / len(TYPE_EMBERS), 4)


def _score_predictability(traj: Trajectory) -> float:
    """Self-repeat rate in type transitions."""
    if len(traj.ticks) < 2:
        return 0.5
    repeats = sum(1 for i in range(1, len(traj.ticks)) if traj.ticks[i].dominant == traj.ticks[i - 1].dominant)
    return round(repeats / (len(traj.ticks) - 1), 4)


def _score_progress(traj: Trajectory) -> float:
    """Mean concentration improvement over ticks (higher = more focus achieved)."""
    if len(traj.ticks) < 2:
        return 0.5
    first = traj.ticks[0].concentration
    last = traj.ticks[-1].concentration
    delta = last - first
    return round(max(0.0, min(1.0, 0.5 + delta * 3)), 4)  # scale +0.33 conc delta → +1.0


# ─── arena orchestrator ────────────────────────────────────────────────────────

def run_arena(snapshot: SimSnapshot, n_ticks: int = 10) -> dict:
    profiles = ["baseline", "explorer", "opposer", "mutator"]
    trajectories = [_simulate_branch(snapshot, p, n_ticks) for p in profiles]

    verdict = select_with_control(trajectories)
    return {"trajectories": trajectories, "verdict": verdict}


# ─── selection gate ────────────────────────────────────────────────────────────

def select_with_control(trajectories: list[Trajectory]) -> Verdict:
    criteria_weights = [
        ("stability", 0.30),
        ("diversity", 0.30),
        ("predictability", 0.20),
        ("progress", 0.20),
    ]

    scorers = {
        "stability": _score_stability,
        "diversity": _score_diversity,
        "predictability": _score_predictability,
        "progress": _score_progress,
    }

    all_criteria: dict[str, list[CriterionResult]] = {}
    all_scores: dict[str, float] = {}

    for traj in trajectories:
        branch_score = 0.0
        crit_list = []
        for name, weight in criteria_weights:
            raw = scorers[name](traj)
            contrib = raw * weight
            branch_score += contrib
            crit_list.append(CriterionResult(
                name=name, normalized=raw, weight=weight, contribution=contrib,
            ))
        all_criteria[traj.branch_id] = crit_list
        all_scores[traj.branch_id] = round(branch_score, 4)

    winner_id = max(all_scores, key=all_scores.get)
    winner_score = all_scores[winner_id]

    # Build reasoning
    parts = []
    for bid, clist in all_criteria.items():
        bits = [f"{c.name}={c.normalized:.2f}×{c.weight}" for c in clist]
        parts.append(f"{bid}={winner_score if bid==winner_id else all_scores[bid]:.3f}({','.join(bits)})")
    reasoning = "; ".join(parts)

    return Verdict(
        winner_id=winner_id,
        winner_score=winner_score,
        all_scores=all_scores,
        criteria=[all_criteria[t.branch_id] for t in trajectories],
        reasoning=reasoning,
    )

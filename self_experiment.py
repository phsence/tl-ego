"""Self-directed randomized controlled trials, scheduled from inside the ego tick loop.

Why this file exists
--------------------
2026-11-11 22:56 the L2 loop closed for the first time:

    [evolve] tick=70920 evidence=verified_intervention
             changes: integrator_concentration_floor: 0.120->0.300

But the *hypothesis generator* was an external operator script
(causal_param_sweep.py): a human picked the parameter, the delta and the window;
ego only verified and applied. That is a semi-automatic loop, not a recursive
one — it improves itself only when someone hands it an experiment.

This module moves the driver inside. Ego picks a parameter, preregisters the
design, alternates arms on its own ticks, measures its own dynamics, and lets
the existing gate do the accepting. No HTTP, no external process.

The hypothesis space is *not* hardcoded. Every numeric parameter in the
self-model table is a candidate, and which (parameter, direction, magnitude) to
try next is chosen by Thompson sampling over the outcomes of every trial in the
ledger — including trials run by other actors in earlier generations. A lever
that keeps returning null loses its sampling weight; an unexplored lever gets a
small exploration bonus and is probed conservatively (smallest step first).

Non-negotiable constraints
--------------------------
* The ledger schema must stay compatible with causal_ab_arm.py. The gate
  recomputes evidence from that ledger (newest_evidence_for); a private format
  would make every result unverifiable, i.e. worthless.
* One cell per tick. The tick loop must never block.
* A restart voids the open trial (the gate is generation-scoped by design) but
  must NOT wipe what was learned — knowledge about levers is not evidence.
* Never trial a parameter that another writer owns every tick. The
  criticality_regulator PID writes noise_amp_boost / explore_stdp_mult, and L5
  writes on its own cadence; an "effect" measured there is an artifact of who
  wrote last, not of the value.
"""

from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import causal_ab_arm as C

# --- trial shape -------------------------------------------------------------
N_ARM = 10              # gate hard-requires >= 10 measured cells per arm
SETTLE_TICKS = 1        # dynamics snap to the new setting within one tick
SAMPLES_PER_CELL = 2    # ticks averaged per cell — see _record for why this exists
MAX_SAMPLES_PER_CELL = 4  # collected when the two samples disagree on regime
REGIME_TOL = 0.02       # max |comp_a - comp_b| for two samples to count as one regime
MAX_OPEN_TICKS = 400    # abandon a trial that cannot finish (tick drift / deadlock)
MAX_OPEN_S = 5400       # wall-clock backstop: ticks can be slow, wall time cannot lie
START_COOLDOWN = 30     # ticks of rest between trials. Not 120: the gate's own
                        # causal attribution runs every 5 ticks and its evolve
                        # pass every 20, so 30 ticks is enough for the evidence to
                        # be picked up. At ~100s/tick the old 120 was over three
                        # hours — longer than a generation lives, which capped the
                        # system at one experiment per boot.
MAX_DEFERS = 3          # start anyway after this many "cannot finish in time" waits
# How long to stay quiet just to measure the clock a trial will run on. Four
# ticks is enough for three gaps, which is the smallest sample the median needs.
QUIET_PROBE_TICKS = 4
# A trial that starts near the end of a generation is voided by the restart,
# no matter how well designed it is. Measured 2026-11-12: 14 voided trials,
# 11 of them "generation_changed" (41% of all starts produced no evidence).
# Leave this much headroom over the 20 measured cells for apply/restore ticks.
#
# HONEST SCOPE LIMIT, measured against those same 11 voids on 2026-11-12:
# replaying this gate on the real ledger catches only 1 of the 11. Ten of them
# started at the BEGINNING of a generation (room ~3900s) and were cut short by
# an EARLY restart. Generation intervals are n=32, min 9.5 / p25 26.4 /
# median 69.4 / p75 103 / max 802.9 minutes - 7 of 32 are under 25 minutes, so
# the median does not predict the next restart. This guard covers the
# predictable tail (a long generation that is genuinely nearly over); it cannot
# predict an exogenous restart. The real lever on void rate is trial WALL TIME
# (20 cells x tick seconds), not the calendar. Do not mistake this for a fix.
GEN_ROOM_MARGIN = 1.15
# How often to re-check while waiting for the next generation. Short on purpose:
# the condition resolves itself the moment the process restarts.
GEN_WAIT_TICKS = 3
# How many times the scheduler may spend a quiet probe before it has to decide
# with whatever clock it has. Two is enough: the first probe always produces
# gaps, the second covers the case where the tick stalled through the first.
QUIET_PROBE_ATTEMPTS = 2
REEXPLORE_TICKS = 2500  # how long a spent (parameter, direction, magnitude) stays spent
WIN_REEXPLORE_TICKS = 60  # a win only sleeps: climb the same lever again soon after
WIN_COOLDOWN_TICKS = 40  # gap after a win, so the gate's own adopt cycle lands first
OUTCOME_METRIC = "fitness_stable_v1"

OFF_FILE = "self_experiment_off"     # touch .lae/self_experiment_off to pause
STATE_FILE = "self_experiment_state.json"

# Magnitudes as a fraction of the parameter's range. Three coarse bins is
# deliberate: with ~40 ticks per trial we cannot afford a fine grid, and the
# bandit needs repeat pulls per arm to learn anything.
MAG_BINS = (0.10, 0.25, 0.50)

# --- reading a failed trial ---------------------------------------------------
# A null result is not one thing. Distinguishing its three causes is the last
# step that used to live outside this module: a human reading the number and
# deciding what to try next. Encoding it is what turns retries into ascent.
#
#   right sign, not significant  -> underpowered: same lever, bigger dose.
#                                   Signal scales with dose; noise does not.
#   significant, wrong sign      -> wrong direction: turn around.
#   flat at the largest dose     -> dead lever: stop spending generations on it.
MIN_SIGNAL = 0.008        # below this the point estimate is indistinguishable
                          # from 0 at our resolution, so it carries no direction
TRIVIAL_EFFECT = 0.005    # a win smaller than this is statistically real and
                          # practically worthless. The gate only asks "is it
                          # nonzero"; significance is not benefit. Without this
                          # floor the loop drifts upward on measurement noise —
                          # observed: +0.0020 then +0.0007 adopted as "wins",
                          # walking the parameter past its plateau into a dead
                          # zone. That is Goodhart with a confidence interval.
ESCALATE_BONUS = 0.30     # A hint is not an arm, it is knowledge we already paid a
                          # whole generation to obtain, so it must be able to beat
                          # the prior draw. An untried cell samples Beta(1,1) =
                          # Uniform(0,1) (sd 0.29); a +0.05 hint lost to that noise
                          # almost every time, and the scheduler ignored its own
                          # conclusion. At 0.30 a hint wins ~85% of draws, and it
                          # self-consumes: once tried, the same cell cannot be
                          # re-run at an unchanged baseline.
DEAD_PENALTY = 0.15       # multiplied by dead votes, subtracted from theta
SPENT_PENALTY = 0.30      # one direction has nothing left to give

# --- cross-ruler evidence ----------------------------------------------------
# 2026-11-12, after 14 completed trials and one verified result: the only lever
# that ever moved the score was integrator_concentration_floor, and the reason is
# arithmetic, not luck — fitness_stable_v1 is 1 - 2*max(0,comp-0.85) - |conc-0.25|,
# a plane in two variables, and that parameter is a *floor on conc*. It wins by
# tautology. Every other lever was probed once, under dynamics_health, whose
# within-arm SD was 0.28-0.49; three of them still recorded d = 0.16 .. 0.60 at
# n = 10-30, i.e. right-signed, real-looking, and unresolvable by that instrument.
# They were then buried twice over: _refresh_stats counted their "not
# significant" as a pull-without-win (halving the posterior mean the sampler
# draws from), and _learn only reads this module's own history, so it never saw
# them at all. Ten of the fourteen trials in the ledger were operator-era and
# invisible to the ladder.
#
# The fix is to use the one statistic that is unit-free: Cohen's d. A lever that
# looked promising to a blunt instrument and has never been re-measured with the
# sharp one is the highest-information experiment available — the effect is
# already suspected, only the resolution was missing.
CROSS_METRIC_MIN_D = 0.15   # below this a cross-ruler reading carries no direction
CROSS_METRIC_BONUS = 0.40   # must outrank a same-ruler dose bump (ESCALATE_BONUS)

# How stale a foreign parameter write has to be before it stops counting as
# contention. A trial is ~20-40 ticks, so anything last written more than a
# trial-length ago cannot still be fighting us for the value. Without an age
# limit the boot-replay burst (all at tick 0) blocked 13 of 15 levers for a
# whole generation — see _refresh_recent.
RECENT_WINDOW_TICKS = 60

# How many raw tick gaps to keep for the robust duration estimate. Twelve is
# two trial-lengths: enough for a median to mean something, small enough that a
# genuinely slower machine shows up within a generation.
MAX_RECENT_TICKS_FOR_BUDGET = 12

# --- pooling repeat trials ---------------------------------------------------
# 2026-11-12. A paired 10-vs-10 test only has 80% power against d >= 0.89, and
# the effects this environment actually produces sit at d = 0.17 .. 0.60. So the
# scheduler's null results are mostly "the instrument could not resolve it", and
# it responded by re-running the same cell: alpha_seed_mix was tried four times
# at 0.1->0.42 for d = +0.34, -0.30, -0.21 — three readings of one cell, each
# judged alone and thrown away. Four generations bought no conclusion, because
# nothing ever added the readings together.
#
# Pooling is the standard answer: weight each reading by 1/var and combine. It
# does not manufacture evidence — a pooled null still means "not resolvable at
# this resolution" — but it does turn four wasted trials into one verdict.
#
# Pooling deliberately stays inside OUTCOME_METRIC. Effects measured on another
# ruler are in different units and must not be added to these; that is what
# _cross / Cohen's d is for, and the two are kept separate on purpose.
POOL_MIN_TRIALS = 2        # one trial is not a pool
POOL_MIN_PAIRS = 24        # below this, agreeing nulls are not evidence of absence
POOL_BONUS = 0.35          # a direction that survives pooling, never yet verified
POOL_DEAD_VOTES = 2        # how many dead votes a pooled null is worth

# Levers the operator has already characterised. They keep a small priority
# boost so a known-good lever is not ignored forever, but they are not special:
# everything else in the table competes on equal footing.
PRIORITY = {
    "integrator_concentration_floor": 0.05,
    "type_dominance_cap": 0.03,
    "diversity_n": 0.02,
}

# Written by another process every tick — their effect is unmeasurable by design.
PID_OWNED = {"noise_amp_boost", "explore_stdp_mult"}
# Our own actors; updates from these do not disqualify a parameter.
OWN_SOURCES = {"operator_recovery", "operator_causal_sweep", "self_experiment"}
# A parameter whose value is replayed from a stale snapshot at boot cannot be
# trialled: the value would be silently reset mid-experiment.
BOOT_REPLAY_SOURCE = "boot_replay"


def fitness_stable(conc: Any, comp: Any) -> Optional[float]:
    """Variance-reduced health. Same definition as causal_param_sweep.py.

    Deliberately drops stagnation_streak and transition_rate_8: measured
    within-arm SD of dynamics_health was 0.40 while real effects were 0.02-0.11,
    i.e. a 1:60 signal-to-noise ruler. comp and conc are stable to ~5e-4.
    """
    if not isinstance(conc, (int, float)) or not isinstance(comp, (int, float)):
        return None
    h = 1.0
    h -= max(0.0, float(comp) - 0.85) * 2.0
    h -= abs(float(conc) - 0.25) * 1.0
    return round(h, 6)


class SelfExperimenter:
    """Per-tick state machine. Construct once; call tick(n) every tick."""

    def __init__(self, workspace: Path, generation_id: str,
                 get_params: Callable[[], Dict[str, dict]],
                 apply_params: Callable[[], None],
                 get_state: Callable[[], Dict[str, Any]],
                 log: Callable[[str], None] = None):
        self.workspace = Path(workspace)
        self.generation_id = generation_id
        self.get_params = get_params
        self.apply_params = apply_params
        self.get_state = get_state
        self.log = log or (lambda msg: print(msg, flush=True))
        self.state_path = self.workspace / ".lae" / STATE_FILE
        self.ledger = C.ledger_path(self.workspace)
        self.updates_path = self.workspace / ".lae" / "parameter_updates.jsonl"
        self._mtime = None
        self._recent: Dict[str, str] = {}
        self._rng = random.Random()
        self._rng_trial: Optional[str] = None
        self._stats_mtime = None
        self._stats: Dict[Tuple[str, int, int], Tuple[int, int]] = {}
        # Every completed trial ever run on a lever, regardless of ruler or
        # decisiveness. Used only for "have we ever touched this?", which is a
        # different question from "how often has it won" — conflating the two is
        # what let an underpowered reading look like a defeat.
        self._attempts: Dict[str, int] = {}
        # (parameter, direction, bin) -> (Cohen's d, metric name) for readings
        # taken on a ruler other than the current one.
        self._cross: Dict[Tuple[str, int, int], Tuple[float, str]] = {}
        # Cells already measured under the CURRENT ruler: never re-propose these
        # from cross-ruler evidence, or we would spend a generation re-running a
        # question this instrument has already answered.
        self._measured_now: set = set()
        # (parameter, direction, bin) -> combined effect/CI over every trial of
        # that cell on the current ruler. Turns repeats into one verdict instead
        # of N separate nulls — see _pool_readings.
        self._pooled: Dict[Tuple[str, int, int], Dict[str, float]] = {}
        # (parameter, direction) -> combined effect over every dose bin of that
        # lever, and the directions that pooled evidence has retired.
        self._pooled_kd: Dict[Tuple[str, int], Dict[str, float]] = {}
        self._pooled_dead: set = set()

    # --- persistence ---------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"generation": None, "open": None, "history": [],
                    "direction": {}, "next_start_tick": 0}

    def _save(self, st: Dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            self.log(f"[self-exp] state save failed: {exc}")

    # --- live parameter access ----------------------------------------------
    def _apply(self, key: str, value: float) -> Optional[float]:
        p = self.get_params().get(key)
        if not isinstance(p, dict):
            return None
        try:
            v = max(float(p["min"]), min(float(p["max"]), float(value)))
        except (KeyError, TypeError, ValueError):
            return None
        p["current"] = v
        try:
            self.apply_params()
        except Exception as exc:
            self.log(f"[self-exp] apply_params failed: {exc}")
            return None
        return v

    # --- safety: who else touched this parameter lately ----------------------
    def _refresh_recent(self, tick: int) -> None:
        """Which parameters another writer changed recently enough to still be live.

        Two defects lived here. Neither looked like a bug from the outside, and
        together they cut the reachable trial set down to two levers.

        * First-wins. `key not in recent` kept the EARLIEST foreign write in the
          window, so a single old external edit blocked a lever forever, even
          after our own writer had touched it later. Recency has to mean
          last-write; _refresh_stats already worked that way.
        * No notion of "how long ago". The 800-line tail is a proxy for recency,
          but boot replays arrive in a burst at tick 0 and stay inside that
          window for hundreds of ticks. Every replayed parameter was therefore
          counted as "another writer owns this right now" for the whole
          generation. Measured 2026-11-12: 13 of 15 keys blocked, including
          integrator_concentration_floor — the only lever that has ever produced
          a verified gain — and three of the four cross-ruler candidates. The
          scheduler's entire reachable set was {diversity_n, alpha_seed_mix},
          which is exactly the pair it spent five generation-long trials on.

        Age is now measured in ticks rather than in lines: a write older than
        RECENT_WINDOW_TICKS is history, not contention. boot_replay is still
        honoured where the concern is real — during boot, when the replay is
        genuinely recent and could still clobber a value mid-experiment.
        """
        try:
            mt = self.updates_path.stat().st_mtime
        except OSError:
            return
        if mt == self._mtime:
            return
        self._mtime = mt
        last: Dict[str, Tuple[str, Any]] = {}
        try:
            with open(self.updates_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh.readlines()[-800:]:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    key = row.get("key")
                    if not key:
                        continue
                    src = str(row.get("source", ""))
                    # last write wins: a later own-write clears an earlier
                    # foreign one, which is the whole point of asking "recently".
                    last[key] = (src, row.get("tick"))
        except Exception:
            self._recent = {}
            return
        recent: Dict[str, str] = {}
        for key, (src, row_tick) in last.items():
            if src in OWN_SOURCES:
                continue
            age = None
            if isinstance(row_tick, (int, float)):
                age = float(tick) - float(row_tick)
            if age is not None and age > RECENT_WINDOW_TICKS:
                continue                    # long ago: it cannot interfere now
            recent[key] = src
        self._recent = recent

    # --- what the ledger already taught us ----------------------------------
    def _refresh_stats(self) -> None:
        """Aggregate every completed trial in the ledger into (pulls, wins).

        Keyed by (parameter, direction, magnitude bin). Wins are trials whose
        paired CI excluded zero in the improving direction — exactly the rule
        the gate applies, so the bandit optimises for the thing that can
        actually change the system, not for a proxy.

        Only *decisive* trials move the win rate. This used to increment n on
        every completed trial, so a reading the instrument could not resolve
        counted as a pull with no win: Beta(1, 1+k) instead of Beta(1, 1). At
        k=2 the posterior mean drops from 0.500 to 0.250, and an untouched cell
        draws ~0.5 — meaning a lever that was merely measured too bluntly is
        abandoned in favour of one nobody has ever looked at. That is how
        diversity_n (d=0.60), evidence_persist_ticks (d=0.30) and
        type_dominance_cap (d=0.16) went unretried while the scheduler spent
        five consecutive trials on alpha_seed_mix, whose best measured effect
        was 0.002. Absence of evidence is not evidence of absence; n has to
        mean "readings that could have distinguished the arms".
        """
        try:
            mt = self.ledger.stat().st_mtime
        except OSError:
            return
        if mt == self._stats_mtime:
            return
        self._stats_mtime = mt
        rows: Dict[str, Dict[str, Any]] = {}
        try:
            with open(self.ledger, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    tid = row.get("trial_id")
                    if tid and row.get("status") == "complete":
                        rows[tid] = row        # last complete snapshot wins
        except Exception:
            return
        params = self.get_params()
        stats: Dict[Tuple[str, int, int], Tuple[int, int]] = {}
        attempts: Dict[str, int] = {}
        cross: Dict[Tuple[str, int, int], Tuple[float, str]] = {}
        measured_now: set = set()
        pooled_reads: Dict[Tuple[str, int, int], list] = {}
        for tid, row in rows.items():
            try:
                key = row["parameter"]
                cv = float(row["control_value"])
                iv = float(row["intervention_value"])
            except (KeyError, TypeError, ValueError):
                continue
            p = params.get(key) or {}
            try:
                span = float(p["max"]) - float(p["min"])
            except (KeyError, TypeError, ValueError):
                span = 0.0
            if span <= 0:
                continue
            frac = abs(iv - cv) / span
            bi = min(range(len(MAG_BINS)), key=lambda i: abs(MAG_BINS[i] - frac))
            d = 1 if iv > cv else -1
            attempts[key] = attempts.get(key, 0) + 1
            try:
                res = C.analyze(self.workspace, tid)
            except Exception:
                continue
            effect = res.get("effect_size")
            if not isinstance(effect, (int, float)):
                continue
            metric = row.get("outcome_metric") or "?"
            # Cohen's d: the only number in the ledger that means the same thing
            # on two different rulers. Recover the arm SD from the CI width —
            # analyze() reports the half-width as 1.96*sd/sqrt(n) for the paired
            # analysis and the same shape for Welch, so either way this inverts.
            ci = res.get("effect_ci95") or []
            dval = 0.0
            try:
                half = (float(ci[1]) - float(ci[0])) / 2.0
                n_arm = max(1, int(res.get("n_control") or 1))
                sd = half * math.sqrt(n_arm) / 1.96
                if sd > 1e-12:
                    dval = float(effect) / sd
            except (IndexError, TypeError, ValueError):
                dval = 0.0

            if metric == OUTCOME_METRIC:
                # A "win" means "improved the objective", and the objectives
                # differ between rulers, so this table is deliberately single-
                # ruler: mixing them would let a lever that helps one objective
                # raise its win rate on another.
                measured_now.add((key, d, bi))
                if res.get("eligible"):
                    n, w = stats.get((key, d, bi), (0, 0))
                    stats[(key, d, bi)] = (n + 1, w + (1 if effect > 0 else 0))
                # Keep every reading of this cell so repeats can be combined.
                # The variance is recovered from the reported CI, so it carries
                # that trial's own noise without assuming which analysis shape
                # produced it.
                try:
                    se = (float(ci[1]) - float(ci[0])) / 2.0 / 1.96
                    npair = min(int(res.get("n_control") or 0),
                                int(res.get("n_intervention") or 0))
                except (IndexError, TypeError, ValueError):
                    se, npair = 0.0, 0
                if se > 1e-12:
                    pooled_reads.setdefault((key, d, bi), []).append(
                        (float(effect), se * se, npair))
                continue

            # Another ruler. Whether it resolved or not, a large |d| means the
            # lever is not inert — and that is the one thing worth carrying
            # across instruments. Keep the direction that looked better, not
            # necessarily the one that was tried.
            if abs(dval) < CROSS_METRIC_MIN_D:
                continue
            good_d = d if effect > 0 else -d
            cell = (key, good_d, bi)
            prev = cross.get(cell)
            if prev is None or abs(dval) > abs(prev[0]):
                cross[cell] = (round(dval, 4), metric)
        self._stats = stats
        self._attempts = attempts
        self._cross = cross
        self._measured_now = measured_now
        self._pooled, self._pooled_kd = self._pool_readings(pooled_reads)

    @staticmethod
    def _combine(reads: list) -> Optional[Dict[str, float]]:
        """Inverse-variance combination of one group of readings."""
        if len(reads) < POOL_MIN_TRIALS:
            return None
        if any(v <= 0 for (_e, v, _n) in reads):
            return None
        wsum = sum(1.0 / v for (_e, v, _n) in reads)
        if wsum <= 0:
            return None
        eff = sum((1.0 / v) * e for (e, v, _n) in reads) / wsum
        se = math.sqrt(1.0 / wsum)
        return {"effect": eff, "se": se,
                "lo": eff - 1.96 * se, "hi": eff + 1.96 * se,
                "trials": float(len(reads)),
                "pairs": float(sum(n for (_e, _v, n) in reads))}

    @staticmethod
    def _pool_readings(readings: Dict[Tuple[str, int, int], list]
                       ) -> Tuple[Dict[Tuple[str, int, int], Dict[str, float]],
                                  Dict[Tuple[str, int], Dict[str, float]]]:
        """Inverse-variance combination of every trial of the same cell.

        Each entry is (effect, variance, number of pairs). Weighting by 1/var is
        the fixed-effects meta-analysis estimate: a trial that happened to run
        during a quiet stretch counts for more than one that ran through a
        storm, and the combined interval is narrower than any of them.

        This produces a *hint*, never a verification. The gate accepts only a
        single-generation randomised trial with its own CI excluding zero; a
        pooled estimate spanning four generations is a much weaker object and
        must never be presented as proof. It answers one question — "is this
        cell worth another generation?" — and nothing else.
        """
        out: Dict[Tuple[str, int, int], Dict[str, float]] = {}
        for cell, reads in readings.items():
            p = SelfExperimenter._combine(reads)
            if p:
                out[cell] = p
        # The same readings, grouped by lever and direction instead of by dose
        # bin. A verdict about a lever has to survive a change of dose: three
        # null readings at 0.1->0.42 say the lever does nothing, and re-testing
        # it at 0.1->0.30 is the same question wearing a different number.
        by_kd: Dict[Tuple[str, int], list] = {}
        for (key, d, _bi), reads in readings.items():
            by_kd.setdefault((key, d), []).extend(reads)
        out_kd: Dict[Tuple[str, int], Dict[str, float]] = {}
        for kd, reads in by_kd.items():
            p = SelfExperimenter._combine(reads)
            if p:
                out_kd[kd] = p
        return out, out_kd

    # --- reading the outcomes we already have --------------------------------
    @staticmethod
    def _parse_sig(sig: Any) -> Optional[Tuple[str, int, int]]:
        """'key:+1:2' -> ('key', 1, 2); legacy 'key:+1' -> ('key', 1, 0).

        Legacy two-part signatures carry no magnitude. Dropping them (the earlier
        behaviour) threw away the single most valuable row in the history: the only
        lever that ever produced a real gain was recorded by the operator era as
        `integrator_concentration_floor:+1`, so the scheduler could not see that it
        had ever worked and spent five straight trials on levers that do nothing.

        bin 0 is the conservative reading — treat it as the smallest dose and let
        the ladder climb from there — rather than guessing a magnitude that was
        never recorded.
        """
        if not sig or not isinstance(sig, str):
            return None
        parts = sig.rsplit(":", 2)
        try:
            if len(parts) == 3:
                return parts[0], int(parts[1]), int(parts[2])
            if len(parts) == 2:
                return parts[0], int(parts[1]), 0
        except ValueError:
            return None
        return None

    def _learn(self, history) -> Tuple[Dict[Tuple[str, int, int], float],
                                       Dict[str, float]]:
        """Turn every past null into a directional hint or a verdict of death.

        Returns
        -------
        escalate: {(parameter, direction, bin) -> bonus}
            The bin worth spending the next trial on. Produced two ways: a
            right-signed but underpowered null bumps the dose (same lever, one
            bigger); a wrong-signed significant effect points the other way.
        dead: {parameter -> penalty}
            Only when BOTH directions have come back flat at the largest
            magnitude. One flat direction on a bounded parameter often just
            means that side is capped, which says nothing about the other side.
        """
        escalate: Dict[Tuple[str, int, int], float] = {}
        flat_max: Dict[str, set] = {}
        spent: Dict[Tuple[str, int], float] = {}
        last_bin = len(MAG_BINS) - 1

        # Use the MOST INFORMATIVE dose per (parameter, direction), not every
        # reading added together. A flat -0.0004 at bin 0 says nothing about the
        # sign; a flat result at bin 2 says the lever is spent. Keeping the
        # highest-bin reading per direction and discarding the rest is what stops
        # stale hints from outliving the evidence that superseded them.
        best_reading: Dict[Tuple[str, int], Tuple[int, float, float, bool]] = {}
        for h in history or ():
            sig = self._parse_sig(h.get("signature"))
            if not sig:
                continue
            key, d, bi = sig
            effect = h.get("effect")
            if not isinstance(effect, (int, float)):
                continue
            slot = (key, d)
            prev = best_reading.get(slot)
            strength = abs(float(effect))
            if prev is None or bi > prev[0] or (bi == prev[0] and strength > prev[1]):
                best_reading[slot] = (bi, strength, float(effect),
                                      bool(h.get("eligible")))

        frontier: Dict[Tuple[str, int], Tuple[int, float]] = {}
        for (key, d), (bi, _strength, effect, eligible) in best_reading.items():
            verdict = self._verdict(f"{key}:{d:+d}:{bi}", effect, eligible)
            if verdict == "no_reading":
                continue
            if verdict in ("flat_at_max_dose", "trivial"):
                # Nothing worth having at this dose. For a trivial win we know the
                # knob does move, it just does not buy anything measurable — so
                # the direction is spent, exactly like a flat reading at the top.
                flat_max.setdefault(key, set()).add(d)
                spent[(key, d)] = SPENT_PENALTY
                continue
            if verdict in ("climb", "underpowered", "flat"):
                # Right sign, or no readable sign. Either way the answer to "why
                # is the effect small" is "the dose was small", so climb the same
                # direction. Flat gets a weaker push: it has no sign to back it.
                good_d, rung = d, min(last_bin, bi + 1)
                weight = ESCALATE_BONUS if verdict != "flat" else ESCALATE_BONUS * 0.6
                if rung == bi:
                    weight *= 0.5             # top rung already: don't queue a twin
            else:                             # hurts / weak_hurts
                good_d, rung = -d, bi
                weight = ESCALATE_BONUS * (0.6 if verdict == "hurts" else 0.4)
            slot = (key, good_d)
            prev = frontier.get(slot)
            if prev is None or rung > prev[0]:
                frontier[slot] = (rung, weight)
            elif rung == prev[0]:
                frontier[slot] = (rung, max(weight, prev[1]))
        for (key, good_d), (rung, weight) in frontier.items():
            escalate[(key, good_d, rung)] = weight
        dead = {k: DEAD_PENALTY * min(3, len(v))
                for k, v in flat_max.items() if len(v) >= 2}

        # Repeats are evidence, but only once they are added up.
        #
        # By 2026-11-12 the scheduler had run alpha_seed_mix at 0.1->0.42 four
        # times, measuring d = +0.34, -0.30, -0.21, and drew no conclusion from
        # any of them because each was judged alone. Three agreeing nulls over
        # 30 pairs is a real answer: at this resolution, on this instrument,
        # the lever does not move the score. Retiring it is what makes the next
        # generation a different experiment instead of the fifth repeat.
        #
        # The opposite case is the one worth paying for. A cell whose pooled
        # interval excludes zero carries a direction no single 10-vs-10 could
        # resolve — exactly the trial to run next, because the effect is
        # already located and only the power was missing.
        for (key, d, bi), p in self._pooled.items():
            if p["lo"] > 0 or p["hi"] < 0:
                good_d = d if p["effect"] > 0 else -d
                escalate[(key, good_d, bi)] = max(
                    escalate.get((key, good_d, bi), 0.0), POOL_BONUS)
            elif p["pairs"] >= POOL_MIN_PAIRS:
                dead[key] = max(dead.get(key, 0.0), DEAD_PENALTY * POOL_DEAD_VOTES)
        # The same verdict, judged across dose bins. Retiring only the exact
        # (lever, direction, bin) let the scheduler slip straight back into the
        # lever on the next dose - measured 2026-11-12: alpha_seed_mix, pooled
        # to +0.0002 over 30 pairs, was picked again one bin over.
        self._pooled_dead = set()
        for (key, d), p in self._pooled_kd.items():
            if p["lo"] <= 0 <= p["hi"] and p["pairs"] >= POOL_MIN_PAIRS:
                self._pooled_dead.add((key, d))
                dead[key] = max(dead.get(key, 0.0), DEAD_PENALTY * POOL_DEAD_VOTES)
        return escalate, dead, spent

    # --- self-measurement: how long does this environment actually stay alive? --
    @staticmethod
    def _gen_ts(gid: str) -> Optional[float]:
        """generation ids embed their boot time: ego-<YYYYMMDDHHMMSS>-<pid>-<hex>."""
        try:
            core = str(gid).split("-")[1]
            return time.mktime(time.strptime(core[:14], "%Y%m%d%H%M%S"))
        except Exception:
            return None

    def _gen_lifetime_s(self) -> Optional[float]:
        """Median seconds a generation survives, measured from the ledger itself.

        This is the single number that decides whether a trial is even feasible:
        the gate voids anything spanning a restart, and this system restarts on its
        own every 20-100 minutes. Hardcoding a trial size would be guessing at an
        environment property the system can measure.
        """
        gens = []
        try:
            with open(self.ledger, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        g = json.loads(line).get("runtime_generation_id")
                    except Exception:
                        continue
                    if g and (not gens or gens[-1] != g):
                        gens.append(g)
        except Exception:
            return None
        ts = [t for t in (self._gen_ts(g) for g in gens) if t]
        if len(ts) < 2:
            return None
        d = sorted(b - a for a, b in zip(ts, ts[1:]) if 5 < (b - a) < 86400)
        if not d:
            return None
        return d[len(d) // 2]

    def _gen_room_s(self) -> Optional[float]:
        """Wall-clock left before this generation is likely to be replaced.

        `_gen_lifetime_s` answers "how long does a generation usually last";
        `_feasible` budgets against that full lifetime. But the question at
        tick N of an OLD generation is not "does a trial fit in 70 minutes",
        it is "does it fit in the 8 minutes that are left". Budgeting against
        the full lifetime is what let the scheduler start a 20-cell trial ten
        minutes before a restart and have it voided. The generation id embeds
        its own boot time, so the elapsed part is measurable without any new
        state. Returns None when either number is unknown (e.g. offline tests
        with a synthetic generation id), in which case the caller does not
        intervene.
        """
        life = self._gen_lifetime_s()
        boot = self._gen_ts(self.generation_id)
        if not life or boot is None:
            return None
        return life - max(0.0, time.time() - boot)

    @staticmethod
    def _cell_ticks(samples: int) -> int:
        """Ticks one cell can cost, for budgeting.

        `samples` readings, plus one more when the regime retry fires (it only
        fires when averaging a pair, so samples=1 is exact). Planning on
        `samples` alone under-counts by up to MAX/samples — and a budget that is
        optimistic is worse than useless, because the trial gets voided by the
        restart the arithmetic said it would beat.
        """
        if samples <= 1:
            return 1
        return min(MAX_SAMPLES_PER_CELL, samples + 1)

    @staticmethod
    def _tick_sec(st: Dict[str, Any]) -> Optional[float]:
        """Wall-clock per tick, robust to one runaway tick.

        The budget answers exactly one question: can 20 cells finish before this
        generation dies? A 0.7/0.3 EMA cannot answer it. Measured 2026-11-12 over
        59 consecutive ticks: median gap 60s, max 369s — but a single ~22-minute
        stall had dragged `tick_sec_ema` to 233s, so every trial got priced at
        20 x 233 = 4666s against a 2160s safe window and was refused, tick after
        tick, because of one bad tick. The scheduler sat idle while the machine
        was in fact fast enough to run two experiments that generation.

        A median over the recent window answers "how long does a tick usually
        take"; the EMA was answering "how bad was the worst thing lately", which
        is the wrong question for a capacity decision. The EMA is kept as the
        fallback until three real gaps exist.
        """
        recent = [float(x) for x in (st.get("tick_sec_recent") or [])
                  if isinstance(x, (int, float)) and x > 0]
        if len(recent) >= 3:
            return sorted(recent)[len(recent) // 2]
        ema = st.get("tick_sec_ema")
        return float(ema) if isinstance(ema, (int, float)) and ema > 0 else None

    @staticmethod
    def _trial_tick_sec(st: Dict[str, Any]) -> Optional[float]:
        """The clock a TRIAL will actually run on.

        Two clocks exist. With decision jobs being yielded to, a tick costs
        ~115s; with them running alongside, ~30s. A trial runs inside the quiet
        period, so its budget must be priced with the second clock. Pricing it
        with the first is what made the scheduler refuse trials it could easily
        have finished: measured 2026-11-12, "needs 2307s but only 2116s is safe"
        for a trial that would have taken 600s once the measurement began.

        The quiet gaps are only known after a quiet period has actually happened,
        so until then this falls back to the general estimate and the caller
        arranges for a short quiet probe (see _start).
        """
        quiet = [float(x) for x in (st.get("tick_sec_quiet_recent") or [])
                 if isinstance(x, (int, float)) and x > 0]
        if len(quiet) >= 3:
            return sorted(quiet)[len(quiet) // 2]
        return SelfExperimenter._tick_sec(st)

    def _choose_samples(self, st: Dict[str, Any]) -> Tuple[int, str]:
        """Pick samples-per-cell from the measured lifetime and tick cost.

        A trial needs 2*N_ARM cells; each cell costs `_cell_ticks(samples)` ticks.
        We spend at most 45% of a typical generation on one trial, so a restart is
        unlikely to land mid-experiment (and when it does, the trial is voided by
        design). samples=1 is a resolution sacrifice, not a free choice — it is
        what the environment's stability can pay for.
        """
        tick_s = self._trial_tick_sec(st)
        life = self._gen_lifetime_s()
        if not life or not tick_s or tick_s <= 0:
            # Nothing measured yet, so nothing can be budgeted. Start with the
            # cheapest valid shape: a 20-tick trial that finishes is worth
            # strictly more than a 40-tick one that gets voided by a restart,
            # and after two ticks the real numbers exist and later trials can
            # afford more samples.
            return 1, "no_history_default"
        budget_ticks = 0.45 * life / max(5.0, tick_s)
        per_cell = budget_ticks / max(1, 2 * N_ARM)
        if per_cell < 2:
            want = 1                          # no room even for one retry
        else:
            want = min(MAX_SAMPLES_PER_CELL - 1, int(per_cell) - 1)
        want = max(1, want)
        return want, (f"lifetime={life:.0f}s tick={tick_s:.0f}s "
                      f"budget={budget_ticks:.0f}ticks per_cell={per_cell:.1f}")

    def _feasible(self, st: Dict[str, Any], samples: int) -> Tuple[bool, str]:
        """Can a full 2*N_ARM-cell trial actually finish inside this lifetime?

        The gate hard-requires ten measured cells per arm, so the design cannot
        be shrunk — only refused. Starting a trial that measurably cannot finish
        wastes a whole generation: the restart voids it, the ledger gets no
        evidence, and the parameter sat off its baseline for nothing. That is
        the "is this worth trying" judgement, expressed as arithmetic on two
        numbers the system measures itself.
        """
        life = self._gen_lifetime_s()
        tick_s = self._trial_tick_sec(st)
        if not life or not tick_s or tick_s <= 0:
            return True, "no_history_yet"
        need_ticks = 2 * N_ARM * self._cell_ticks(samples)
        spend = need_ticks * max(5.0, tick_s)
        safe = 0.5 * life
        if spend <= safe:
            return True, f"{need_ticks}ticks x {tick_s:.0f}s = {spend:.0f}s < {safe:.0f}s"
        return False, (f"needs {spend:.0f}s but only {safe:.0f}s is safe inside a "
                       f"{life:.0f}s generation")

    # --- hypothesis proposal -------------------------------------------------
    def _cross_hints(self, escalate: Dict[Tuple[str, int, int], float],
                     dead: Dict[str, float],
                     spent: Dict[Tuple[str, int], float]
                     ) -> Dict[Tuple[str, int, int], Tuple[float, str]]:
        """Cross-ruler readings that survive what the current ruler already knows.

        Kept as its own method so the rule can be tested without re-implementing
        it in the test — a copy of the filter in the test would drift from the
        copy in the scheduler and stop guarding anything.

        Four things disqualify a cross-ruler reading:
          * the same ruler produced a hint for that cell (same ruler wins);
          * the same ruler already measured that cell (it has answered);
          * the lever is dead on the same ruler (flat at both ends);
          * that direction is spent on the same ruler (nothing left at max dose).
        """
        return {cell: val for cell, val in self._cross.items()
                if cell not in escalate
                and cell not in self._measured_now
                and cell[0] not in dead
                and (cell[0], cell[1]) not in spent}

    def request_conflict_trial(self, param, verified_value, override_value,
                               reason=""):
        """An operator override ran AGAINST pooled confirmed evidence.

        Instead of letting the operator channel and the evolve gate tug-of-war
        over the same lever (observed overnight 2026-11-13: an L5 task pushed
        floor 0.35->0.1 through the operator exemption, evolve pulled it back
        three times, each pull was overridden again), register the disagreement
        and let a tie-break RCT decide: control = the override value now live,
        intervention = the verified value the pooled evidence supports. climb
        rolls the override back; anything else and the override stands. No one
        loses the right to override — the disagreement just gets measured
        instead of re-fought.
        """
        try:
            v_new = float(override_value)
            v_old = float(verified_value)
        except (TypeError, ValueError):
            return False
        if abs(v_new - v_old) < 1e-9:
            return False
        d_override = 1 if v_new > v_old else -1
        pk = self._pooled_kd.get((param, -d_override)) or {}
        pk_lo, pk_hi, pk_eff = pk.get("lo"), pk.get("hi"), pk.get("effect")
        if not (isinstance(pk_eff, (int, float))
                and isinstance(pk_lo, (int, float))
                and isinstance(pk_hi, (int, float))
                and (pk_lo > 0 or pk_hi < 0) and pk_eff > 0):
            self.log(f"[self-exp] override {param} {v_old}->{v_new}: no "
                     "pooled confirmed evidence in the opposite direction - "
                     "no tie-break needed, override stands unmeasured")
            return False
        st = self._load()
        st["conflict_trial"] = {
            "parameter": param,
            "verified_value": v_old,
            "override_value": v_new,
            "pooled": {"effect": pk_eff, "lo": pk_lo, "hi": pk_hi,
                       "pairs": int(pk.get("pairs") or 0)},
            "ts": time.time(),
            "reason": str(reason)[:160],
        }
        self._save(st)
        self.log(f"[self-exp] CONFLICT: operator override {param} {v_old}->"
                 f"{v_new} contradicts pooled confirmed evidence "
                 f"(direction {-d_override:+d}: {pk_eff:+.4f} "
                 f"[{pk_lo:+.4f},{pk_hi:+.4f}]) -> tie-break RCT scheduled")
        return True

    def _propose(self, st: Dict[str, Any], tick: int) -> Optional[Dict[str, Any]]:
        params = self.get_params()
        # Tie-break for an operator-override-vs-evidence conflict (see
        # request_conflict_trial): control is the override value now live,
        # intervention is the verified value the pooled evidence supports.
        # climb rolls the override back via the normal adopt path; any other
        # verdict and the override stands. Consumed either way so the dispute
        # is measured once, not re-fought forever.
        conflict = st.get("conflict_trial")
        if conflict and not st.get("open"):
            _cp = conflict.get("parameter")
            _p = params.get(_cp) if isinstance(params, dict) else None
            if isinstance(_p, dict):
                try:
                    _cur = float(_p["current"])
                    _tgt = float(conflict.get("verified_value"))
                except (KeyError, TypeError, ValueError):
                    _cur = _tgt = None
                if _cur is not None and _tgt is not None \
                        and abs(_cur - _tgt) > 1e-9:
                    _d = 1 if _tgt > _cur else -1
                    _sig = f"{_cp}:{int(_d):+d}:0"
                    conflict["signature"] = _sig
                    self._save(st)
                    self.log(f"[self-exp] tie-break trial: override value "
                             f"{_cur} vs pooled-confirmed verified {_tgt} on "
                             f"{_cp} - the RCT decides")
                    return {"parameter": _cp,
                            "control_value": round(_cur, 6),
                            "intervention_value": round(_tgt, 6),
                            "signature": _sig,
                            "magnitude": 0.35}
            # consumed: parameter gone, or already back at the verified value
            st.pop("conflict_trial", None)
            self._save(st)
        history = st.get("history", [])
        # "done" must mean IMPROVED, not merely measured. A significantly negative
        # effect also passes the gate's CI test, but the gate then recommends the
        # control value and nothing changes. Treating that as done would retire a
        # lever after learning that it hurts.
        #
        # It also must not be permanent: once a lever improves, its new value is a
        # new baseline, and climbing further from there is exactly what we want.
        # So a win only sleeps the lever, it does not retire it.
        # "Won" has to mean "won something worth having" — a significant but
        # trivial gain (below TRIVIAL_EFFECT) is not a green light to keep
        # pushing, it is a sign this direction is done.
        def _real_win(h):
            return (self._verdict(h.get("signature"), h.get("effect"),
                                  bool(h.get("eligible"))) == "climb")
        done = {h.get("parameter") for h in history
                if _real_win(h)
                and tick - int(h.get("tick") or 0) < WIN_REEXPLORE_TICKS}
        win_sigs = {h.get("signature") for h in history
                    if h.get("signature") and _real_win(h)}
        # The baseline at which each signature was last run. Re-running a cell is
        # a *new* experiment only if the baseline moved since — the gate adopted
        # and this is a different position on the response curve. Comparing
        # against the baseline of the last WIN instead makes "it moved" true
        # forever after one adoption, and the scheduler then re-runs the same
        # cell every cooldown indefinitely (observed: the same lever probed at
        # 0.42 four times, all null, learning nothing).
        last_baseline: Dict[str, Any] = {}
        for h in history:
            if h.get("signature"):
                last_baseline[h["signature"]] = h.get("baseline")
        tried = {h.get("signature") for h in history if h.get("signature")}
        last_try: Dict[str, int] = {}
        for h in history:
            k = h.get("parameter")
            if k:
                last_try[k] = max(last_try.get(k, 0), int(h.get("tick") or 0))
        self._refresh_recent(tick)
        self._refresh_stats()
        escalate, dead, spent = self._learn(history)
        # A reading taken through a different instrument verifies nothing — only
        # the gate's same-ruler CI test does that — but it does say where to aim
        # the current instrument. Units do not transfer between rulers; Cohen's d
        # does. So each unresolved cross-ruler reading becomes a hint to re-run
        # that exact cell under the current metric, at a bonus above a same-ruler
        # dose bump: re-measuring an already-suspected effect with a ruler 10x
        # sharper is strictly more informative than escalating a lever that ruler
        # has already read.
        #
        # It must not, however, resurrect what the current ruler has retired.
        # The hint tier overwrites theta outright (that is the point of having a
        # tier), so without that guard a stale cross-ruler reading would keep
        # nominating a lever the sharp instrument has already pronounced flat at
        # both ends of its range. Same ruler wins, always.
        cross = self._cross_hints(escalate, dead, spent)
        for cell, (dval, _metric) in cross.items():
            # Rank by |d| so the best-evidenced lever wins the tier outright.
            # Without the spread every cross-ruler hint ties at the same bonus
            # and the winner is whichever key the dict happened to yield first.
            escalate[cell] = CROSS_METRIC_BONUS + min(0.10, abs(dval) * 0.10)
        if escalate:
            self.log(f"[self-exp] ladder hints: "
                     + ", ".join(f"{k}:{d:+d}:{b}" for (k, d, b) in sorted(escalate)))
        if cross:
            self.log("[self-exp] cross-ruler retries (d measured on another ruler): "
                     + ", ".join(f"{k}:{d:+d}:{b}(d={v[0]:+.2f}@{v[1]})"
                                 for (k, d, b), v in
                                 sorted(cross.items(), key=lambda t: -abs(t[1][0]))))
        if dead:
            self.log(f"[self-exp] flat at max dose, deprioritising: "
                     + ", ".join(sorted(dead)))
        _pc = []
        for (k, d, b), p in sorted(self._pooled.items(),
                                   key=lambda t: -abs(t[1]["effect"])):
            if p["lo"] > 0 or p["hi"] < 0:
                tag = "confirmed"
            elif p["pairs"] >= POOL_MIN_PAIRS:
                tag = "retired"
            else:
                continue
            _pc.append(f"{k}:{d:+d}:{b}x{int(p['trials'])}="
                       f"{p['effect']:+.4f}[{p['lo']:+.4f},{p['hi']:+.4f}] {tag}")
        if _pc:
            self.log("[self-exp] pooled repeats: " + ", ".join(_pc))
        if spent:
            self.log("[self-exp] spent directions: "
                     + ", ".join(f"{k}:{d:+d}" for (k, d) in sorted(spent)))

        best = None
        retired_cands = []
        hinted = None
        for key, p in params.items():
            if not isinstance(p, dict) or key in PID_OWNED or key in done:
                continue
            if key in self._recent:
                continue                    # another writer owns it right now
            try:
                lo, hi = float(p["min"]), float(p["max"])
                cur = float(p["current"])
            except (KeyError, TypeError, ValueError):
                continue
            span = hi - lo
            if span <= 0:
                continue
            # "Have we ever moved this lever?" must count both sources: the
            # ledger (every completed trial) and this module's own history.
            # Counting only the ledger would let a stale ledger block a dose
            # escalation the history has already justified.
            # "Have we ever touched this lever?" is a weaker question than "how
            # often has it won", and it has to be answered from every completed
            # trial — including the ones the current ruler could not resolve.
            # A lever measured once by a blunt instrument is not unexplored; it
            # is explored and unresolved, so it may take a larger step. Using
            # the decisive-only count here would force every re-measurement back
            # to the smallest bin and throw away the magnitude that was already
            # known to be safe.
            pulls_here = (int(self._attempts.get(key, 0))
                          + sum(1 for h in history
                                if (self._parse_sig(h.get("signature")) or ("", 0, 0))[0] == key))
            for d in (+1.0, -1.0):
                for bi, frac in enumerate(MAG_BINS):
                    if pulls_here == 0 and bi > 0:
                        continue            # unexplored lever: probe conservatively
                    target = max(lo, min(hi, cur + d * frac * span))
                    if abs(target - cur) < max(0.02, 0.05 * span):
                        continue            # rail: this direction cannot move
                    sig = f"{key}:{int(d):+d}:{bi}"
                    # A win only sleeps (climb it again soon); a null sleeps long.
                    sleep = WIN_REEXPLORE_TICKS if sig in win_sigs else REEXPLORE_TICKS
                    if sig in tried and tick - last_try.get(key, 0) < sleep:
                        continue
                    if sig in last_baseline:
                        prev_b = last_baseline[sig]
                        if (isinstance(prev_b, (int, float))
                                and abs(cur - float(prev_b)) <= 1e-9):
                            continue    # same cell at the same baseline: nothing to learn
                    if (key, int(d)) in self._pooled_dead:
                        # Retired by pooled evidence. Kept as a last resort so
                        # the scheduler can never idle itself to death - but the
                        # check above comes FIRST, so a retired cell can still
                        # not be re-run at a baseline it has already been run
                        # at. Without that order the fallback path re-ran the
                        # same cell three times (measured 2026-11-12).
                        retired_cands.append((key, d, bi, frac, target, sig))
                        continue
                    n, w = self._stats.get((key, int(d), bi), (0, 0))
                    theta = random.betavariate(1 + w, 1 + (n - w))
                    if n == 0:
                        theta += 0.015      # explore the unknown a little
                    theta += PRIORITY.get(key, 0.0)
                    # What the last result on this lever actually told us: climb the
                    # dose where it was right-signed, and stop paying for knobs
                    # that are flat at both ends of their range.
                    hint = escalate.get((key, int(d), bi), 0.0)
                    theta += hint
                    theta -= dead.get(key, 0.0)
                    theta -= spent.get((key, int(d)), 0.0)
                    if hint > 0 and (hinted is None or hint > hinted[0]):
                        hinted = (hint, theta, key, d, frac, target, sig, n, w)
                    if best is None or theta > best[0]:
                        best = (theta, key, d, frac, target, sig, n, w)

        # A hint outranks the sampling draw, always.
        #
        # Thompson sampling treats every unmeasured cell as Beta(1,1) = Uniform,
        # and with a few hundred cells in play the best of those draws sits near
        # 0.999. No additive bonus survives that: measured live, a +0.30 hint
        # lost to a lucky 0.9953 prior draw and the scheduler ignored the very
        # conclusion it had just paid a generation for. A hint is not a competing
        # arm, it is the next step implied by a finished experiment, so it gets
        # its own tier and the sampler only decides when nothing is implied.
        # This does not stop exploration: a hinted cell self-consumes (once run at
        # this baseline it cannot run again here), so the next proposal falls back
        # to the sampler.
        if hinted is not None:
            best = hinted[1:]
        if best is None:
            if not retired_cands:
                return None
            # Rotate through the retired cells instead of always taking the
            # first one: a fixed fallback re-measures the same cell forever.
            key, d, bi, frac, target, sig = self._rng.choice(retired_cands)
            self.log(f"[self-exp] every candidate is retired by pooled evidence - "
                     f"falling back to {sig} rather than idling")
            best = (0.0, key, d, frac, target, sig, 0, 0)
        theta, key, d, frac, target, sig, n, w = best
        p = params[key]
        return {"parameter": key, "control_value": round(float(p["current"]), 6),
                "intervention_value": round(target, 6), "signature": sig,
                "theta": round(theta, 4), "pulls": n, "wins": w,
                "magnitude": round(frac, 3)}

    # --- trial lifecycle -----------------------------------------------------
    def _start(self, st: Dict[str, Any], tick: int) -> None:
        samples, samples_why = self._choose_samples(st)
        ok, why = self._feasible(st, samples)
        defers = int(st.get("defer_streak") or 0)
        # No measurement of the quiet clock yet? Go quiet for a few ticks and
        # measure it, instead of pricing the trial with the clock of a machine
        # busy thinking. Measured 2026-11-12: the busy clock said "needs 2307s
        # but only 2116s is safe" for a trial that takes ~600s once the
        # measurement starts. Deferring on that number was refusing a runnable
        # experiment - and each deferral cost 30 ticks, so three of them cost
        # 90 ticks before the trial would start anyway.
        quiet_known = len(st.get("tick_sec_quiet_recent") or []) >= 3
        # Measure the quiet clock BEFORE pricing the trial, not only when the
        # busy clock says the trial does not fit. The sample size is derived
        # from the same clock, and a trial priced with the busy clock came out
        # at samples=1 when the quiet clock would have paid for samples=2 -
        # halving the noise, doubling Cohen's d. Paying four ticks to know
        # which ruler to use is the cheapest thing in this whole loop.
        probes = int(st.get("quiet_probe_count") or 0)
        if not quiet_known and probes < QUIET_PROBE_ATTEMPTS:
            st["quiet_probe"] = True
            st["quiet_probe_count"] = probes + 1
            st["next_start_tick"] = tick + QUIET_PROBE_TICKS
            self._save(st)
            self._set_quiet(st)
            self.log(f"[self-exp] measuring the clock the trial will run on - "
                     f"going quiet for {QUIET_PROBE_TICKS} ticks "
                     f"(busy-clock estimate said: {why})")
            return
        st["quiet_probe"] = False
        # Only a trial that FITS a whole generation, but no longer fits THIS
        # one, waits. When even a fresh generation cannot fit it, the existing
        # defer -> force path stays in charge, so the scheduler can never wait
        # forever for a generation that would make a trial affordable.
        if ok:
            tick_s = self._trial_tick_sec(st)
            room = self._gen_room_s()
            if tick_s and room is not None:
                need = (2 * N_ARM * self._cell_ticks(samples)
                        * max(5.0, tick_s) * GEN_ROOM_MARGIN)
                if need > room:
                    st["defer_streak"] = 0
                    st["next_start_tick"] = tick + GEN_WAIT_TICKS
                    if st.get("gen_wait_announced") != self.generation_id:
                        st["gen_wait_announced"] = self.generation_id
                        self.log(f"[self-exp] only {room:.0f}s left in this generation, "
                                 f"a trial needs {need:.0f}s - waiting for the next "
                                 "generation instead of starting one that will be voided")
                    self._save(st)
                    return
        if not ok and defers < MAX_DEFERS:
            # Waiting is only rational while there is a chance the environment gets
            # faster; ticks are dominated by background jobs we do not control, so
            # after MAX_DEFERS deferrals start anyway and find out for real.
            st["defer_streak"] = defers + 1
            st["next_start_tick"] = tick + 30
            self._save(st)
            self.log(f"[self-exp] deferring ({defers + 1}/{MAX_DEFERS}): {why}")
            return
        if not ok:
            self.log(f"[self-exp] forcing after {defers} deferrals: {why}")
        st["defer_streak"] = 0
        pick = self._propose(st, tick)
        if pick is None:
            st["next_start_tick"] = tick + 60
            self._save(st)
            return
        if not ok:
            samples = 1                   # cheapest shape that can still be valid
            samples_why = "forced_minimum"
        try:
            trial = C.preregister(
                self.workspace, parameter=pick["parameter"],
                control_value=pick["control_value"],
                intervention_value=pick["intervention_value"],
                n_arm=N_ARM, seed=random.randrange(2 ** 31),
                runtime_generation_id=self.generation_id,
                outcome_metric=OUTCOME_METRIC)
        except Exception as exc:
            self.log(f"[self-exp] preregister failed: {exc}")
            st["next_start_tick"] = tick + 60
            self._save(st)
            return
        st["open"] = {
            "trial_id": trial["trial_id"],
            "parameter": pick["parameter"],
            "signature": pick["signature"],
            "control_value": pick["control_value"],
            "intervention_value": pick["intervention_value"],
            "started_tick": tick,
            "started_wall": time.time(),
            "samples_per_cell": samples,
            "phase": "apply",
            "applied": None,
            "measure_at": 0,
            "cells": 0,
        }
        self._save(st)
        self._set_quiet(st)
        self.log(f"[self-exp] started {trial['trial_id']} "
                 f"{pick['parameter']} {pick['control_value']}->{pick['intervention_value']} "
                 f"(mag={pick.get('magnitude')} pulls={pick.get('pulls')} wins={pick.get('wins')} "
                 f"theta={pick.get('theta')}) n_arm={N_ARM} samples={samples} [{samples_why}]")

    def _next_arm(self, cur: Dict[str, Any]) -> Optional[str]:
        try:
            trial = C.load_trial(self.workspace, cur["trial_id"])
        except Exception:
            return None
        if not trial:
            return None
        if self._rng_trial != cur["trial_id"]:
            # One persistent stream per trial: a fresh Random(seed) per cell would
            # replay the same first draw and keep handing back the same arm.
            self._rng = random.Random(int(trial.get("seed", 0)) ^ 0x5EED)
            self._rng_trial = cur["trial_id"]
        return C.next_assignment(trial, self._rng)

    def _record(self, st: Dict[str, Any], tick: int) -> None:
        cur = st["open"]
        state = self.get_state() or {}
        conc = state.get("last_concentration", state.get("resting_concentration"))
        comp = state.get("last_competition", state.get("resting_competition"))
        out = fitness_stable(conc, comp)
        if out is None:
            st["open"]["phase"] = "apply"
            self._save(st)
            return

        # Average consecutive ticks into one cell, and prefer a pair that shares the
        # same regime.
        #
        # Measured on the newborn_inject_n trial (2026-11-12 08:00): within-pair SD
        # was 0.030 while every real effect we have ever seen is 0.01-0.13. The
        # noise is not white — `dominant` flips between veto/integrator on
        # consecutive ticks, swinging comp between ~0.93 and ~0.98. Averaging two
        # consecutive ticks samples both phases and halves the SD; when the two
        # samples land in *different* regimes (|Δcomp| > REGIME_TOL) we spend two
        # more ticks and keep the closest pair, which is the cheapest way to avoid
        # averaging a swing instead of cancelling it.
        comp = comp if isinstance(comp, (int, float)) else None
        samples = cur.get("samples_per_cell") or SAMPLES_PER_CELL
        samples_list = cur.get("samples") or []
        samples_list.append({"out": float(out), "comp": comp})
        agree = (len(samples_list) >= 2
                 and samples_list[-1]["comp"] is not None
                 and samples_list[-2]["comp"] is not None
                 and abs(samples_list[-1]["comp"] - samples_list[-2]["comp"]) <= REGIME_TOL)
        # Two different rules, and conflating them cost 4x on every cheap trial:
        #   1. collect at least `samples` readings;
        #   2. only when we were going to AVERAGE a pair does the regime check
        #      apply — a single sample has no pair, so `agree` is vacuously False
        #      and the old combined condition always ran the cell up to
        #      MAX_SAMPLES_PER_CELL. Measured live: samples=1 was taking 4 ticks
        #      per cell, so a "20-tick" trial was really 80 ticks and every
        #      feasibility estimate was off by 4.
        if len(samples_list) < max(1, samples):
            cur["samples"] = samples_list
            cur["phase"] = "measure"
            cur["measure_at"] = tick + 1
            self._save(st)
            return
        if samples >= 2 and not agree and len(samples_list) < MAX_SAMPLES_PER_CELL:
            cur["samples"] = samples_list
            cur["phase"] = "measure"
            cur["measure_at"] = tick + 1
            self._save(st)
            return
        best = None
        for i in range(len(samples_list) - 1):
            a, b = samples_list[i]["comp"], samples_list[i + 1]["comp"]
            d = abs(a - b) if (a is not None and b is not None) else 9e9
            if best is None or d < best[0]:
                best = (d, i)
        i = best[1] if best else max(0, len(samples_list) - 2)
        j = min(len(samples_list) - 1, i + max(1, samples - 1))
        pair = samples_list[i:j + 1]
        cell = round(sum(s["out"] for s in pair) / len(pair), 6)
        cur["samples"] = []
        cur["spread"] = round(best[0], 4) if best else None

        try:
            res = C.record_arm(self.workspace, cur["trial_id"], cur["pending_arm"],
                               outcome=cell, match_key=f"self{tick}", tick=tick)
        except Exception as exc:
            self.log(f"[self-exp] record failed: {exc}")
            st["open"]["phase"] = "apply"
            self._save(st)
            return
        if res.get("ok") is False:
            # Silent no-ops here would let a closed trial keep "measuring" forever
            # while the ledger holds nothing — the exact fake-certainty this module
            # exists to prevent.
            self.log(f"[self-exp] record rejected: {res.get('reason')}")
        cur["cells"] = int(res.get("recorded", cur["cells"] + 1))
        cur["phase"] = "apply"
        self._save(st)
        if res.get("status") == "complete":
            self._finish(st, tick)

    def _finish(self, st: Dict[str, Any], tick: int) -> None:
        cur = st["open"]
        tid = cur["trial_id"]
        try:
            res = C.analyze(self.workspace, tid)
        except Exception as exc:
            self.log(f"[self-exp] analyze failed: {exc}")
            res = {"eligible": False, "reason": f"analyze_error:{exc}"}
        key = cur["parameter"]
        effect = res.get("effect_size")
        eligible = bool(res.get("eligible"))
        verdict = self._verdict(cur.get("signature"), effect, eligible)
        st.setdefault("history", []).append({
            "parameter": key, "signature": cur.get("signature"), "effect": effect,
            "ci95": res.get("effect_ci95"), "eligible": eligible,
            "reason": res.get("reason"), "tick": tick,
            "verdict": verdict,
            "baseline": cur["control_value"],
            "probed": cur["intervention_value"],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        # Hand the parameter back to baseline: from here on only the gate may move
        # it. Leaving an experimental value live would let the trial contaminate
        # the very next trial's control arm.
        self._apply(key, cur["control_value"])
        st["open"] = None
        # Tie-break resolution: if this trial was scheduled to arbitrate an
        # operator-override-vs-evidence conflict, the verdict IS the ruling.
        _conf = st.get("conflict_trial") or {}
        if _conf.get("signature") == cur.get("signature"):
            st.pop("conflict_trial", None)
            if verdict == "climb":
                self.log(f"[self-exp] tie-break verdict: evidence upheld - "
                         f"override rolled back, {key} returns to the "
                         f"verified value")
            else:
                self.log(f"[self-exp] tie-break verdict: override upheld "
                         f"({verdict}) - the operator channel may know "
                         f"something the pooled evidence does not")
        # A win needs a longer gap than a null: the gate must finish adopting the
        # new value before the next trial measures against it as its control.
        st["next_start_tick"] = tick + (WIN_COOLDOWN_TICKS if verdict == "climb"
                                        else START_COOLDOWN)
        st["quiet_probe"] = False
        self._set_quiet(st)
        self._save(st)
        self.log(f"[self-exp] done {tid} verdict={verdict} eligible={eligible} "
                 f"effect={effect} ci95={res.get('effect_ci95')} "
                 f"reason={res.get('reason')}")

    @staticmethod
    def _verdict(sig: Any, effect: Any, eligible: bool) -> str:
        """One word for what this trial taught us.

        This is the classification `_learn` consumes, kept as a single function
        so the label written to history and the scheduling decision derived
        from it can never drift apart.

            climb              eligible, effect > 0  -> same lever, climb
            trivial            eligible but the gain is below TRIVIAL_EFFECT
                               -> this direction is spent, not a green light
            hurts              eligible, effect <= 0 -> turn around
            underpowered       effect > 0, CI touched 0 -> same lever, more dose
            weak_hurts         effect < 0, CI touched 0 -> turn around, gently
            flat               |effect| near 0, not the last rung -> bigger rung
            flat_at_max_dose   |effect| near 0 at the top rung -> lever is dead
            no_reading         analysis produced no number
        """
        if not isinstance(effect, (int, float)):
            return "no_reading"
        if eligible:
            if effect <= 0:
                return "hurts"
            return "climb" if effect >= TRIVIAL_EFFECT else "trivial"
        if abs(effect) < MIN_SIGNAL:
            parsed = SelfExperimenter._parse_sig(sig)
            if parsed and parsed[2] >= len(MAG_BINS) - 1:
                return "flat_at_max_dose"
            return "flat"
        return "underpowered" if effect > 0 else "weak_hurts"

    def _set_quiet(self, st: Dict[str, Any], want: Optional[bool] = None) -> None:
        """Tell the server to stop yielding ticks while a trial is open.

        Measured 2026-11-12. The resting loop yields to background decision
        jobs ("[heartbeat] decision in flight - resting tick yielding"), so a
        tick costs 34s on a quiet machine and up to 170s while a job runs. That
        is not only slow: a yielded tick is a MISSING measurement, and the job
        that causes the skip is also writing evidence - which is exactly what
        evidence_persist_ticks governs. Measuring that lever with the
        confounder running is measuring the wrong thing.

        Two costs are paid for keeping ticks running during a trial:
          * speed - two readings per cell become affordable (noise halves, so
            Cohen's d doubles), which the budget otherwise refuses;
          * validity - the measured arms are no longer contaminated by a
            background process whose activity differs between them.
        The decision job is not the object of study, so it may compete for the
        same cores; it must not decide WHEN the measurement happens.

        `want=None` means "follow the state": quiet while a trial is open, or
        while a short quiet probe is running (see _start).
        """
        if want is None:
            want = bool(st.get("open") or st.get("quiet_probe"))
        try:
            state = self.get_state()
        except Exception:
            return
        # 2026-11-12 18:28: L5 goal l5g-20261112-071767 wrote
        # integrator_concentration_floor 0.3 -> 0.1 via /apply-parameter WHILE
        # cab_type_dominance_cap_1794478304 was measuring. The first 8 matched
        # pairs were a clean +0.05 (control ~0.74, intervention ~0.79); after
        # the write the last 3 pairs collapsed to 0.53-0.62 in BOTH arms and the
        # paired CI went from excluding zero to [-0.0213, +0.0626]. An
        # unmeasured heuristic edit destroyed a climb that was already there.
        # A quiet period stops ticks from being yielded; it does NOT stop a
        # foreign writer from changing the machine under the measurement. That
        # needs a separate flag: armed only while a trial is actually open (a
        # quiet probe is not a trial, so it must not block anything).
        armed = bool(st.get("open"))
        if state.get("self_experiment_armed") != armed:
            try:
                state["self_experiment_armed"] = armed
                self.log(f"[self-exp] parameter guard "
                         f"{'ARMED (trial open)' if armed else 'released'}"
                         " - foreign runtime writes blocked while measuring")
            except Exception:
                pass
        if state.get("self_experiment_quiet") == want:
            return
        try:
            state["self_experiment_quiet"] = want
            self.log(f"[self-exp] quiet period {'ON (trial open)' if want else 'OFF'}"
                     " - resting ticks stop yielding to decision jobs")
        except Exception:
            pass

    def _void(self, st: Dict[str, Any], why: str, tick: int) -> None:
        cur = st.get("open")
        if not cur:
            return
        row = {"schema": "causal_ab_arm.v1", "trial_id": cur["trial_id"],
               "status": "void", "parameter": cur["parameter"],
               "voided_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "voided_tick": tick, "reason": why,
               "runtime_generation_id": cur.get("generation", self.generation_id)}
        try:
            C._append_ledger(self.workspace, row)
        except Exception:
            pass
        # A generation change means the PROCESS restarted. Boot replay has
        # already restored the authoritative baseline from provenance
        # (tl_ego_server._load_runtime_params / _replay_verified_value).
        # Restoring control_value here would write back the experimental arm
        # that happened to be live when the old process died, undoing that
        # recovery. Measured 2026-11-12 17:51: the 0.425 intervention arm
        # survived a restart this way, and the next trial opened with
        # control=0.425 - spending a full RCT to rediscover the already
        # verified 0.3. Every other void reason happens inside the live
        # generation, where the control arm IS still the right baseline.
        if why == "generation_changed":
            self.log(f"[self-exp] void {cur['trial_id']}: {why} "
                     "(keeping the boot-restored baseline; stale control arm "
                     f"{cur.get('control_value')} not restored)")
        else:
            self._apply(cur["parameter"], cur["control_value"])
            self.log(f"[self-exp] void {cur['trial_id']}: {why}")
        st["open"] = None
        st["quiet_probe"] = False
        st["next_start_tick"] = tick + 5
        self._set_quiet(st)

    # --- the tick ------------------------------------------------------------
    def tick(self, tick: int) -> None:
        if (self.workspace / ".lae" / OFF_FILE).exists():
            return
        st = self._load()
        if st.get("open"):
            # Belt and braces: a trial opened by another process (or before a
            # restart) still means the measurement must not be starved.
            self._set_quiet(st)
        # Self-measurement: observed tick duration is what decides how large an
        # experiment can be. Measure it rather than assume it.
        prev_tick, prev_wall = st.get("last_tick"), st.get("last_wall")
        if isinstance(prev_tick, int) and tick > prev_tick and isinstance(prev_wall, (int, float)):
            dt = time.time() - prev_wall
            if 0 < dt < 7200:
                ema = st.get("tick_sec_ema")
                st["tick_sec_ema"] = round(
                    dt if not isinstance(ema, (int, float)) else 0.7 * ema + 0.3 * dt, 2)
                # Keep the raw samples too: the budget uses their median, because
                # one 20-minute stall must not be allowed to price every trial out
                # of feasibility (see _tick_sec).
                hist = [x for x in (st.get("tick_sec_recent") or [])
                        if isinstance(x, (int, float))]
                hist.append(round(dt, 2))
                st["tick_sec_recent"] = hist[-12:]
                # The same gap, but only when the quiet period was on. A trial
                # runs inside the quiet period, so this is the clock its budget
                # has to be priced with - see _trial_tick_sec.
                if st.get("open") or st.get("quiet_probe"):
                    qhist = [x for x in (st.get("tick_sec_quiet_recent") or [])
                             if isinstance(x, (int, float))]
                    qhist.append(round(dt, 2))
                    st["tick_sec_quiet_recent"] = qhist[-12:]
        st["last_tick"] = tick
        st["last_wall"] = time.time()
        if st.get("generation") != self.generation_id:
            # Generation is the gate's identity: anything measured under the old one
            # is unreachable, so a half-finished trial is pure noise and must be
            # voided. Learned history is a different thing — it is knowledge about
            # the levers, not evidence, and it must survive the restart. Wiping it
            # made the scheduler re-run the identical doomed experiment after every
            # crash (observed: alpha_seed_mix tested with the same +0.32 delta twice).
            if st.get("open"):
                self._void(st, "generation_changed", tick)
            st["generation"] = self.generation_id
            # The quiet tick clock is a property of the LIVE machine, not of
            # accumulated knowledge, so it must not survive a restart. Measured
            # 2026-11-12 16:33: a fresh generation priced its first trial with
            # the previous generation's busy clock (tick=100s, samples=1) while
            # the new machine was in fact running at ~30s, where samples=2 was
            # affordable. `_start` only probes when the clock is unknown, and a
            # stale 12-slot window made it look known. Clearing forces one short
            # re-measurement at the start of every generation.
            st["tick_sec_quiet_recent"] = []
            st["quiet_probe_count"] = 0
            st["gen_wait_announced"] = None
            st["next_start_tick"] = tick + 3
            self._save(st)
            return

        cur = st.get("open")
        if not cur:
            if tick < int(st.get("next_start_tick", 0)):
                self._save(st)                # persist tick-duration EMA while idle
                return
            self._start(st, tick)
            return

        if tick - int(cur.get("started_tick", tick)) > MAX_OPEN_TICKS:
            self._void(st, "budget_exceeded", tick)
            return
        begun = cur.get("started_wall")
        if isinstance(begun, (int, float)) and time.time() - begun > MAX_OPEN_S:
            # Ticks can take 20+ minutes each (measured dt=133s/168s, worst 1335s).
            # A tick budget alone cannot bound a trial in wall time, and an
            # unbounded trial holds a parameter off its baseline.
            self._void(st, "wallclock_budget", tick)
            return

        if cur.get("phase") == "measure":
            if tick < int(cur.get("measure_at", 0)):
                return
            self._record(st, tick)
            st = self._load()
            cur = st.get("open")
            if not cur:
                return
            # Fall through and apply the next arm on this same tick: one cell per
            # tick instead of two halves the exposure to an unplanned restart,
            # which is the dominant failure mode here (4 boots in 3 hours).

        # phase == apply: choose the arm the design says comes next, apply it,
        # then wait SETTLE_TICKS before reading the outcome.
        arm = self._next_arm(cur)
        if arm is None:
            self._finish(st, tick)
            return
        value = cur["control_value"] if arm == "control" else cur["intervention_value"]
        landed = self._apply(cur["parameter"], value)
        if landed is None:
            self._void(st, "apply_failed", tick)
            return
        if arm == "intervention" and abs(landed - value) > 1e-6:
            # Clamped into the control value: this cell carries no perturbation.
            self._apply(cur["parameter"], cur["control_value"])
            self._save(st)
            return
        cur["pending_arm"] = arm
        cur["phase"] = "measure"
        cur["measure_at"] = tick + SETTLE_TICKS
        cur["applied"] = landed
        self._save(st)

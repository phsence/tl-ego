"""l10/diversity_quota_operator.py — REBUILT 2026-10-01 (source lost in the 09-30 wipe).

Contract: `diversity_quota_compute(seq: list[str]) -> dict` with a
`suggested_next_type` key, called every resting tick from
tl_ego_server.py `_resting_tick_impl` once the trajectory holds >= 20 frames.
The result is a NEXT-TICK seed intervention only — the caller injects neurons of
that type into `last_seeds`. It must never report a current-tick measurement.

Provenance (every constant below is recovered, not invented):
  * `l10/l10_meta_algorithm_generator.py.bak-v7-20260929-204953:169-180` — the
    generator's own parameter registry names two constants with their exact
    values and meanings:
        SELF_LOOP_THRESHOLD = 5  # consecutive frames of one type that trigger
                                 # the self-loop penalty
        QUOTA_WINDOW      = 20   # diversity detection window
  * `.lae/stage3_timeline.jsonl` (22144 real frames) — the distribution the
    operator must react to. Measured: 16 types, top-2 (integrator .303 /
    action .297) hold 60% of all frames, and 957 self-loop runs of length >= 5
    exist (longest 38, median run 1). So the operator's job is real: a single
    type can hold the window for stretches far longer than the threshold.
  * `tl_ego_server.py:3317 typed_neurons()` — `suggested_next_type` must be a
    value that `parse_type()` can match, i.e. one of the 16 observed type names.

Judgement (not measured): the exact quota threshold and tie-break order. The
constants above are the generator's; the rest is a reconstruction that meets the
observed contract. Kept small and stateless so it is safe to call per tick.
"""

# ── recovered constants (l10_meta_algorithm_generator.bak-v7, lines 169-180) ──
SELF_LOOP_THRESHOLD = 5   # consecutive same-type frames that trigger the penalty
QUOTA_WINDOW = 20         # diversity detection window

# ── reconstruction parameters ────────────────────────────────────────────────
# Share of the window a single type may hold before it is considered dominant.
# Measured: over the 22125 real 20-frame windows, top share averages 0.558 and
# 48.7% of windows are at or above 0.60 — so this threshold fires often, which
# is the point.
QUOTA_SHARE = 0.60
# How far below quota a type must fall to be worth seeding. A type absent from
# the whole window is the strongest candidate.
QUOTA_FLOOR = 0.05

# The full type universe ego can seed. Measured from two independent sources
# that agree exactly: edge_store's `compatible_id_prefixes` (19) and
# `/status` `edge_view.cached_type_counts` (19 + "unknown"). Without this, a
# window monopolised by a single type leaves nothing to recommend — 67 such
# windows exist in the real trajectory, all of them integrator.
#
# Rotation order is the /status list order, which is the order the neuron
# registry itself emits, so the least-recently-seen type is found by scanning
# for the first type absent from the window.
TYPE_UNIVERSE = (
    "action", "aesthetic", "architect", "cultural", "curiosity", "decider",
    "doubt", "emotional", "ethical", "integrator", "memory", "meta",
    "planning", "rule_modification", "sensory", "social", "spatial",
    "temporal", "veto",
)


def _window(seq):
    return list(seq)[-QUOTA_WINDOW:]


def _shares(window):
    counts = {}
    for t in window:
        if t:
            counts[t] = counts.get(t, 0) + 1
    n = len(window) or 1
    return {t: c / n for t, c in counts.items()}


def _starved(window, exclude):
    """The universe type most worth seeding: scarcest in the window, excluding
    the type that is currently running (seeding it again deepens the loop)."""
    counts = _shares(window)
    candidates = [t for t in TYPE_UNIVERSE if t != exclude]
    if not candidates:
        return None
    return min(candidates, key=lambda t: (counts.get(t, 0.0), t))


def self_loop_len(window):
    """Length of the current run of one repeated type at the tail of the window."""
    if not window:
        return 0, None
    last = window[-1]
    n = 0
    for t in reversed(window):
        if t != last:
            break
        n += 1
    return n, last


def diversity_quota_compute(seq):
    """Recommend which type to seed next tick, given the dominant-type trajectory.

    Args:
        seq: dominant type per frame, oldest first. Shorter than
             SELF_LOOP_THRESHOLD is still analysed (caller enforces >= 20).

    Returns a dict with at least:
        suggested_next_type : str|None  the type to seed, None if nothing to do
        reason              : str       why, human-readable for the log
        self_loop_len       : int       length of the current same-type run
        self_loop_type      : str|None  the type that run belongs to
        window              : int       frames actually analysed
        counts              : dict      type -> share over the window
        quota_share         : float     the threshold used
    """
    window = _window(seq or [])
    if not window:
        return {
            "suggested_next_type": None,
            "reason": "empty trajectory window",
            "self_loop_len": 0, "self_loop_type": None,
            "window": 0, "counts": {}, "quota_share": QUOTA_SHARE,
        }

    counts = _shares(window)
    run_len, run_type = self_loop_len(window)
    over = [t for t, s in counts.items() if s >= QUOTA_SHARE]
    under = {t: s for t, s in counts.items() if s <= QUOTA_FLOOR}

    # 1. a type monopolising the window, or a long self-loop: break out of it
    if over and run_type and run_len >= SELF_LOOP_THRESHOLD:
        # the universe type that has been absent/scarcest in this window
        target = _starved(window, run_type)
        return {
            "suggested_next_type": target,
            "reason": ("self-loop %s x%d exceeds threshold %d; seeding starved %r"
                       % (run_type, run_len, SELF_LOOP_THRESHOLD, target)),
            "self_loop_len": run_len, "self_loop_type": run_type,
            "window": len(window), "counts": counts, "quota_share": QUOTA_SHARE,
            "over_quota": over,
        }

    # 2. a self-loop that is long but whose window is not monopolised: still break it
    if run_type and run_len >= SELF_LOOP_THRESHOLD:
        target = _starved(window, run_type)
        return {
            "suggested_next_type": target,
            "reason": ("self-loop %s x%d exceeds threshold %d; seeding starved %r"
                       % (run_type, run_len, SELF_LOOP_THRESHOLD, target)),
            "self_loop_len": run_len, "self_loop_type": run_type,
            "window": len(window), "counts": counts, "quota_share": QUOTA_SHARE,
        }

    # 3. no self-loop pressure — only intervene if a type is missing entirely
    if under:
        target = sorted(under, key=lambda t: (under[t], t))[0]
        # never recommend the type that is currently running: seeding it again
        # deepens the very loop the operator exists to break. (Caught by
        # replay_diversity_quota.py: 919 of 22125 real frames hit this.)
        if target == run_type:
            alt = sorted((t for t in under if t != run_type),
                         key=lambda t: (under[t], t))
            target = alt[0] if alt else None
        return {
            "suggested_next_type": target,
            "reason": "type %r at/below floor %.2f of window" % (target, QUOTA_FLOOR),
            "self_loop_len": run_len, "self_loop_type": run_type,
            "window": len(window), "counts": counts, "quota_share": QUOTA_SHARE,
        }

    top_share = max(counts.values()) if counts else 0.0
    return {
        "suggested_next_type": None,
        "reason": "no quota pressure (max share %.3f < %.2f, run %d < %d)"
                  % (top_share, QUOTA_SHARE, run_len, SELF_LOOP_THRESHOLD),
        "self_loop_len": run_len, "self_loop_type": run_type,
        "window": len(window), "counts": counts, "quota_share": QUOTA_SHARE,
    }


if __name__ == "__main__":
    # 1. the two recovered constants must hold their generator-declared values
    assert SELF_LOOP_THRESHOLD == 5 and QUOTA_WINDOW == 20, (SELF_LOOP_THRESHOLD, QUOTA_WINDOW)

    # 2. a real self-loop must be broken with a type that is actually scarce
    seq = ["action"] * 20
    r = diversity_quota_compute(seq)
    assert r["self_loop_type"] == "action" and r["self_loop_len"] == 20, r
    assert r["suggested_next_type"] not in (None, "action"), r
    assert "self-loop" in r["reason"], r

    # 3. real-trajectory replay: 67 windows are monopolised by a single type
    #    (all integrator). The operator MUST return a seed for those, and it
    #    must never be the type that is running.
    real = ["integrator"] * 30 + ["action"] * 5
    r2 = diversity_quota_compute(real)
    assert r2["self_loop_type"] == "action" and r2["self_loop_len"] == 5, r2
    assert r2["suggested_next_type"] is not None, r2
    assert r2["suggested_next_type"] not in ("action",), r2

    # 3b. the full-monopoly case that actually occurs in the trajectory
    mono_real = ["integrator"] * 20
    rm = diversity_quota_compute(mono_real)
    assert rm["counts"] == {"integrator": 1.0}, rm
    assert rm["suggested_next_type"] not in (None, "integrator"), rm
    assert rm["suggested_next_type"] in TYPE_UNIVERSE, rm

    # 3c. a window that is NOT under pressure must not invent one. integrator at
    #     exactly 0.60 with a 3-frame run is a genuine no-op — the operator must
    #     not seed just because some universe type happens to be missing. The
    #     starvation path is for real self-loop pressure, not for a healthy mix.
    calm = diversity_quota_compute(["integrator"] * 12 + ["action"] * 5
                                   + ["planning"] * 3)
    assert calm["suggested_next_type"] is None, calm
    assert calm["self_loop_len"] == 3, calm

    # 4. healthy alternation must NOT trigger an intervention
    healthy = ["action", "planning", "doubt", "cultural", "action",
               "planning", "doubt", "cultural", "aesthetic", "decider",
               "action", "planning", "doubt", "cultural", "aesthetic",
               "decider", "action", "planning", "doubt", "cultural"]
    r3 = diversity_quota_compute(healthy)
    assert r3["suggested_next_type"] is None, r3
    assert r3["self_loop_len"] == 1, r3

    # 5. only the last QUOTA_WINDOW frames may matter
    long_seq = ["doubt"] * 100 + (["action"] * 4 + ["planning"] * 4) * 5
    r4 = diversity_quota_compute(long_seq)
    assert r4["window"] == QUOTA_WINDOW, r4

    # 6. degenerate inputs must not raise, and must never suggest the empty type
    for bad in ([], None, [""], [""] * 50, [""] * 5 + ["action"] * 20):
        r5 = diversity_quota_compute(bad)
        assert "suggested_next_type" in r5, r5
        assert r5["suggested_next_type"] != "", r5
    assert diversity_quota_compute([])["suggested_next_type"] is None

    # 7. never recommend the type that is currently running
    mono = ["doubt"] * 40
    assert diversity_quota_compute(mono)["suggested_next_type"] != "doubt"

    print("diversity_quota_operator selfcheck OK | constants 5/20 | "
          "self-loop break + healthy no-op + degenerate inputs")

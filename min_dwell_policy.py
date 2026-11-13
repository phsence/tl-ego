"""Pure decision helpers for the resting min-dwell gate."""


def is_int_press_escape(
    *,
    int_press_counter: int,
    concentration: float,
    candidate: str,
    raw_attractor: str,
) -> bool:
    """Release the dwell hold when integration pressure is genuine,
    independent of the candidate type. The pressure counter and
    concentration are sufficient signals; a candidate-type guard causes
    permanent early_hold lock when the selector proposes a non-integrator type."""
    del raw_attractor  # Kept explicit so callers cannot hide the observed attractor.
    return bool(
        int_press_counter >= 4
        and concentration >= 0.03
    )


def choose_continuing_dominant(
    *,
    incumbent: str,
    candidate: str,
    raw_attractor: str,
    stagnation: int = 0,
    int_press_ctr: int = 0,
    raw_dominant_history: dict | None = None,
    raw_attractor_concentration: float = 0.0,
) -> str:
    """Keep the behavioral incumbent when the gate reports `continue`.

    Break the incumbent lock when stagnation is high and the selector's
    candidate diverges from the raw attractor. Without this check, select_autonomous_dominant
    can return the incumbent type (e.g. action) due to coverage+novelty bias even when the
    KWTA attractor is cycling between rule_modification and integrator. The result is a
    permanent behavioral lock on the incumbent type with stagnation climbing indefinitely.

    int_press_ctr: when >= 8 and raw_attractor is unknown, genuine integration
    pressure is active but the raw attractor has no clear winner. Break the incumbent
    lock by promoting the most-frequent non-incumbent type from raw_dominant_history.
    This prevents _md_int_press_escape from being structurally inert (fires but votes
    for the same type because _md_cand == _md_prev).
    """
    # l5g-20260828-stagseesaw: normalize raw_attractor. "unknown" is NO evidence of a
    # divergent attractor (happens ~8/20 ticks when observed_path lacks a clear winner)
    # and must never be returned as a behavioral dominant nor treated as divergence.
    _raw_norm = raw_attractor if (raw_attractor and str(raw_attractor) != "unknown") else None
    if incumbent and candidate == incumbent:
        # L5g-20260914-attractor-trust: follow a confident attractor even before
        # stagnation builds. The KWTA has a clear winner that differs
        # from the behavioral incumbent -- trust it instead of holding.
        # l5g-20261108-067686: bar 0.15 -> 0.10 (== integrator_promo_allowed
        # default floor). Measured 2026-11-08 ticks 67687-67691: raw KWTA leader
        # lived at 0.1285-0.1453 with competition 0.833-0.897 - a genuine winner
        # (>=2x the 20-type flat share 0.05) that the absolute 0.15 bar refused,
        # so the incumbent latched (dispatch stagnation=2 at tick 67686, raw
        # winner 0.1453, gate_divergent=True) and the dominant rotated
        # doubt -> sensory without consolidating (world ledger mc_attack 0/26).
        if _raw_norm and _raw_norm != incumbent and raw_attractor_concentration >= 0.10:
            return _raw_norm
        if _raw_norm and stagnation >= 5 and _raw_norm != incumbent:
            return _raw_norm
        # l5g-20260829-004531-FIX: int_press_escape is inert when raw_attractor="unknown"
        # and candidate==incumbent. _md_int_press_escape fires (int_press_ctr>=8,
        # conc>=0.04) but choose_continuing_dominant ignores it, always returning incumbent.
        # Promote the top non-incumbent raw type to convert structural escape into real switch.
        if int_press_ctr >= 4 and _raw_norm is None and raw_dominant_history:
            _non_incumbent = {t: c for t, c in raw_dominant_history.items() if t != incumbent}
            if _non_incumbent:
                return max(_non_incumbent, key=_non_incumbent.get)
        # l5g-20260919-018642-FIX: escape when attractor==incumbent under genuine pressure.
        # Root cause: when raw_attractor==incumbent (both 'action'), the branch above
        # (requires _raw_norm is None) and the pre-branch (requires _raw_norm != incumbent)
        # both fail. Integration pressure counter >= 8 is genuine but structurally ignored.
        # Result: integrator permanently suppressed; action stays behavioral dominant.
        # Fix: under high pressure, promote most-frequent non-incumbent from raw history.
        if int_press_ctr >= 4 and _raw_norm == incumbent and raw_dominant_history:
            _non_incumbent = {t: c for t, c in raw_dominant_history.items() if t != incumbent}
            if _non_incumbent:
                return max(_non_incumbent, key=_non_incumbent.get)
        # l5g-20260922-021430-FIX: genuine integration pressure with low-but-sufficient concentration.
        # Root cause: when _int_press_ctr>=4 (genuine pressure) and raw_attractor is a known
        # non-incumbent type (e.g. integrator while incumbent=action) but conc<0.15, all existing
        # pre-branch checks fail (Check A requires conc>=0.15, Check B requires stag>=5,
        # Check C requires raw=None, Check D requires raw==incumbent). Falls through to
        # return incumbent, permanently suppressing the attractor winner.
        # Fix: when int_press>=4 and attractor conc>=0.03, promote the attractor winner.
        if int_press_ctr >= 4 and _raw_norm and _raw_norm != incumbent and raw_attractor_concentration >= 0.03:
            return _raw_norm
        # l5g-20260922-021485-FIX: escape when attractor==incumbent under genuine integration pressure.
        # Root cause: when raw_attractor==incumbent (e.g. both=doubt) and int_press_ctr>=4
        # but conc<0.15, all existing checks fail (Check A requires conc>=0.15, B stag>=5,
        # C raw=None+ctr>=8, D raw==incumbent+ctr>=8). Falls through to return incumbent,
        # permanently suppressing the selector's candidate despite genuine integration pressure.
        # Fix: when int_press>=4, attractor==incumbent, and conc>=0.03, promote attractor.
        if int_press_ctr >= 4 and _raw_norm == incumbent and raw_attractor_concentration >= 0.03:
            return _raw_norm
        return incumbent
    # l5g-20260830-005477-FIX: rule_modification alternation escape.
    # Root cause: when _md_prev==rule_modification and _md_cand!=rule_modification,
    # _md_dwell resets to 1 every tick; diversity_override gated on _md_dwell>=2
    # NEVER fires. choose_continuing_dominant else-branch returns _md_cand blindly.
    # Integration pressure (int_press_ctr>=8) is genuine but ignored in else-branch.
    # Fix: when incumbent is rm AND int_press>=8 AND raw_attractor unknown
    # (no clear KWTA winner), promote the most-frequent non-rm raw type.
    # This is a guaranteed escape independent of dwell accumulation.
    if (incumbent == 'rule_modification'
            and int_press_ctr >= 4
            and _raw_norm is None
            and raw_dominant_history):
        _non_rm = {t: c for t, c in raw_dominant_history.items()
                   if t != 'rule_modification'}
        if _non_rm:
            return max(_non_rm, key=_non_rm.get)
    return _raw_norm or candidate


# ───────────────────────────────────────────────────────────────────────
# l5g-20260823-000999 P-B v3: Gate-divergence diversity enforcement
# ───────────────────────────────────────────────────────────────────────
# Root cause (live observed at tick=146):
#   * raw_profile has 6 distinct types (architect=58%, rule_modification=22%,
#     integrator=15%, veto=3%, doubt=1%, action=1%)
#   * profile (behavioral) has only 3 distinct types (action=70%, integrator=29%, doubt=1%)
#   * rule_modification and veto from raw_profile are entirely missing from profile
#   * gate_divergence_count = 126/146 = 86.3% (raw ≠ behavioral almost every tick)
#   * dominant_dwell locks at action via min_dwell_continue:late_repeat_strong
#
# Prior patches v1/v2 worked at the affordance layer (cf-floor, int_press_inject)
# but the routing chain (observed_raw → arbitration_candidate → behavioral) still
# compresses 6 raw types into 1-2 behavioral types via the min_dwell dwell lock.
#
# Fix: when gate divergence is extreme (>=85%) AND raw is diverse (>=4 types
# with >=10 ticks each) AND behavioral incumbent dominates (>65%), inject the
# second-most-frequent raw type into the behavioral dominant for one tick.
# This forces the profile to count a different type, breaking the dominant
# lock at the persistence layer (PERSISTENT_IDENTITY updates every 10 ticks).
#
# Surgical: a pure-function override applied BEFORE _choose_continuing_dominant
# in tl_ego_server.py. Returns None to defer to existing logic, or returns a
# recommended dominant type to override the incumbent.
def gate_divergence_diversity_override(
    *,
    incumbent: str,
    raw_attractor: str,
    raw_profile: dict,
    profile: dict,
    gate_divergence_count: int,
    raw_total_ticks: int,
    stagnation: int = 0,
    int_press_counter: int = 0,
    concentration: float = 0.0,
    min_gate_div_ratio: float = 0.50,
    min_raw_types: int = 4,
    min_raw_type_ticks: int = 8,
    min_behavioral_dominance: float = 0.60,
    min_stagnation_deep_lock: int = 0,
    behavioral_recent: list | None = None,
    min_recent_window: int = 12,
    min_recent_share: float = 0.70,
) -> str | None:
    """Return an override dominant type when gate divergence is extreme.

    Conditions (all must hold):
      1. raw_total_ticks >= 30 (don't fire on early-life data)
      2. gate_divergence_count / raw_total_ticks >= min_gate_div_ratio (0.80)
      3. raw_profile has >= min_raw_types distinct types
      4. At least one non-incumbent raw type has >= min_raw_type_ticks ticks
      5. incumbent dominates profile with >= min_behavioral_dominance share
      6. raw_attractor != incumbent (raw already wants to switch)

    Returns:
      * The second-most-frequent raw type (excluding incumbent), or
      * None to defer to existing routing logic
    """
    # Guard 1: enough ticks
    if raw_total_ticks < 30:
        return None

    # Path C (2026-08-28 behavioral single-type lock): the cumulative
    # gate-divergence ratio is DILUTED when raw returns 'unknown' often
    # (unknown ticks never increment raw_gate_divergence_count at
    # tl_ego_server L15322), so a recent-window behavioral lock can hide
    # under Guard 2. When the behavioral channel is dominated by one type
    # in the recent window and raw history still holds a non-incumbent
    # type with support, force a switch regardless of cumulative
    # divergence / dominance. This is the fix for the 3948-3967 lock
    # (17/20 action, raw 11/20 unknown, gate_div 37.5% < 50%).
    if behavioral_recent:
        _win = [t for t in behavioral_recent if t and t not in ("unknown", "?")]
        if len(_win) >= min_recent_window:
            _inc_recent = sum(1 for t in _win if t == incumbent)
            # l5g-20261109-068735-FIX: detect 2-type alternation locks, not only
            # single-type locks. Measured 2026-11-09 (resting-log ticks
            # 68537-68748, n=220): gate divergence 108/220 (49.1 pct) while the
            # behavioral channel alternated integrator/decider in 4-tick runs
            # (top-2 share 1.00); rule_modification suppressed 34 ticks,
            # architect 26. The single-type >=0.70 share test never fires on a
            # 2-type lock, so Path C was structurally unreachable in this regime.
            _beh_counts = {}
            for _bt in _win:
                _beh_counts[_bt] = _beh_counts.get(_bt, 0) + 1
            _top2_share = sum(sorted(_beh_counts.values(), reverse=True)[:2]) / len(_win)
            if (_inc_recent / len(_win) >= min_recent_share
                    or _top2_share >= min_recent_share):
                # l5g-20261102-064721-FIX: defer when the raw attractor already
                # agrees with the incumbent. Measured 2026-11-02: raw_attractor
                # = rule_modification (KWTA winner; arbitration candidate_score
                # 0.659508 == kwta_winner_score, margin 0.0) and behavioral
                # incumbent = rule_modification for 10 consecutive ticks
                # (64711-64720) -- healthy agreement that min_dwell correctly
                # held -- yet Path C fired and stomped the genuine winner with
                # the cumulative-count leader: integrator (13760/63070 raw
                # ticks) at 64721 via the int_press>=8 branch, architect (7884)
                # at 64724 via the count loop, manufacturing gate divergence
                # (div 27528->27529->27530) where raw and behavioral agreed.
                # The recent-window signal cannot distinguish "behavioral
                # locked while raw is diverse" (Path C's purpose) from
                # "behavioral following a confident raw winner";
                # raw_attractor == incumbent is exactly the second case.
                # 'unknown' is not agreement (stagseesaw normalization).
                if raw_attractor and raw_attractor == incumbent:
                    return None
                _path_c_raw = sorted(
                    ((t, c) for t, c in raw_profile.items() if t and t != incumbent),
                    key=lambda x: -x[1],
                )
                if _path_c_raw:
                    # l5g-20260830-005064-FIX: integrator pressure promotion in Path C.
                    # Root cause: Path C returns the highest-raw-count non-incumbent type
                    # (rule_modification=1077, veto=1041) without any integration-pressure
                    # awareness. When int_press_ctr >= 8 and raw_attractor is not integrator,
                    # genuine integration pressure is active but the override ignores it
                    # and promotes rule_modification/veto instead. integrator dom_rate stays
                    # at 0.0% because the selector keeps proposing 'action' (coverage bias).
                    # Fix: check for integration pressure here; if integrator has sufficient
                    # raw support (>= min_raw_type_ticks), return it before the
                    # behavioral_set filter. This makes Path C integration-pressure-aware
                    # without changing its behavioral_set anti-cycling guard.
                    if (int_press_counter >= 8
                            and raw_attractor != "integrator"
                            and raw_profile.get("integrator", 0) >= min_raw_type_ticks):
                        return "integrator"
                    # l5g-20260829-004460: exclude types already active in the behavioral
                    # channel from Path C override candidates. Without this guard, Path C
                    # cycles between rule_modification and veto because both accumulate
                    # raw_profile counts while cycling in behavioral. Filtering
                    # behavioral_recent forces the override to reach genuinely different types.
                    _behavioral_set = set(
                        t for t in behavioral_recent if t and t not in ("unknown", "?")
                    ) if behavioral_recent else set()
                    for candidate_type, _count in _path_c_raw:
                        if candidate_type not in _behavioral_set:
                            return candidate_type
                    return _path_c_raw[0][0]  # last resort: all top types are behavioral-active

    # Path B (deep-lock release, 2026-08-26 stag=182 intervention): when
    # stagnation is extreme the lock is proven by stagnation itself, so relax
    # the gate-divergence floor and behavioral-dominance requirement, and
    # allow fallback to the strongest non-incumbent raw type. Default 0 =
    # disabled; opt in explicitly at the call site.
    _deep_lock = min_stagnation_deep_lock > 0 and stagnation >= min_stagnation_deep_lock

    # Guard 2: gate divergence ratio
    if raw_total_ticks > 0:
        gate_div_ratio = gate_divergence_count / raw_total_ticks
    else:
        gate_div_ratio = 0.0
    if gate_div_ratio < min_gate_div_ratio and not _deep_lock:
        return None

    # Guard 3: raw type diversity
    if not isinstance(raw_profile, dict) or len(raw_profile) < min_raw_types:
        return None

    # Guard 6: raw wants to switch (deep-lock may fall back to the strongest
    # non-incumbent raw type via non_inc_raw below)
    # l5g-20260909-009421-FIX: exempt integrator-pressure promotion from Guard 6.
    # Root cause: raw_attractor='unknown' in flat attractor regime triggers
    # 'not raw_attractor' branch of Guard 6, returning None before the int_press
    # integrator promotion (lines 172-175) can execute. The promotion is dead code
    # in this case, causing integrator dom_rate=0.0 when int_press>=8.
    # Fix: allow Path C to proceed when genuine integration pressure is active
    # and integrator has sufficient raw history support (>= min_raw_type_ticks).
    if (not raw_attractor or raw_attractor == incumbent) and not _deep_lock \
            and not (int_press_counter >= 8 and raw_profile.get("integrator", 0) >= min_raw_type_ticks):
        return None

    # Guard 4: a non-incumbent raw type has enough support
    sorted_raw = sorted(
        ((t, c) for t, c in raw_profile.items() if t and t != incumbent),
        key=lambda x: -x[1],
    )
    non_inc_raw = [(t, c) for t, c in sorted_raw if c >= min_raw_type_ticks]
    if not non_inc_raw:
        return None

    # Guard 5: behavioral incumbent dominance
    if isinstance(profile, dict) and profile:
        total = sum(profile.values())
        if total > 0:
            inc_share = profile.get(incumbent, 0) / total
            if inc_share < min_behavioral_dominance and not _deep_lock:
                return None
        else:
            return None
    else:
        return None

    # All guards passed: pick the best non-incumbent raw type.
    # Prefer raw_attractor_dominant if it has enough support; else the next.
    override_target = raw_attractor
    if not override_target or override_target == incumbent:
        override_target = non_inc_raw[0][0]

    return override_target


def integrator_promo_allowed(
    *,
    int_conc: float,
    leader_conc: float,
    int_press: int,
    min_conc: float = 0.10,
    tie_margin: float = 0.90,
) -> bool:
    """Whether integration pressure may hand the raw-attractor win to integrator.

    The old gate was an absolute `int_conc >= 0.10` while `int_press > 0`. In a
    20-type field a flat share is 0.05, so integrator cleared the bar while
    holding half the leader's mass (observed live: leader veto 0.206 vs
    integrator 0.101 -> the minority type got crowned, raw integrator 88% of
    ticks and only 2 behavioral types survived). The promo exists to break ties
    while integrator consolidates, so require a near tie with the current
    leader, not merely an absolute floor.
    """
    if int_press <= 0:
        return False
    return int_conc >= max(min_conc, leader_conc * tie_margin)


def should_release_late_dwell(
    *,
    crisis: bool,
    strong_repeated: bool,
    integrator_escape: bool,
    int_press_escape: bool = False,
    raw_attr_int_press_escape: bool = False,
    uniform_release: bool,
    stale_incumbent_release: bool,
    autonomous_competitive_release: bool,
) -> bool:
    """Return whether a late-dwell challenger may replace the incumbent.

    int_press_escape: when _int_press_ctr >= 9, concentration >= 0.09, and
    per-neuron margin >= 0, integration pressure is genuine and must be allowed
    to break the dwell lock even in the late window (dwell >= 3).

    raw_attr_int_press_escape: when _raw_attractor_dominant == integrator AND
    _int_press_ctr >= 8 AND _md_conc >= 0.08, the raw KWTA attractor is
    persistently integrator and integration pressure is genuine. This escapes
    the late-window lock even when _md_cand == _md_prev (action→action deadlock)
    and all other late-window conditions are False."""
    return bool(
        crisis
        or strong_repeated
        or integrator_escape
        or int_press_escape
        or raw_attr_int_press_escape
        or uniform_release
        or stale_incumbent_release
        or autonomous_competitive_release
    )

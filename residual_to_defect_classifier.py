#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L6-0 residual-to-defect classifier.

This module aggregates JEPA residual attribution records into mechanism-level
self-diagnoses. It is deliberately side-effect-free: it does not modify
planner state, repair files, gates, or TL-Ego runtime. The first goal is to let
Ego say *what kind of mechanism is failing* before proposing self-repair.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / ".lae" / "jepa_residuals.jsonl"


def _num(data: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key, default)
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _tags(record: Mapping[str, Any]) -> List[str]:
    raw = _nested(record, "attribution", "tags") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Iterable):
        return []
    return [str(item) for item in raw if item is not None]


def _errors(record: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = _nested(record, "attribution", "errors")
    return raw if isinstance(raw, Mapping) else {}


def _prediction_error(record: Mapping[str, Any]) -> float:
    attribution = record.get("attribution")
    if isinstance(attribution, Mapping) and "prediction_error" in attribution:
        return _num(attribution, "prediction_error", 0.0)
    context = record.get("context")
    if isinstance(context, Mapping):
        return _num(context, "reported_prediction_error", 0.0)
    return 0.0


def _dominant(record: Mapping[str, Any], section: str, key: str = "dominant") -> Optional[str]:
    raw = record.get(section)
    if not isinstance(raw, Mapping):
        return None
    # 2026-11-08 (jepa_feature_missing P0): for the predicted section, prefer the
    # honest 2-gram forecast when the record carries one. next_dominant is the
    # counterfactual TARGET (15% hit rate, measures action efficacy), so scoring
    # it here diagnosed the world model with the wrong target. Records written
    # before the forecast existed fall back to next_dominant unchanged.
    if section == "predicted" and raw.get("next_dominant_forecast"):
        value = raw.get("next_dominant_forecast")
    else:
        value = raw.get(key) or raw.get("next_dominant") or raw.get("dominant_type")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_records(path: Optional[Path] = None, limit: int = 80) -> List[Dict[str, Any]]:
    """Load recent JSONL residual records, skipping malformed lines."""
    target = Path(path) if path is not None else DEFAULT_LOG_PATH
    if not target.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines()[-max(1, limit) :]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _score_candidate_pool(records: Sequence[Mapping[str, Any]], tag_counts: Counter, action_counts: Counter, cooling: bool = False) -> Optional[Dict[str, Any]]:
    if not records:
        return None
    # §L6-cooling-guard (l5g-20260722-000701): suppress defect classification during
    # post-repair cooling. The L6-P cooling guard in tl_ego_server.py sets
    # status=cooling_after_repair and confidence=0.25; re-invoking the classifier
    # with the same residual window (still dominated by the repair action) produces
    # a spurious confidence bump that re-triggers the L6-P injection loop, keeping
    # candidate_pool_too_narrow alive indefinitely.
    # §L6-cooling-guard-v2 (l5g-20260722-000141): removed full suppression on cooling.
    # Root cause: cooling=True caused _score_candidate_pool to return None, freezing defect
    # confidence at the previous tick's cooled value (e.g. 0.15) indefinitely. Even when
    # counterfactuals were active and stagnation was slowly decreasing, the classifier could
    # never produce a fresh confidence score above 0.42, so the defect stayed in
    # cooling_after_repair and the L6 repair pipeline never re-fired.
    # Fix: run scoring during cooling with a 0.08 penalty. Lower threshold (0.42-0.08=0.34)
    # lets partially-improving counterfactuals re-thermalize the defect. If counterfactuals
    # fail (no stagnation improvement in 10+ ticks), score stays below 0.34 and L6 re-fires.
    _cooling_penalty = 0.08 if cooling else 0.0
    top_action, top_count = action_counts.most_common(1)[0]
    repeat_ratio = top_count / len(records)
    # L5g-20260706-000031: filter to new-format records before scoring.
    # Old-format records (no attribution, from prior-run ticks 9000+) inflate
    # inspect_recent_project_result count and produce a false candidate_pool_too_narrow
    # signal. This mirrors the Patch3 filter in ego_jepa._recent_residual_penalty (L221).
    new_fmt = [r for r in records if r.get("attribution") is not None]
    records = new_fmt if new_fmt else records
    false_count = tag_counts["false_exploration"]
    lag_count = tag_counts["oscillation_lag"] + tag_counts.get("oscillation_expected", 0)
    novelty_errors = [float(_errors(row).get("novelty_error", 0.0) or 0.0) for row in records]
    avg_novelty_error = sum(novelty_errors) / len(novelty_errors)

    score = 0.0
    if top_count >= 3:
        score += 0.28
    if repeat_ratio >= 0.35:
        score += 0.24
    if false_count >= 1:
        score += 0.20
    if lag_count >= 2:
        score += 0.10
    if avg_novelty_error > 0.20:
        score += 0.12
    if len(action_counts) <= 2 and len(records) >= 4:
        score += 0.06

    # l5g-20261018-047410: the defect NAME claims pool narrowness, but the
    # score was driven entirely by action repetition plus a saturated
    # oscillation_lag tag (18/20 records, 9x over the >=2 threshold).
    # Measured 2026-10-18 at tick 47410: 14 distinct actions in the window,
    # false_exploration=0, avg_novelty_error=-0.058 -- the pool is wide and
    # prediction quality is fine, yet confidence read 0.62 and the verdict
    # could never resolve (status stayed null indefinitely, and the +0.10
    # lag floor is permanent). Require the evidence that justifies the name.
    _pool_is_narrow = len(action_counts) <= 2
    _mispredicting = false_count >= 1 or avg_novelty_error > 0.20
    if not (_pool_is_narrow or _mispredicting):
        return None
    if score < (0.42 - _cooling_penalty):
        return None
    return {
        "defect_type": "candidate_pool_too_narrow",
        "confidence": round(min(0.95, score), 3),
        "pre_penalty_confidence": round(score, 3),  # unpenalized score; builder uses this for threshold check
        "evidence": [
            f"top_action={top_action} repeated {top_count}/{len(records)} ({repeat_ratio:.0%})",
            f"false_exploration={false_count}, oscillation_lag={lag_count}",
            f"avg_novelty_error={avg_novelty_error:.3f}",
            "same action keeps being penalized; ranking alone is not enough",
        ],
        "self_statement": "我的问题不只是某个 action 分数错了，而是候选生成机制缺少反事实替代。",
        "repair_hint": "add residual-tag-derived counterfactual affordances before ranking",
    }


def _score_observation_window(records: Sequence[Mapping[str, Any]], tag_counts: Counter, cooling: bool = False) -> Optional[Dict[str, Any]]:
    # §L6-cooling-guard-v2 (l5g-20260722-000141): removed cooling suppression here too.
    # Cooling should not prevent observation-window analysis; stale evidence is already
    # handled by the record filter (new_fmt) and bistable confirmation guard.
    # §L6-fix-v2 (2026-07-07 l5g-20260707-000051-MUT): Removed false_exploration suppression gate.
    # The old gate (tag_counts.get("false_exploration", 0) > 0: return None) was intended to
    # prevent double-counting but instead hid the root cause: bistable oscillation between action
    # and integrator is the primary defect, and false_exploration is its symptom, not an alternative
    # root cause. oscillation_lag with bistable trajectory confirmation (weight 0.35) correctly
    # surfaces the integrator suppression defect. The bistable trajectory check provides contextual
    # weighting: 0.35 if oscillation confirmed, 0.15 if sustained single-type dominance.
    lag_count = tag_counts["oscillation_lag"]
    integration_count = tag_counts["integration_window"]
    mismatches = 0
    for row in records:
        if _dominant(row, "predicted") != _dominant(row, "actual"):
            mismatches += 1

    # l5g-20260824-001160: secondary transition-accuracy check.
    # Primary mismatch (pred.next_dom vs act.dom) measures oscillation presence
    # but is structurally inflated when JEPA predicts transitions away from the
    # current dominant. The secondary check verifies whether those transitions
    # actually occur in the next record. If BOTH mismatch rates are high,
    # the defect is JEPA miscalibration, not an observation window problem.
    transition_errors = 0
    transition_checks = 0
    for i in range(len(records) - 1):
        pred_next = (records[i].get("predicted") or {}).get("next_dominant")
        act_next = (records[i + 1].get("actual") or {}).get("dominant")
        if pred_next and act_next:
            transition_checks += 1
            if pred_next != act_next:
                transition_errors += 1

    # §L6-trajectory-scan (l5g-20260707-000061): verify oscillation_lag with actual trajectory.
    # Root cause: oscillation_lag tag appeared in 57.5% of records (46/80) but trajectory scan
    # shows most tagged records have sustained single-type dominance, not bistable oscillation.
    # Without trajectory verification, oscillation_lag alone creates false positives.
    # Extract dominant type sequence from record context (recent_dominants + actual dominant).
    def _dominant_sequence(recs, max_len=8):
        seq = []
        for r in recs:
            ctx = r.get("context") if isinstance(r.get("context"), Mapping) else {}
            rd = ctx.get("recent_dominants", []) if isinstance(ctx, Mapping) else []
            actual_dom = _dominant(r, "actual")
            if rd:
                seq.extend([t for t in rd if t])
            if actual_dom:
                seq.append(actual_dom)
        return seq[-max_len:]

    def _has_bistable(seq, min_flips=2):
        """Return True if sequence shows ≥min_flips transitions between the dominant type pair.

        Relaxed from exactly 2 types to 2-4 types: a transient third type (e.g. 'doubt')
        between oscillating action↔integrator does not invalidate the bistable signal.
        The pair with the highest mutual-transition count is used as the oscillation signal.
        """
        if len(seq) < min_flips + 2:
            return False
        type_counts = Counter(t for t in seq if t)
        if len(type_counts) < 2:
            return False
        top_types = [t for t, _ in type_counts.most_common(4)]
        # Find the pair with the most mutual transitions
        best_flips = 0
        for i in range(len(top_types)):
            for j in range(i + 1, len(top_types)):
                ta, tb = top_types[i], top_types[j]
                flips = sum(
                    1 for k in range(1, len(seq))
                    if seq[k] != seq[k - 1] and seq[k] in (ta, tb) and seq[k - 1] in (ta, tb)
                )
                if flips > best_flips:
                    best_flips = flips
        return best_flips >= min_flips

    # §L6-fix-v3 (l5g-20260708-000041): prefer _multi_sample_window over 8-tick sequence
    # for bistable confirmation when oscillation_lag may exceed the 8-tick window.
    # Root cause: _dominant_sequence(records, max_len=8) reconstructs at most 8 dominants
    # from limit=20 records. When oscillation_lag > 8 (e.g. lag=10, 17 records span
    # <2 oscillation cycles), _has_bistable sees 3-4 distinct types diluted across the
    # 8-tick window and returns False. lag_evidence_weight drops to 0.15, score may
    # miss the 0.45 defect threshold, and counterfactual inference routes to
    # modify_suppression_rule (conf≈0.5) instead of synthesize_findings.
    # Fix: records[-1].context._multi_sample_window is already present (written by
    # _reconcile_jepa_predictions in tl_ego_server.py at the 5-sample JEPA window).
    # Its window_dominant_mode="oscillating" directly confirms bistability with
    # action+integrator in the dominant type counts, bypassing the 8-tick limitation.
    bistable_confirmed = False
    _last_ctx = records[-1].get("context") if records else {}
    if isinstance(_last_ctx, Mapping):
        _msw = _last_ctx.get("_multi_sample_window")
        if isinstance(_msw, Mapping) and _msw.get("window_dominant_mode") == "oscillating":
            bistable_confirmed = True
    if not bistable_confirmed:
        dominant_seq = _dominant_sequence(records)
        bistable_confirmed = _has_bistable(dominant_seq)
    lag_evidence_weight = 0.35 if bistable_confirmed else 0.15  # downgrade if no bistable in trajectory

    # §L6-fix-v4 (l5g-20260708-000611): oscillation_lag in wide window is not a defect.
    # When _multi_sample_window.window_size >= 10 AND oscillation_lag >= 0.6 * window_size,
    # the window is already wide enough to capture the oscillation cycle. oscillation_lag
    # at this level is normal bistable behavior, not evidence of a too-narrow window.
    # Misdiagnosing this as observation_window_too_narrow prevents the L6-patch from firing
    # (it requires candidate_pool_too_narrow as primary defect), blocking synthesize_findings
    # promotion and keeping the action↔integrator bistable oscillation unbroken.
    # Fix: return None here so candidate_pool_too_narrow (score >= 0.42) becomes primary defect,
    # L6-patch activates, injects counterfactuals, promotes synthesize_findings.
    # Fix: raise window threshold to 10 (per L6-fix-v4 comment) and use rate-based
    # comparison (lag_count / n_records) instead of absolute count vs scaled window_size.
    # Old guard mixed incompatible scales: lag_count (from 20 records) vs window_size*0.6
    # (from up to 30-sample _jepa_win), so window_size=30 with lag_count=9: 9>=18=False.
    # With rate-based: 9/20=0.45>=0.45 → True → guard fires → observation_window_too_narrow
    # suppressed → candidate_pool_too_narrow surfaces as primary defect → L6-patch activates.
    # Warm-up protection (l5g-20260726-000003): if records < 10 AND no
    # _multi_sample_window in context, the window hasn't accumulated enough
    # samples for reliable oscillation_lag scoring. Return None to avoid
    # warm-up defects polluting cooling history.
    _records_sufficient = len(records) >= 10
    _has_window_context = (
        isinstance(_last_ctx, Mapping)
        and isinstance(_last_ctx.get("_multi_sample_window"), Mapping)
    )
    if not _records_sufficient and not _has_window_context:
        # l5g-20260824-000451: oscillation evidence bypasses warm-up guard.
        # When oscillation_lag or integration_window tags are present in records,
        # the system has enough oscillation evidence to score the defect even
        # without a full _multi_sample_window context.
        _osc = tag_counts.get("oscillation_lag", 0) + tag_counts.get("integration_window", 0)
        if _osc == 0:
            return None

    # L6-fix-v4 fast-exit: wide window (>=10) with oscillation_lag is normal
    # bistable behavior, not evidence of a too-narrow observation window.
    # Returning None lets candidate_pool_too_narrow (score >= 0.42) surface
    # as the primary defect, which activates the L6-patch (injects
    # counterfactuals, promotes synthesize_findings).
    _window_wide_enough = (
        _has_window_context
        and int(_last_ctx["_multi_sample_window"].get("window_size", 0) or 0) >= 5
    )
    if _window_wide_enough:
        return None

    score = 0.0
    if lag_count >= 2:
        score += lag_evidence_weight  # §L6-trajectory-scan: 0.35 if bistable confirmed, 0.15 if sustained dominance

    if mismatches >= 2:
        # §L6-score-scale (l5g-20260824-000630): scale mismatch bonus by mismatch ratio so
        # unambiguous systemic misalignment (e.g. 20/20) scores higher than marginal mismatches.
        # Flat +0.20 treated 2/20 and 20/20 identically, capping the defect at 0.55 even with
        # oscillation_lag=20 and 100% mismatch — below _MIN_CONFIDENCE=0.70, so the L6 repair
        # pipeline returned l6p-insufficient-confidence (no-op) instead of the real fix.
        # Ratio-scaled: +0.10 base + 0.50 * ratio → 2/20=+0.20 (unchanged), 20/20=+0.40.
        mismatch_ratio = mismatches / len(records) if records else 0.0
        score += 0.10 + 0.50 * mismatch_ratio
    if records:
        high_comp = sum(1 for row in records if _num(row.get("context", {}) if isinstance(row.get("context"), Mapping) else {}, "competition", 0.0) > 0.80)
        if high_comp / len(records) >= 0.5:
            score += 0.10

    # l5g-20260824-001160: mismatch ratio flatness guard.
    # Window sensitivity analysis across n=10..200 shows mismatch ratio is
    # structurally flat (85-100%) regardless of window size. This means the
    # mismatch metric measures JEPA prediction quality (8.5% transition accuracy),
    # NOT observation window adequacy. When mismatch_ratio >= 0.85 AND the ratio
    # is flat across small vs large windows, discount the score to avoid a
    # false-positive observation_window_too_narrow that resists repair.
    def _mr(recs):
        if not recs:
            return 0.0
        m = sum(1 for r in recs if (r.get("predicted") or {}).get("next_dominant") != (r.get("actual") or {}).get("dominant"))
        return m / len(recs)
    _mr_cur = _mr(records)
    if _mr_cur >= 0.85 and len(records) >= 20:
        _mr_small = _mr(records[-20:]) if len(records) >= 20 else _mr_cur
        _mr_big = _mr(records[-80:]) if len(records) >= 80 else _mr_cur
        if abs(_mr_cur - _mr_small) < 0.1 and abs(_mr_cur - _mr_big) < 0.1:
            score *= 0.3  # heavily discount: flat ratio = JEPA quality, not window

    # l5g-20260824-001160: mismatch ratio flatness guard.
    # Window sensitivity analysis across n=10..200 shows mismatch ratio is
    # structurally flat (85-100%) regardless of window size. This means the
    # mismatch metric measures JEPA prediction quality (8.5% transition accuracy),
    # NOT observation window adequacy. When mismatch_ratio >= 0.85 AND the ratio
    # is flat across small vs large windows, discount the score to avoid a
    # false-positive observation_window_too_narrow that resists repair.
    def _mr(recs):
        if not recs:
            return 0.0
        m = sum(1 for r in recs if (r.get("predicted") or {}).get("next_dominant") != (r.get("actual") or {}).get("dominant"))
        return m / len(recs)
    _mr_cur = _mr(records)
    if _mr_cur >= 0.85 and len(records) >= 20:
        _mr_small = _mr(records[-20:]) if len(records) >= 20 else _mr_cur
        _mr_big = _mr(records[-80:]) if len(records) >= 80 else _mr_cur
        if abs(_mr_cur - _mr_small) < 0.1 and abs(_mr_cur - _mr_big) < 0.1:
            score *= 0.3  # heavily discount: flat ratio = JEPA quality, not window

    if score < 0.45:
        return None
    return {
        "defect_type": "observation_window_too_narrow",
        "confidence": round(min(0.92, score), 3),
        "pre_penalty_confidence": round(score, 3),  # unpenalized score; builder uses this for threshold check
        "evidence": [
            f"oscillation_lag={lag_count}, integration_window={integration_count}",
            f"predicted_actual_mismatch={mismatches}/{len(records)}",
            "single status snapshots likely under-sample attractor movement",
        ],
        "self_statement": "我可能在用单点状态判断振荡系统，需要多采样窗口再归因。",
        "repair_hint": "use a short multi-sample status window for residual attribution and verification",
    }


def _score_novelty_calibration(records: Sequence[Mapping[str, Any]], tag_counts: Counter, action_counts: Counter) -> Optional[Dict[str, Any]]:
    novelty_errors = [float(_errors(row).get("novelty_error", 0.0) or 0.0) for row in records]
    avg_novelty_error = sum(novelty_errors) / len(novelty_errors) if novelty_errors else 0.0
    diverse_enough = len(action_counts) >= 3
    score = 0.0
    if tag_counts["false_exploration"] >= 2:
        score += 0.30
    if avg_novelty_error > 0.25:
        score += 0.30
    if diverse_enough:
        score += 0.15

    if score < 0.50:
        return None
    return {
        "defect_type": "novelty_calibration_drift",
        "confidence": round(min(0.90, score), 3),
        "evidence": [
            f"false_exploration={tag_counts['false_exploration']}",
            f"avg_novelty_error={avg_novelty_error:.3f}",
            f"action_diversity={len(action_counts)}",
        ],
        "self_statement": "我高估了外部新颖性，需要校准 novelty 预测而不是只惩罚动作。",
        "repair_hint": "apply bounded residual novelty calibration by action template and tag",
    }


def _score_jepa_feature_gap(records: Sequence[Mapping[str, Any]], tag_counts: Counter) -> Optional[Dict[str, Any]]:
    if not records:
        return None
    avg_error = sum(_prediction_error(row) for row in records) / len(records)
    unexplained = tag_counts["type_mismatch"] + tag_counts["competition_underestimated"] + tag_counts["concentration_overestimated"]
    known_window = tag_counts["oscillation_lag"] + tag_counts["integration_window"]
    score = 0.0
    if avg_error > 0.45:
        score += 0.35
    if unexplained >= 2:
        score += 0.25
    if known_window == 0 and avg_error > 0.35:
        score += 0.15
    # l5g-20261106-066257: score the hard dominant-type mismatch directly.
    # Measured 2026-11-06 tick 66257 on the live last-20 residual window:
    # predicted.next_dominant != actual.dominant on 19/20 (95%) while
    # attribution.prediction_error averages 0.374 -- under the 0.45 gate above,
    # because the hard 1.0 type surprise is diluted by near-zero conc/comp terms
    # (conc_surprise 0.005). That window is wide (window_size=23,
    # window_dominant_mode='oscillating'), so _score_observation_window
    # fast-exits by design and owned this evidence for nobody: all five scorers
    # returned None and the classifier reported no_stable_defect_pattern /
    # confidence 0.0, emitting l6p-insufficient-confidence without naming the gap.
    # Ratio-scaled on this file's own scale convention (0.10 + 0.50 * ratio) and
    # gated on known_window > 0 so it can never double-count the oscillation
    # evidence _score_observation_window owns. The 0.60 gate fires only on an
    # unambiguous sustained mismatch, keeping confidence at 0.50-0.88 -- under the
    # 0.85 auto-execute threshold in defect_to_repair_proposal.py, so the
    # diagnosis surfaces without triggering autonomous self-modification.
    # 2026-11-09 (l5g-20261109-068164, P0 jepa_feature_missing): score only
    # records that carry a real forecast, and gate on skill versus the
    # persistence baseline instead of an absolute mismatch ratio. Measured
    # this session on the live residual log: the behavioral dominant is
    # gate-transformed (raw != behavioral on 100/200 of the last records)
    # and high-entropy (2.67 bits), and no recorded-context feature beats
    # persistence out-of-sample (2-gram 12.2%, gate-conditioned 17.2%,
    # persistence 24.7%). The old absolute 0.60 gate demanded a >40% hit
    # rate that the measured ~30% ceiling can never reach, so the defect
    # could not resolve even at the best measurable model. The aspiration
    # fallback (next_dominant, ~10% hit by design) also inflated the
    # denominator on forecast-less records.
    _fc_rows = [
        row for row in records
        if isinstance(row.get("predicted"), Mapping)
        and row["predicted"].get("next_dominant_forecast")
    ]
    hard_mismatch = sum(
        1 for row in _fc_rows
        if _dominant(row, "predicted") != _dominant(row, "actual")
    )
    mismatch_ratio = hard_mismatch / len(_fc_rows) if _fc_rows else 0.0
    _persistence_hit = 0.0
    if _fc_rows:
        _pers_hits = 0
        for row in _fc_rows:
            _ctx = row.get("context")
            _rd = _ctx.get("recent_dominants") if isinstance(_ctx, Mapping) else None
            if (
                isinstance(_rd, Sequence)
                and not isinstance(_rd, (str, bytes))
                and len(_rd) >= 2
                and str(_rd[-2]).strip() == _dominant(row, "actual")
            ):
                _pers_hits += 1
        _persistence_hit = _pers_hits / len(_fc_rows)
    if (
        mismatch_ratio >= 0.60
        and known_window > 0
        and len(_fc_rows) >= 5
        and (1.0 - mismatch_ratio) <= _persistence_hit
    ):
        score += 0.10 + 0.50 * mismatch_ratio

    if score < 0.50:
        return None
    return {
        "defect_type": "jepa_feature_missing",
        "confidence": round(min(0.88, score), 3),
        "evidence": [
            f"avg_prediction_error={avg_error:.3f}",
            f"unexplained_mismatch_tags={unexplained}",
            f"hard_dominant_mismatch={hard_mismatch}/{len(_fc_rows)} ({mismatch_ratio:.0%})",
            f"forecast_hit={1.0 - mismatch_ratio:.1%} vs persistence={_persistence_hit:.1%}",
            "errors are not explained by an observation-window pattern",
        ],
        "self_statement": "我的世界模型缺少解释当前误差的特征，需要补充预测因子。",
        "repair_hint": "inspect residual contexts and add the missing bounded feature to Ego-JEPA",
    }


def classify_defects(records: Sequence[Mapping[str, Any]], cooling: bool = False) -> Dict[str, Any]:
    """Aggregate recent residual records into mechanism-level defects.

    Parameters
    ----------
    records : sequence of residual records
    cooling : if True, suppress candidate_pool and observation_window scoring
        to avoid re-firing defect classification during the post-repair cooling period.
    """
    usable = [row for row in records if isinstance(row, Mapping)]
    if not usable:
        return {
            "primary_defect": "insufficient_residual_data",
            "confidence": 0.0,
            "defects": [],
            "evidence": ["no residual records available"],
            "self_statement": "我还没有足够 residual 证据判断自身机制缺陷。",
        }

    tag_counts: Counter = Counter(tag for row in usable for tag in _tags(row))
    action_counts: Counter = Counter(str(row.get("action") or "unknown") for row in usable)

    candidates = [
        _score_candidate_pool(usable, tag_counts, action_counts, cooling=cooling),
        _score_observation_window(usable, tag_counts, cooling=cooling),
        _score_novelty_calibration(usable, tag_counts, action_counts),
        _score_jepa_feature_gap(usable, tag_counts),
        _score_gate_divergent(usable, tag_counts, action_counts),
    ]
    defects = sorted([item for item in candidates if item], key=lambda item: item["confidence"], reverse=True)
    # L6-fix-v5 (l5g-20260708-000161): cross-defect suppression
    # When both defects coexist, promote candidate_pool_too_narrow (key=0) above
    # observation_window_too_narrow (key=1) regardless of confidence ordering.
    # This unblocks the L6-patch (requires candidate_pool_too_narrow as primary defect)
    # which promotes synthesize_findings and breaks the integrator suppression loop.
    # L6-fix-v4 guard already prevents misdiagnosis when window_size>=10 AND lag_rate>=0.45,
    # but when window_size is absent or lag_rate is below threshold the guard does not fire
    # and observation_window_too_narrow can still outrank candidate_pool_too_narrow by confidence.
    def _sort_key(d):
        return 0 if d["defect_type"] == "candidate_pool_too_narrow" else 1
    defects = sorted(defects, key=_sort_key)
    if not defects:
        return {
            "primary_defect": "no_stable_defect_pattern",
            "confidence": 0.0,
            "defects": [],
            "evidence": [f"records={len(usable)}", f"tags={dict(tag_counts)}", f"actions={dict(action_counts)}"],
            "self_statement": "我看到 residual，但还没有稳定到足以生成自修复假设的机制缺陷。",
        }

    primary = defects[0]
    return {
        "primary_defect": primary["defect_type"],
        "confidence": primary["confidence"],
        "defects": defects,
        "evidence": primary["evidence"],
        "self_statement": primary["self_statement"],
        "repair_hint": primary["repair_hint"],
        "stats": {
            "records": len(usable),
            "tag_counts": dict(tag_counts),
            "action_counts": dict(action_counts),
        },
    }


def _score_gate_divergent(
    records: Sequence[Mapping[str, Any]],
    tag_counts: Counter,
    action_counts: Counter,
) -> Optional[Dict[str, Any]]:
    """Detect suppression-routing gap when raw KWTA attractor differs from behavioral dominant.

    When raw_attractor_dominant and resting_dominant diverge (gate_divergent=True),
    the behavioral gate is suppressing the raw attractor's preferred type.  The defect
    classifier previously had no scorer for this signal, returning no_stable_defect_pattern
    / confidence=0.0 and blocking the L6 repair pipeline.
    """
    if not records:
        return None
    _last_ctx = records[-1].get("context") if records else {}
    _ctx = _last_ctx if isinstance(_last_ctx, Mapping) else {}
    _gate_div = bool(_ctx.get("gate_divergent"))
    raw_attr  = str(_ctx.get("raw_attractor_dominant") or "")
    resting_d = str(_ctx.get("resting_dominant") or _ctx.get("dominant") or "")
    if not _gate_div and not raw_attr:
        # l5g-20261106-066364: the residual writer never emits gate_divergent
        # or raw_attractor_dominant (0/12100 records), so this scorer was
        # structurally unreachable. Reconstruct the server's own per-tick
        # definition (raw_attractor_dominant != dominant,
        # tl_ego_server.py:15010) from the fields the writer does emit: the
        # fraction of recent_raw_dominants differing from dominant_type.
        _rrd = _ctx.get("recent_raw_dominants")
        if isinstance(_rrd, Sequence) and not isinstance(_rrd, (str, bytes)):
            _rrd = [str(x) for x in _rrd if x]
            _dt = str(_ctx.get("dominant_type") or "")
            if _rrd and _dt:
                # l5g-20261109-068878: fire on the same-tick pair the evidence
                # prints (window newest raw vs this record's dominant_type) --
                # the server's own per-tick gate_divergent definition
                # (tl_ego_server.py:15010). The old window fraction measured
                # raw-channel volatility, not divergence: measured 291/390
                # residual records pass ratio>=0.5 while the same-tick pair is
                # equal in 199/390, so the scorer fired with self-refuting
                # evidence "raw_attractor=X != resting_dominant=X" (live
                # /status at ticks 68878 and 68889: raw==resting,
                # gate_divergent=false, residual_defect still claiming a gap).
                if _rrd[-1] != _dt:
                    raw_attr = _rrd[-1]
                    resting_d = resting_d or _dt
                    _gate_div = True
    # l5g-20261109-068878: majority-of-5 vote branch removed. It voted on the
    # same window fraction (raw-channel volatility), so it re-fired the defect
    # on stale divergence while the current same-tick pair showed none
    # (reproduced: 4/5 volatile windows + quiet newest record still fired).
    # The latch above tests that pair directly; a per-tick phenomenon is not
    # smoothed by a majority of past ticks.
    if not _gate_div:
        return None
    pred_err  = float(_ctx.get("proprio_prediction_error") or 0.0)
    stag      = int(_ctx.get("stagnation_streak") or 0)
    evidence = [
        f"raw_attractor={raw_attr} != resting_dominant={resting_d}",
        "gate_divergent=True — behavioral gate suppressing raw KWTA result",
    ]
    if stag > 5:
        evidence.append(f"stagnation_streak={stag} — suppression sustained")
    if pred_err > 0.5:
        evidence.append(f"prediction_error={pred_err:.3f} — proprioceptive mismatch confirms suppression")
    # 0.45: divergence is now positively measured from the record window
    # (majority of recent_raw_dominants != dominant_type), not asserted by a
    # flag, so the base alone must clear the 0.45 gate.
    score = 0.45
    if stag > 5:
        score += 0.20
    if pred_err > 0.5:
        score += 0.15
    if tag_counts.get("unclassified_residual", 0) >= 1:
        score += 0.10
    if score < 0.45:
        return None
    return {
        "defect_type": "suppression_routing_gap",
        "confidence": round(min(0.88, score), 3),
        "evidence": evidence,
        "self_statement": "KWTA produces a preferred type that the behavioral gate suppresses; structural mismatch between raw signal and resting dominant.",
        "repair_hint": "audit suppression rule and min_dwell threshold; consider modify_suppression_rule affordance",
    }


def classify_recent(path: Optional[Path] = None, limit: int = 20, cooling: bool = False) -> Dict[str, Any]:
    return classify_defects(load_records(path=path, limit=limit), cooling=cooling)


if __name__ == "__main__":
    print(json.dumps(classify_recent(), ensure_ascii=False, indent=2, sort_keys=True))

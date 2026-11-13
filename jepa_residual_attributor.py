#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ego-JEPA residual attribution v0.

This module turns a JEPA prediction mismatch into structured, low-risk
attribution tags. v0 is deliberately side-effect-light: it does not change
planning, perturbation, repair application, or TL-Ego core state. The optional
append_record helper only writes JSONL audit records.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple


DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / ".lae" / "jepa_residuals.jsonl"


def _num(data: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key, default)
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _dominant(data: Mapping[str, Any]) -> Optional[str]:
    # 2026-11-08 (jepa_feature_missing P0): next_dominant_forecast, when present,
    # is the only honest prediction in the record — next_dominant is the
    # counterfactual TARGET ("what type this action is meant to induce",
    # measured 15% hit rate over 40 rows), so scoring it as a prediction
    # measured action efficacy, not model quality. Prefer the forecast.
    value = data.get("dominant")
    if value is None:
        value = data.get("next_dominant_forecast")
    if value is None:
        value = data.get("next_dominant")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ── 2026-11-08: dominant-type sequence forecast (the missing JEPA feature) ────
# Measured on 12,359 recorded rows: P(next | prev2,prev1) = 45.6%, P(next|prev1)
# = 38.3%, persistence = 35.9%, while the counterfactual-target "prediction"
# scored 15%. The dominant type is strongly autocorrelated; the sequence IS the
# feature the defect report said the world model was missing.
# Table = 12x12 counters, built once per process from the residual log's own
# actual dominants (leak-free: consecutive actuals, never recent_dominants[-1]).
_2GRAM: Optional[Dict[Tuple[str, str], Counter]] = None


def _load_2gram(path: Optional[Path] = None) -> Dict[Tuple[str, str], Counter]:
    global _2GRAM
    if _2GRAM is not None:
        return _2GRAM
    table: Dict[Tuple[str, str], Counter] = {}
    try:
        p = path or DEFAULT_LOG_PATH
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4_000_000))  # ponytail: 4MB tail = ~1.2k rows; full-file read if counts ever too thin
            chunks = f.read().decode("utf-8", "replace").splitlines()
        if chunks and not chunks[0].lstrip().startswith("{"):
            chunks = chunks[1:]  # partial first line from the seek
        seq: List[str] = []
        for ln in chunks:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            dom = (d.get("actual") or {}).get("dominant")
            if isinstance(dom, str) and dom:
                seq.append(dom)
        for i in range(2, len(seq)):
            table.setdefault((seq[i - 2], seq[i - 1]), Counter())[seq[i]] += 1
    except Exception:
        pass
    _2GRAM = table
    return table


def two_gram_forecast(seq: Iterable[str], path: Optional[Path] = None) -> Optional[str]:
    """Most likely next dominant given the pre-tick dominant sequence."""
    s = [str(x).strip() for x in (seq or []) if x]
    s = [x for x in s if x]
    if not s:
        return None
    table = _load_2gram(path)
    if len(s) >= 2:
        counted = table.get((s[-2], s[-1]))
        if counted:
            return counted.most_common(1)[0][0]
    agg: Counter = Counter()
    for (_a, b), counted in table.items():
        if b == s[-1]:
            agg.update(counted)
    if agg:
        return agg.most_common(1)[0][0]
    return s[-1]


def _recent_types(context: Mapping[str, Any]) -> List[str]:
    raw = context.get("recent_dominants") or context.get("dominant_window") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.replace("->", ",").split(",")]
    if not isinstance(raw, Iterable):
        return []
    result: List[str] = []
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _drive(context: Mapping[str, Any], name: str) -> float:
    drives = context.get("drives") or context.get("titan_drive") or {}
    if isinstance(drives, Mapping) and name in drives:
        return _num(drives, name, 0.0)
    return _num(context, f"{name}_drive", _num(context, name, 0.0))


def _stagnation_delta(predicted: Mapping[str, Any], actual: Mapping[str, Any], context: Mapping[str, Any]) -> float:
    if "stagnation_delta" in actual:
        return _num(actual, "stagnation_delta", 0.0)
    before = _num(context, "stagnation", _num(context, "stagnation_streak", 0.0))
    after = _num(actual, "stagnation", _num(actual, "stagnation_streak", before))
    return after - before


def classify_residual(
    predicted: Mapping[str, Any],
    actual: Mapping[str, Any],
    context: Mapping[str, Any],
) -> Dict[str, Any]:
    """Classify a JEPA prediction residual into tags and corrections.

    Inputs are intentionally plain dictionaries so the function can be called
    from scripts, tests, or later TL-Ego integration without import coupling.
    """
    tags: List[str] = []
    corrections: Dict[str, Any] = {}
    errors: Dict[str, Any] = {}
    notes: List[str] = []

    pred_dom = _dominant(predicted)
    actual_dom = _dominant(actual)
    comp = _num(context, "competition", _num(actual, "competition", 0.0))
    conc = _num(context, "concentration", _num(actual, "concentration", 0.0))
    stag = _num(context, "stagnation", _num(context, "stagnation_streak", 0.0))
    recent = _recent_types(context)

    if comp > 0.80 and conc < 0.35 and stag <= 1:
        tags.append("integration_window")
        corrections["perturb"] = False
        corrections["observation_window"] = "extend"
        notes.append("high competition + low concentration + low stagnation; treat as integration window")

    if pred_dom != actual_dom:
        errors["type_mismatch"] = {"predicted": pred_dom, "actual": actual_dom}
        if pred_dom and (pred_dom in recent or actual_dom in recent) and not (pred_dom in recent and actual_dom in recent and len(set(recent)) >= 3):
            tags.append("oscillation_lag")
            corrections["type_mismatch_penalty"] = "reduce"
            corrections.setdefault("observation_window", "extend")
        elif pred_dom and pred_dom in recent and actual_dom in recent and len(set(recent)) >= 3:
            tags.append("oscillation_expected")
            corrections["type_mismatch_penalty"] = "skip"
        else:
            tags.append("type_mismatch")

    # 2026-11-08: next_dominant is the counterfactual TARGET (what the action is
    # meant to induce), never a forecast — keep its miss rate as its own signal.
    # type_mismatch above now reads next_dominant_forecast when present, so
    # without this line the action-efficacy fact would vanish from the record.
    _aspiration = str(predicted.get("next_dominant") or "").strip()
    if _aspiration and actual_dom and _aspiration != actual_dom:
        errors["aspiration_mismatch"] = {"predicted": _aspiration, "actual": actual_dom}

    predicted_stag_delta = _num(predicted, "stagnation_delta", 0.0)
    actual_stag_delta = _stagnation_delta(predicted, actual, context)
    errors["stagnation_error"] = round(actual_stag_delta - predicted_stag_delta, 3)
    if predicted_stag_delta < 0 and actual_stag_delta >= 0 and stag > 2:
        tags.append("stagnation_not_reduced")

    pred_novelty = _num(predicted, "novelty", _num(predicted, "external_novelty", 0.0))
    actual_novelty = _num(actual, "novelty", _num(actual, "external_novelty", pred_novelty))
    novelty_error = round(pred_novelty - actual_novelty, 3)
    errors["novelty_error"] = novelty_error
    if novelty_error > 0.25 and actual_stag_delta >= 0 and "integration_window" not in tags:
        tags.append("false_exploration")
        corrections["downrank_action_template"] = True
        # §JEPA-fix (2026-06-28): During integration_window (comp>0.80, conc<0.35, stagn<=1),
        # novelty overestimation is legitimate integration absorption, not false exploration.
        # False_exploration would downrank the action template, preventing integrator consolidation.
        # Guard prevents false_exploration tag from firing during integration_window phase.

    pred_conc = _num(predicted, "concentration", conc)
    actual_conc = _num(actual, "concentration", conc)
    conc_error = round(pred_conc - actual_conc, 3)
    errors["concentration_error"] = conc_error
    if conc_error > 0.10:
        tags.append("concentration_overestimated")

    pred_comp = _num(predicted, "competition", comp)
    actual_comp = _num(actual, "competition", comp)
    comp_error = round(actual_comp - pred_comp, 3)
    errors["competition_error"] = comp_error
    if comp_error > 0.08:
        tags.append("competition_underestimated")

    novelty_drive = _drive(context, "novelty")
    integration_drive = _drive(context, "integration")
    if (
        novelty_drive > integration_drive
        and actual_dom in {"sensory", "action"}
        and pred_dom in {"integrator", "decider"}
    ):
        tags.append("novelty_override")
        corrections["do_not_mark_integrator_failure"] = True
        notes.append("novelty drive exceeded integration drive and selected sensory/action")

    pressure_counter = _num(context, "integrator_pressure_counter", _num(context, "int_press_counter", 0.0))
    if pred_dom == "integrator" and actual_dom != "integrator" and comp > 0.80 and pressure_counter > 0:
        tags.append("integrator_pressure_delay")
        corrections["wait_ticks"] = "2-4"
        corrections["check_counter_decrement"] = True

    if context.get("repair_old_text_not_found") and context.get("equivalent_logic_present"):
        tags.append("repair_candidate_stale")
        corrections["skip_gate2"] = True

    # Preserve order while removing duplicates.
    deduped_tags = list(dict.fromkeys(tags)) or ["unclassified_residual"]

    return {
        "tags": deduped_tags,
        "correction": corrections,
        "errors": errors,
        "prediction_error": estimate_prediction_error(errors, pred_dom, actual_dom),
        "notes": notes,
    }


def estimate_prediction_error(errors: Mapping[str, Any], predicted_dominant: Optional[str], actual_dominant: Optional[str]) -> float:
    """Return a bounded rough error score for trend monitoring, not grading."""
    score = 0.0
    if predicted_dominant != actual_dominant:
        score += 0.35
    for key, weight in (
        ("stagnation_error", 0.18),
        ("novelty_error", 0.16),
        ("concentration_error", 0.14),
        ("competition_error", 0.17),
    ):
        try:
            score += min(1.0, abs(float(errors.get(key, 0.0)))) * weight
        except (TypeError, ValueError):
            pass
    return round(max(0.0, min(1.0, score)), 3)


def build_record(
    predicted: Mapping[str, Any],
    actual: Mapping[str, Any],
    context: Mapping[str, Any],
    action: str = "unknown",
    tick: Optional[int] = None,
) -> Dict[str, Any]:
    attribution = classify_residual(predicted, actual, context)
    if tick is None:
        raw_tick = context.get("tick", actual.get("tick", predicted.get("tick")))
        try:
            tick = int(raw_tick) if raw_tick is not None else None
        except (TypeError, ValueError):
            tick = None
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tick": tick,
        "action": action,
        "predicted": dict(predicted),
        "actual": dict(actual),
        "context": dict(context),
        "attribution": attribution,
    }


def append_record(record: Mapping[str, Any], path: Optional[Path] = None) -> Path:
    """Append one JSONL audit record and return the path written."""
    target = Path(path) if path is not None else DEFAULT_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return target


def attribute_and_append(
    predicted: Mapping[str, Any],
    actual: Mapping[str, Any],
    context: Mapping[str, Any],
    action: str = "unknown",
    tick: Optional[int] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    record = build_record(predicted, actual, context, action=action, tick=tick)
    append_record(record, path=path)
    return record


if __name__ == "__main__":
    sample = attribute_and_append(
        predicted={"next_dominant": "integrator", "stagnation_delta": -1, "external_novelty": 0.70, "concentration": 0.42, "competition": 0.72},
        actual={"dominant": "sensory", "stagnation_delta": 1, "external_novelty": 0.32, "concentration": 0.30, "competition": 0.86},
        context={"tick": 50, "competition": 0.86, "concentration": 0.29, "stagnation": 0, "recent_dominants": ["sensory", "action", "integrator", "decider"], "novelty_drive": 0.69, "integration_drive": 0.52, "integrator_pressure_counter": 3},
        action="sample_residual_attribution",
    )
    print(json.dumps(sample["attribution"], ensure_ascii=False, indent=2, sort_keys=True))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L6-1 defect-to-repair proposal generator.

Turns a mechanism-level residual defect into a bounded, auditable repair
proposal. This module is intentionally proposal-only: it does not write
repair.json, apply patches, restart TL-Ego, or call external services.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


def _sha256_file(path: str) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_code_fingerprint(target_files: list[str], workspace_root: str = "") -> Dict[str, str]:
    """Compute SHA256 fingerprint for each target file.

    Returns {"relative/path": "sha256:<hex>"}."""
    fingerprints: Dict[str, str] = {}
    for rel in target_files:
        abs_path = rel if not workspace_root else os.path.join(workspace_root, rel)
        try:
            fingerprints[rel] = f"sha256:{_sha256_file(abs_path)}"
        except (OSError, FileNotFoundError):
            fingerprints[rel] = "sha256:MISSING"
    return fingerprints


_MIN_CONFIDENCE = 0.70


def _confidence(defect: Mapping[str, Any]) -> float:
    try:
        return float(defect.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _base(defect: Mapping[str, Any], target_files: list[str] | None = None) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "schema": "tl-ego.l6.repair_proposal.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "residual_to_defect_classifier",
        "autonomy_level": "proposal_only",
        "defect_type": defect.get("primary_defect"),
        "confidence": round(_confidence(defect), 3),
        "diagnosis": defect.get("self_statement") or "No self-statement available.",
        "evidence": list(defect.get("evidence") or [])[:5],
        "safety": {
            "apply_mode": "never_apply_directly",
            "requires_three_gate": True,
            "external_write": False,
            "authenticated_network": False,
            "data_upload": False,
        },
    }
    if target_files:
        base["code_fingerprint"] = compute_code_fingerprint(target_files)
    return base


def _candidate_pool(defect: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = _base(defect, target_files=["projects/tl-ego/tl_ego_server.py"])
    proposal.update({
        "proposal_id": "l6p-candidate-pool-too-narrow",
        "title": "Generate counterfactual affordances before JEPA ranking",
        "target_files": ["projects/tl-ego/tl_ego_server.py"],
        "change_type": "bounded_logic",
        "risk": "low_to_medium",
        "patch_intent": [
            "Read residual_defect when deriving workspace affordances.",
            "If candidate_pool_too_narrow is high-confidence, append 1-2 counterfactual read-only affordances before ranking.",
            "Keep affordances observe/read-only; do not add command execution or external writes.",
        ],
        "expected_effect": [
            "action diversity increases before JEPA ranking",
            "inspect_recent_project_result repeat ratio decreases",
            "false_exploration repeats decrease over the next residual window",
        ],
        "verification": {
            "compile_check": True,
            "status_check": True,
            "unit_tests": [
                "tests/test_residual_to_defect_classifier.py",
                "tests/test_defect_to_repair_proposal.py",
            ],
            "success_criteria": [
                {"metric": "candidate_action_diversity", "operator": "gte", "value": 3, "window": 20},
                {"metric": "top_action_repeat_ratio", "operator": "lt", "value": 0.65, "window": 20},
                {"metric": "false_exploration_count", "operator": "lt", "value": 20, "window": 20},
            ],
        },
        "rollback": "restore .l5bak via scripts/l5_apply_repair.ps1 Gate 2 rollback",
    })
    return proposal


def _observation_window(defect: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = _base(defect, target_files=["projects/tl-ego/tl_ego_server.py"])
    proposal.update({
        "proposal_id": "l6p-observation-window-too-narrow",
        "title": "Use multi-sample status windows for residual attribution",
        "target_files": ["projects/tl-ego/tl_ego_server.py"],
        "change_type": "bounded_logic",
        "risk": "low_to_medium",
        "patch_intent": [
            "Replace single-snapshot residual comparison with a short bounded observation window.",
            "Keep sampling local-only and event-driven; do not add cron jobs.",
            "Store only compact dominant/concentration/competition summaries.",
        ],
        "expected_effect": [
            "oscillation_lag is distinguished from true prediction failure",
            "type_mismatch penalties are reduced during integration windows",
        ],
        "verification": {
            "compile_check": True,
            "status_check": True,
            "unit_tests": ["tests/test_residual_to_defect_classifier.py", "tests/test_defect_to_repair_proposal.py"],
            "success_criteria": [
                {"metric": "window_sample_count", "operator": "gte", "value": 3, "window": 1},
                {"metric": "oscillation_lag_false_alarm_ratio", "operator": "lt", "value": 0.5, "window": 20},
            ],
        },
        "rollback": "restore .l5bak via scripts/l5_apply_repair.ps1 Gate 2 rollback",
    })
    return proposal


def _novelty_calibration(defect: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = _base(defect, target_files=["projects/tl-ego/ego_jepa.py"])
    proposal.update({
        "proposal_id": "l6p-novelty-calibration-drift",
        "title": "Bound external novelty calibration from residual tags",
        "target_files": ["projects/tl-ego/ego_jepa.py"],
        "change_type": "bounded_logic",
        "risk": "low",
        "patch_intent": [
            "Adjust predicted external_novelty using recent false_exploration novelty_error.",
            "Keep the adjustment bounded and reversible.",
            "Do not change action execution permissions.",
        ],
        "expected_effect": [
            "predicted external_novelty tracks actual novelty more closely",
            "false_exploration tags decay for diverse actions",
        ],
        "verification": {
            "compile_check": True,
            "status_check": True,
            "unit_tests": ["tests/test_ego_jepa_residual_ranking.py", "tests/test_defect_to_repair_proposal.py"],
            "success_criteria": [
                {"metric": "avg_novelty_error", "operator": "lt", "value": 0.25, "window": 20},
            ],
        },
        "rollback": "restore .l5bak via scripts/l5_apply_repair.ps1 Gate 2 rollback",
    })
    return proposal


def _jepa_feature_missing(defect: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = _base(defect, target_files=["projects/tl-ego/ego_jepa.py"])
    proposal.update({
        "proposal_id": "l6p-jepa-feature-missing",
        "title": "Inspect residual contexts before adding a bounded JEPA feature",
        "target_files": ["projects/tl-ego/ego_jepa.py"],
        "change_type": "analysis_first",
        "risk": "medium",
        "patch_intent": [
            "Do not patch immediately; first summarize which context feature explains prediction error.",
            "If a feature is stable, add one bounded scalar feature to Ego-JEPA scoring.",
        ],
        "expected_effect": ["prediction_error drops without suppressing exploration"],
        "verification": {
            "compile_check": True,
            "status_check": True,
            "unit_tests": ["tests/test_ego_jepa_residual_ranking.py", "tests/test_defect_to_repair_proposal.py"],
            "success_criteria": [
                {"metric": "avg_prediction_error", "operator": "lt", "value": 0.35, "window": 20},
            ],
        },
        "rollback": "restore .l5bak via scripts/l5_apply_repair.ps1 Gate 2 rollback",
    })
    return proposal


def build_repair_proposal(defect: Mapping[str, Any], min_confidence: float = _MIN_CONFIDENCE, workspace_root: str = "") -> Dict[str, Any]:
    """Return a bounded repair proposal, or a no-op proposal if evidence is weak.

    If workspace_root is provided, code_fingerprint will be computed for all
    target_files and embedded in the proposal dict."""
    confidence = _confidence(defect)
    defect_type = str(defect.get("primary_defect") or "")

    # Determine target files for fingerprinting
    target_files_map = {
        "candidate_pool_too_narrow": ["projects/tl-ego/tl_ego_server.py"],
        "observation_window_too_narrow": ["projects/tl-ego/tl_ego_server.py"],
        "novelty_calibration_drift": ["projects/tl-ego/ego_jepa.py"],
        "jepa_feature_missing": ["projects/tl-ego/ego_jepa.py"],
    }
    known_targets = target_files_map.get(defect_type, [])

    if confidence < min_confidence:
        proposal = _base(defect, target_files=known_targets if known_targets else None)
        proposal.update({
            "proposal_id": "l6p-insufficient-confidence",
            "title": "Collect more residual evidence before proposing self-repair",
            "change_type": "none",
            "risk": "none",
            "patch_intent": [],
            "expected_effect": ["avoid overfitting self-repair to weak residual evidence"],
            "verification": {"compile_check": False, "status_check": False, "success_criteria": []},
        })
        return proposal

    builders = {
        "candidate_pool_too_narrow": _candidate_pool,
        "observation_window_too_narrow": _observation_window,
        "novelty_calibration_drift": _novelty_calibration,
        "jepa_feature_missing": _jepa_feature_missing,
    }
    builder = builders.get(defect_type)
    if builder is None:
        proposal = _base(defect, target_files=None)
        proposal.update({
            "proposal_id": "l6p-unknown-defect",
            "title": "Unknown defect type; require analysis before repair",
            "change_type": "analysis_first",
            "risk": "medium",
            "patch_intent": ["Summarize residual evidence and define a falsifiable repair target before patching."],
            "expected_effect": ["avoid ungrounded self-modification"],
            "verification": {"compile_check": True, "status_check": True, "success_criteria": []},
        })
        return proposal
    proposal = builder(defect)
    # Confidence-based autonomy promotion: high-confidence known-repairable defects
    # are promoted from "proposal_only" to "auto_execute" so L5 can act on them
    # without waiting for Three-Gate review.
    _AUTO_THRESHOLD = 0.85
    _AUTO_DEFECT_TYPES = {
        "candidate_pool_too_narrow",
        "observation_window_too_narrow",
    }
    if confidence >= _AUTO_THRESHOLD and defect_type in _AUTO_DEFECT_TYPES:
        proposal["autonomy_level"] = "auto_execute"
        proposal.setdefault("safety", {})
        proposal["safety"]["apply_mode"] = "l1_auto_approve_eligible"
        proposal["safety"]["requires_three_gate"] = False
    return proposal


if __name__ == "__main__":
    import os as _os
    sample = {
        "primary_defect": "candidate_pool_too_narrow",
        "confidence": 0.94,
        "self_statement": "我的问题不只是某个 action 分数错了，而是候选生成机制缺少反事实替代。",
        "evidence": ["top_action=inspect_recent_project_result repeated 16/20"],
    }
    proposal = build_repair_proposal(sample, workspace_root="C:/Users/Administrator/.openclaw/workspace")
    print(json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True))

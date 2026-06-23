import urllib.request, urllib.parse
import os, sys, atexit, signal, subprocess, gc
from pathlib import Path

# ─── Single-instance lock ───
WORKSPACE = Path(__file__).resolve().parent.parent.parent
PID_FILE = WORKSPACE / ".lae" / "ego.pid"
def _ensure_single_instance():
    try:
        if PID_FILE.exists():
            old_pid = int(PID_FILE.read_text().strip())
            # check process exists (Windows)
            import subprocess
            try:
                out = subprocess.check_output(["tasklist", "/FI", f"PID eq {old_pid}"], stderr=subprocess.DEVNULL, text=True)
                if str(old_pid) in out:
                    print(f"[TL-Ego] Another instance (PID {old_pid}) is already running. Exiting.")
                    sys.exit(0)
            except subprocess.CalledProcessError:
                pass  # PID not found, fall through
            PID_FILE.unlink(missing_ok=True)  # stale regardless
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
        def _cleanup():
            try:
                # P2: persist STDP + ego extras before exit
                if INIT_COMPLETE:
                    try:
                        save_checkpoint()
                    except Exception as _cpe:
                        print(f'[cleanup] save_checkpoint error: {_cpe}', flush=True)
                    try:
                        _save_ego_extras()
                    except Exception as _cpe2:
                        print(f'[cleanup] _save_ego_extras error: {_cpe2}', flush=True)
            except Exception:
                pass
            try:
                if PID_FILE.exists() and int(PID_FILE.read_text()) == os.getpid():
                    PID_FILE.unlink()
            except Exception:
                pass
        atexit.register(_cleanup)
        signal.signal(signal.SIGTERM, lambda s,f: _cleanup() or sys.exit(0))
        signal.signal(signal.SIGINT,  lambda s,f: _cleanup() or sys.exit(0))
    except Exception as e:
        print(f"[TL-Ego] Lock setup error: {e}")

_ensure_single_instance()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-Ego Server v1 - 三环自我持久化全图服务
  Port: 8766 | HTTP JSON API
  Init: ~2 min (load 100K grid + 5.6M edges)
  Query: ~1-5s (full attractor + LLM System 2)

Endpoints:
  GET  /status           -> 网格统计、连接图大小、STDP 步数
  POST /contemplate      -> 全图吸引子分析 + System 2 + Aggregator + STDP
  POST /save-checkpoint   -> 将 STDP 增量持久化到 SQLite
  POST /run-many         -> 多查询批量测试

Phase 1: 全图吸引子分析 (stability > 0.7 目标)
Phase 2: System 1(网络) + System 2(LLM) + Aggregator
Phase 3: 在线 STDP 可塑性 (有界 delta 层 + 周期性持久化)
"""

import sys, json, time, math, random, requests, re, threading
import numpy as np
from pathlib import Path
from collections import Counter, defaultdict
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import traceback
import torch

# ─── Paths ───
WORKSPACE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(Path.home() / ".openclaw" / "skills" / "neural_grid"))

DATA_DIR = Path.home() / ".openclaw" / "skills" / "neural_grid" / "data"
import neural_grid as ng
ng.DATA_DIR = DATA_DIR
ng.NEURON_REGISTRY = DATA_DIR / "neuron_registry.jsonl"
ng.GRID_EDGES_DB = DATA_DIR / "grid_edges.sqlite"
ng.EdgeStore._instance = None; ng._edge_store = None

# ─── Tools Registry ───
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools_registry import resolve_tool, resolve_tool_for_comm, TOOLS
try:
    from ego_jepa import rank_affordances as rank_jepa_affordances, predict_external as jepa_predict_external, predict_outcome as jepa_predict_outcome
except Exception as _ego_jepa_import_error:
    rank_jepa_affordances = None
    jepa_predict_external = None
    jepa_predict_outcome = None
    print(f"[ego-jepa] import failed: {_ego_jepa_import_error}", flush=True)

# ─── API endpoints ───
BGE_URL = "http://127.0.0.1:8002/v1/embeddings"
LLM_URL = "http://127.0.0.1:8001/v1/chat/completions"
GAC_URL = "http://127.0.0.1:18020/enhance"

TYPE_NAMES = ["sensory", "integrator", "decider", "action", "memory", "rule_modification", "veto", "doubt", "curiosity", "planning", "social", "ethical", "aesthetic", "temporal", "emotional", "spatial", "cultural", "meta"]
TYPE_IDS = {t: i for i, t in enumerate(TYPE_NAMES)}

# ─── Global state ───
NEURONS = []           # list of neuron IDs (matched to grid)
# ADJ removed -- EDGE_ACTIVE is the single source of truth
NEURON_EMBED = {}      # id -> normalized embedding vector
NEURON_TYPE = {}       # id -> type string
NEURON_IDX = {}        # id -> row index in ng._embeddings_matrix (for embedding feedback)
NEURONS_MAP = {}       # pre-built full neurons_map for AttractorNetwork step
EDGE_ACTIVE = {}       # (src,dst) -> weight (single source of truth, loaded directly from EdgeStore)
STDP_DELTA = {}        # (src,dst) -> accumulated delta
STDP_TOTAL_UPDATES = 0  # total STDP edge updates across all ticks (for /status)
STDP_CHANGED = set()    # (src,dst) pairs modified by STDP (for efficient save)
EDGE_UPDATE_COUNT = {}  # (src,dst) -> how many times this edge has been updated
                        # §P2: used for salience gating — frequently updated edges
                        # get lower plasticity (metaplasticity: learned = stable)
QUERY_COUNT = 0
EMBED_FEEDBACK_UPDATES = 0   # Phase 6: total neurons updated via embedding feedback
EMBED_FAISS_REBUILDS = 0     # Phase 6: total Faiss index rebuilds
EMBED_CHANGED = set()        # Neuron IDs modified via feedback_embedding (for delta persistence)
PERSISTENT_IDENTITY = {}    # P5: {dominant_type, confidence, profile} -- the Ego's long-term sense of self
METACONTEMPLATE_COUNT = 0  # Phase 4.0: recursive self-observation runs
COGNITIVE_DISSONANCE_STREAK = 0  # Phase 6: consecutive S1-stable + S2-disagree
DISSONANCE_THRESHOLD = 3       # trigger auto-metacontemplate after N streaks
INIT_COMPLETE = False        # becomes True after background init finishes
START_TIME = time.time()
SELF_MODEL_LOCK = threading.Lock()

# ─── Phase 9 removed: no external intent rules. Network dynamics speak for itself. ───


def _read_json_file(path: Path, default):
    if not path.exists() or path.stat().st_size == 0:
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_file_atomic(path: Path, data, backup_path: Path | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if backup_path is not None and path.exists() and path.stat().st_size > 0:
        path.replace(backup_path)
    tmp_path.replace(path)


def _classify_s1_s2_alignment(s1_type: str, s2_type: str, *, s1_conf: float = 0.0, s2_conf: float = 0.0, stability: float = 0.0) -> dict:
    """Classify S1/S2 alignment without collapsing all non-identical roles into disagreement.

    strict_agreement is still the literal same-type check. compatible_agreement captures
    adjacent cognitive pipeline states where S1 and S2 can be describing different
    levels of the same act: input -> choice -> action -> integration.
    """
    strict = s1_type == s2_type
    if strict:
        return {
            "strict_agreement": True,
            "compatible_agreement": True,
            "axis": "same_type",
            "reason": "S1 and S2 name the same cognitive role.",
        }

    pair = (s1_type, s2_type)
    compatibility = {
        ("sensory", "memory"): ("evidence_to_recall", 0.45, "sensory evidence is retrieving or matching memory context"),
        ("sensory", "decider"): ("evidence_to_choice", 0.45, "sensory evidence is being interpreted as a decision cue"),
        ("sensory", "integrator"): ("evidence_to_synthesis", 0.45, "sensory evidence is being synthesized into a wider state"),
        ("memory", "sensory"): ("recall_to_evidence", 0.45, "memory context is grounding or re-reading sensory evidence"),
        ("memory", "decider"): ("recall_to_choice", 0.50, "memory context is being used to decide"),
        ("memory", "action"): ("recall_to_action", 0.55, "memory context is being converted into action"),
        ("memory", "integrator"): ("recall_to_synthesis", 0.50, "memory context is being integrated"),
        ("decider", "action"): ("choice_to_action", 0.50, "a decision is being converted into action"),
        ("action", "decider"): ("action_to_choice", 0.50, "an action tendency is being evaluated by an explicit decision bridge"),
        ("action", "integrator"): ("action_to_synthesis", 0.40, "an action tendency is being integrated into self-narrative"),
        ("integrator", "decider"): ("synthesis_to_choice", 0.45, "an integrated state is producing a decision criterion"),
        ("integrator", "action"): ("synthesis_to_action", 0.55, "an integrated state is being converted into action through an implicit decision bridge"),
        ("integrator", "sensory"): ("synthesis_to_evidence", 0.40, "an integrated state is re-grounding in raw sensory evidence"),
        # rule_modification compatibility
        ("rule_modification", "integrator"): ("rule_proposal_to_synthesis", 0.50, "a rule modification proposal is being integrated into self-narrative"),
        ("rule_modification", "decider"): ("rule_proposal_to_choice", 0.55, "a rule modification proposal is being evaluated for adoption"),
        ("rule_modification", "action"): ("rule_proposal_to_execution", 0.60, "a rule modification is being executed"),
        ("integrator", "rule_modification"): ("synthesis_to_rule_proposal", 0.50, "an integrated state is producing a rule change proposal"),
        ("decider", "rule_modification"): ("choice_to_rule_proposal", 0.55, "a decision is leading to a rule change proposal"),
        ("action", "rule_modification"): ("action_to_rule_proposal", 0.50, "an action is generating feedback that triggers a rule modification"),
        # veto compatibility
        ("veto", "decider"): ("veto_to_choice", 0.50, "a veto is forcing a re-decision"),
        ("veto", "action"): ("veto_to_action", 0.55, "a veto is blocking an action before execution"),
        ("veto", "memory"): ("veto_to_recall", 0.45, "a veto is retrieving precedent from memory"),
        ("decider", "veto"): ("choice_to_veto", 0.50, "a decision is being evaluated for veto"),
        ("action", "veto"): ("action_to_veto", 0.50, "an action is being vetted before execution"),
        ("veto", "rule_modification"): ("veto_to_rule_proposal", 0.45, "a veto is triggering a rule change proposal"),
        ("rule_modification", "veto"): ("rule_proposal_to_veto", 0.50, "a rule change proposal is being vetted"),
        ("integrator", "veto"): ("synthesis_to_veto", 0.45, "an integrated state is generating a veto signal"),
        # doubt compatibility
        ("doubt", "integrator"): ("doubt_to_synthesis", 0.45, "doubt is being integrated into self-narrative"),
        ("doubt", "memory"): ("doubt_to_recall", 0.45, "doubt is triggering memory search for counterexamples"),
        ("doubt", "veto"): ("doubt_to_veto", 0.50, "doubt is escalating to a veto"),
        ("doubt", "decider"): ("doubt_to_choice", 0.50, "doubt is influencing decision-making"),
        ("integrator", "doubt"): ("synthesis_to_doubt", 0.50, "an integrated conclusion is generating self-doubt"),
        ("memory", "doubt"): ("recall_to_doubt", 0.50, "memory retrieval is triggering doubt"),
        ("decider", "doubt"): ("choice_to_doubt", 0.55, "a decision process is generating doubt"),
        ("action", "doubt"): ("action_to_doubt", 0.45, "an action outcome is generating doubt"),
    }
    item = compatibility.get(pair)
    if not item:
        return {
            "strict_agreement": False,
            "compatible_agreement": False,
            "axis": "role_conflict",
            "reason": f"No compatibility rule for {s1_type}->{s2_type}.",
        }

    axis, min_stability, reason = item
    confident_enough = max(float(s1_conf or 0.0), float(s2_conf or 0.0)) >= 0.55
    stable_enough = float(stability or 0.0) >= min_stability
    compatible = confident_enough and stable_enough
    return {
        "strict_agreement": False,
        "compatible_agreement": compatible,
        "axis": axis,
        "reason": reason if compatible else f"Potential {axis}, but confidence/stability is too low.",
        "min_stability": min_stability,
    }


def _build_diagnosis_summary(log: dict) -> dict:
    """Build a human-readable TL-Ego diagnosis from existing signals only."""
    agg = log.get("aggregator", {})
    s1 = log.get("system1", {})
    stdp = log.get("stdp", {})
    emb = log.get("embedding_feedback", {})
    dissonance = log.get("cognitive_dissonance", {})
    lock = log.get("concentration_lock", {})

    s1_type = s1.get("dominant_type", "unknown")
    s2_type = agg.get("s2_type", "unknown")
    verdict = agg.get("verdict", "unknown")
    confidence = float(agg.get("final_confidence", 0) or 0)
    concentration = float(s1.get("concentration", 0) or 0)
    competition = float(s1.get("competition", 0) or 0)
    agreement = bool(agg.get("agreement", False))
    strict_agreement = bool(agg.get("strict_agreement", agreement))
    compatible_agreement = bool(agg.get("compatible_agreement", agreement))
    alignment_axis = agg.get("alignment_axis", "same_type" if strict_agreement else "role_conflict")
    locked = bool(lock.get("locked", False))
    dissonance_streak = int(dissonance.get("streak", 0) or 0)
    stdp_updates = int(stdp.get("updates", 0) or 0)
    embedding_updates = int(emb.get("neurons_updated", 0) or 0)

    if locked:
        risk_level = "high"
        next_action = "trigger perturbation or metacontemplate before accepting the result"
    elif dissonance_streak >= max(1, DISSONANCE_THRESHOLD - 1) or (not compatible_agreement and confidence >= 0.7):
        risk_level = "medium"
        next_action = "run metacontemplate or collect another observation tick to resolve S1/S2 disagreement"
    elif confidence < 0.55:
        risk_level = "medium"
        next_action = "keep observing; avoid strong action until confidence improves"
    else:
        risk_level = "low"
        next_action = "continue normal resting and feedback learning"

    if agreement and strict_agreement:
        summary = f"S1 and S2 strictly agree on a {s1_type} state with {verdict} confidence."
    elif compatible_agreement:
        summary = f"S1 is {s1_type} while S2 reads {s2_type}; this is a compatible alignment via {alignment_axis}."
    else:
        summary = f"S1 is {s1_type} while S2 reads {s2_type}; this is an active self-disagreement signal."

    if concentration >= 0.8:
        summary += " The attractor is concentrated, so monitor for lock-in."
    elif competition >= 0.7:
        summary += " Multiple attractors are competing, so integration is still unsettled."

    confidence_reason = (
        f"verdict={verdict}, confidence={confidence:.2f}, "
        f"concentration={concentration:.2f}, competition={competition:.2f}, "
        f"agreement={agreement}, strict_agreement={strict_agreement}, alignment_axis={alignment_axis}, dissonance_streak={dissonance_streak}"
    )

    return {
        "summary": summary,
        "risk_level": risk_level,
        "next_action": next_action,
        "confidence_reason": confidence_reason,
        "signals": {
            "s1_type": s1_type,
            "s2_type": s2_type,
            "verdict": verdict,
            "confidence": round(confidence, 3),
            "concentration": round(concentration, 3),
            "competition": round(competition, 3),
            "agreement": agreement,
            "strict_agreement": strict_agreement,
            "compatible_agreement": compatible_agreement,
            "alignment_axis": alignment_axis,
            "dissonance_streak": dissonance_streak,
            "stdp_updates": stdp_updates,
            "embedding_updates": embedding_updates,
        },
    }


TTR_STATE_PATH = WORKSPACE / ".lae" / "ttr_state.json"
TTR_TYPE_PHASES = {
    "sensory": 0.00,
    "memory": 1.10,
    "integrator": 2.20,
    "decider": 3.30,
    "action": 4.40,
}


def _ttr_phase_delta(left: float, right: float) -> float:
    return math.atan2(math.sin(left - right), math.cos(left - right))


def _ttr_anneal_advice(semantic_heat: float, coherence: float, compatible: bool) -> str:
    if semantic_heat >= 0.65:
        return "melt"
    if semantic_heat >= 0.35 or (not compatible and coherence < 0.55):
        return "hold_plastic"
    return "anneal"


def _update_ttr_state_from_contemplate(log: dict) -> dict:
    """TTR-Lite sidecar: convert S1/S2 alignment into simulated thermodynamic state.

    This is intentionally conservative: it writes an advisory state file and does
    not mutate TL-Ego STDP directly. Resting may consume the advice as a small
    learning-rate modulation.
    """
    s1 = log.get("system1", {})
    agg = log.get("aggregator", {})
    s1_type = s1.get("dominant_type", "unknown")
    s2_type = agg.get("s2_type", log.get("system2", {}).get("type", "unknown"))
    s1_phase = TTR_TYPE_PHASES.get(s1_type)
    s2_phase = TTR_TYPE_PHASES.get(s2_type)
    if s1_phase is None or s2_phase is None:
        return {"status": "skipped", "reason": f"unknown type {s1_type}->{s2_type}"}

    confidence = float(agg.get("final_confidence", 0.0) or 0.0)
    concentration = float(s1.get("concentration", 0.0) or 0.0)
    competition = float(s1.get("competition", 0.0) or 0.0)
    compatible = bool(agg.get("compatible_agreement", agg.get("agreement", False)))
    intensity = max(0.15, min(1.0, 0.45 + confidence * 0.35 + competition * 0.25 - concentration * 0.10))
    raw_coherence = (math.cos(_ttr_phase_delta(s1_phase, s2_phase)) + 1.0) / 2.0
    coherence = max(0.0, min(1.0, raw_coherence))
    if compatible:
        coherence = min(1.0, coherence + 0.18)
    semantic_heat = max(0.0, min(1.0, (1.0 - coherence) * intensity))

    previous = _read_json_file(TTR_STATE_PATH, {"entropy": 0.0, "tick": 0})
    entropy = max(0.0, min(1.0, 0.72 * float(previous.get("entropy", 0.0) or 0.0) + 0.28 * semantic_heat))
    plasticity = max(0.05, min(1.0, 0.18 + 0.72 * semantic_heat + 0.15 * entropy))
    advice = _ttr_anneal_advice(semantic_heat, coherence, compatible)
    state = {
        "status": "ok",
        "source": "contemplate",
        "tick": int(previous.get("tick", 0) or 0) + 1,
        "resting_tick": RESTING_STATE.get("tick", 0),
        "s1_type": s1_type,
        "s2_type": s2_type,
        "compatible_agreement": compatible,
        "coherence": round(coherence, 4),
        "semantic_heat": round(semantic_heat, 4),
        "entropy": round(entropy, 4),
        "plasticity": round(plasticity, 4),
        "anneal_advice": advice,
        "resting_stdp_mult": round(0.85 + plasticity * 0.30, 4),
        "query_count": QUERY_COUNT,
        "updated_at": time.time(),
    }
    _write_json_file_atomic(TTR_STATE_PATH, state)
    return state


def _load_ttr_resting_advice() -> dict:
    try:
        if not TTR_STATE_PATH.exists():
            return {}
        state = _read_json_file(TTR_STATE_PATH, {})
        if not isinstance(state, dict) or state.get("status") != "ok":
            return {}
        if time.time() - float(state.get("updated_at", 0.0) or 0.0) > 1800:
            return {}
        return state
    except Exception as exc:
        print(f"[ttr] advice load skipped: {exc}", flush=True)
        return {}


def _load_arbitration_policy(rule_name: str):
    """Load one durable arbitration policy for matching cognitive dissonance.

    Kept intentionally small: policies are plain JSON so successful one-off
    arbitration criteria can become reusable Ego strategy without changing code.
    """
    if rule_name != "resolve_disagreement":
        return None
    try:
        policy_path = WORKSPACE / ".lae" / "ego_arbitration_policies.json"
        if not policy_path.exists():
            return None
        data = json.loads(policy_path.read_text(encoding="utf-8"))
        recent = CONTEXT_MEMORY[-2:]
        if len(recent) < 2:
            return None
        s1_types = {cm.get("s1_type") for cm in recent}
        s2_types = {cm.get("s2_type") for cm in recent}
        verdicts = " ".join(str(cm.get("verdict", "")) for cm in recent)
        for policy in data.get("policies", []):
            if not policy.get("enabled", True):
                continue
            match = policy.get("match", {})
            if match.get("s1_type") and match["s1_type"] not in s1_types:
                continue
            if match.get("s2_any_of") and not (set(match["s2_any_of"]) & s2_types):
                continue
            if match.get("verdict_contains") and match["verdict_contains"] not in verdicts:
                continue
            return policy
    except Exception as _pe:
        print(f"[policy] arbitration load skip: {_pe}", flush=True)
    return None


def _update_arbitration_policy_from_feedback(body: dict):
    """Adjust durable arbitration policy weights from explicit or bridge-attributed feedback.

    Safety boundaries:
    - prefer explicit policy_id/policy_applied in feedback
    - otherwise infer only from the latest L5 bridge when task_id matches its intent
    - only changes one numeric weight in a small clamped range
    - appends an audit entry for reversibility/debugging
    """
    policy_id = body.get("policy_id") or body.get("policy_applied")
    attribution_source = "explicit"
    fb_text = " ".join(str(body.get(k, "")) for k in ("task_id", "outcome", "judge", "reason"))
    if not policy_id and "memory_vs_action_v1" in fb_text:
        policy_id = "memory_vs_action_v1"
        attribution_source = "text"
    if not policy_id:
        try:
            bridge_path = WORKSPACE / ".l5_bridge.json"
            if bridge_path.exists():
                bridge = json.loads(bridge_path.read_text(encoding="utf-8"))
                applied = bridge.get("policy_applied") or {}
                bridge_policy_id = applied.get("id")
                bridge_task_id = f"intent_{bridge.get('tick')}_{bridge.get('intent_type')}"
                incoming_task_id = str(body.get("task_id", ""))
                if bridge_policy_id and incoming_task_id and incoming_task_id == bridge_task_id:
                    policy_id = bridge_policy_id
                    attribution_source = "l5_bridge_task_id"
        except Exception as _ae:
            print(f"[policy-learn] attribution skip: {_ae}", flush=True)
    if not policy_id:
        return None
    try:
        policy_path = WORKSPACE / ".lae" / "ego_arbitration_policies.json"
        if not policy_path.exists():
            return None
        data = json.loads(policy_path.read_text(encoding="utf-8"))
        judge = str(body.get("judge", "")).lower()
        if judge in ("success", "correct", "positive", "delayed_success"):
            delta = 0.05
        elif judge in ("failed", "failure", "incorrect", "negative"):
            delta = -0.05
        else:
            return None
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        for policy in data.get("policies", []):
            if policy.get("id") != policy_id:
                continue
            old_weight = float(policy.get("action_weight_multiplier", 1.0) or 1.0)
            new_weight = round(max(1.0, min(1.8, old_weight + delta)), 3)
            policy["action_weight_multiplier"] = new_weight
            policy["learned_updates"] = int(policy.get("learned_updates", 0) or 0) + 1
            policy.setdefault("learning_log", []).append({
                "ts": now,
                "task_id": body.get("task_id", ""),
                "judge": body.get("judge", ""),
                "old_weight": old_weight,
                "new_weight": new_weight,
                "delta": delta,
                "reason": body.get("reason", ""),
                "attribution_source": attribution_source,
            })
            policy["learning_log"] = policy["learning_log"][-50:]
            data["updated_at"] = now
            tmp = policy_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(policy_path)
            return {"policy_id": policy_id, "old_weight": old_weight, "new_weight": new_weight, "delta": delta, "attribution_source": attribution_source}
    except Exception as _pe:
        print(f"[policy-learn] skip: {_pe}", flush=True)
    return None


# ─── Phase 5: Context Memory (跨查询连贯自我感) ───
# Ring buffer: last N contemplation results for cross-query continuity
CONTEXT_MEMORY = []          # list of {query, s1_type, s2_type, verdict, confidence, ts}
CONTEXT_MEMORY_SIZE = 8      # keep last 8 queries
SELF_NARRATIVE = ""          # evolving self-description updated each query
ACCUMULATED_NARRATIVE = ""   # §16-A: LLM-compressed autobiography from fingerprints
ACCUMULATED_NARRATIVE_TS = ""  # timestamp of last accumulated narrative update
LAST_NARRATIVE_FINGERPRINT_COUNT = 0  # track how many fingerprints were in last narrative build
ATTENTION_SCHEMA = ""          # §20: attention schema — Ego's model of its own attention
LAST_ATTENTION_SCHEMA_TICK = 0  # last tick when attention schema was updated

# §21: Hierarchical Predictive Coding — multi-level prediction hierarchy
# Level 1 (low): predicts STDP change rate
# Level 2 (mid): predicts dominant shift + concentration
# Level 3 (high): predicts narrative coherence / fragmentation risk
# Prediction errors flow upward; priors flow downward (HPC feedback loop)
HPC_STATE = {
    "last_tick": 0,
    # Level 1: STDP sensor
    "l1_stdp_change_rate": 0.0,        # current STDP change rate
    "l1_predicted_rate": 0.0,           # predicted next STDP rate
    "l1_error": 0.0,                    # prediction error
    "l1_prior_sensitivity": 0.5,        # adjusted by L2 error
    # Level 2: Attractor dynamics
    "l2_predicted_dominant": "unknown", # predicted next dominant
    "l2_predicted_conc": 0.5,           # predicted next concentration
    "l2_predicted_comp": 0.5,           # predicted next competition
    "l2_dominant_surprise": 0.0,        # 1.0 if dominant shifted unexpectedly
    "l2_conc_error": 0.0,               # |actual - predicted| concentration
    "l2_prior_confidence": 0.5,         # adjusted by L3 error
    # Level 3: Narrative coherence
    "l3_narrative_fragmentation": 0.0,  # current fragmentation (0=coherent, 1=fragmented)
    "l3_predicted_fragmentation": 0.0,  # predicted fragmentation
    "l3_error": 0.0,                    # prediction error
    "l3_narrative": "",                 # HPC self-narrative (1-2 sentences)
    # Tracking
    "trajectory_buffer": [],            # last 10 entries for multi-scale analysis
    "recent_stdp_deltas": [],           # last 10 STDP change deltas
}

# §22: Active Inference / Expected Free Energy — unified drive system
# EFE = Epistemic Value + Pragmatic Value - Complexity
# Replaces hand-crafted drive rules with a unified objective function
EFE_STATE = {
    "last_tick": 0,
    # Core EFE components
    "epistemic_value": 0.5,      # information gain expected from action
    "pragmatic_value": 0.5,      # goal achievement / utility
    "complexity_cost": 0.3,      # KL divergence from preferred state
    "total_efe": 0.0,            # EFE = epistemic + pragmatic - complexity
    # EFE-derived drives (replaces stage2 homeostatic drives)
    "efe_exploration": 0.1,      # drive to seek novel information
    "efe_integration": 0.1,      # drive to synthesize competing attractors
    "efe_novelty": 0.1,          # drive to resolve prediction errors
    "efe_stability": 0.1,        # drive to maintain preferred attractor basin
    # Preferred state (homeostatic setpoint)
    "preferred_concentration": 0.55,
    "preferred_competition": 0.45,
    # Tracking
    "efe_narrative": "",          # 1-2 sentence EFE self-analysis
    "history": [],                # last 20 EFE snapshots
}

# §23: Global Workspace Theory — consciousness as broadcast competition
# AS, HPC, EFE each produce a "broadcast candidate" every tick
# The candidate with highest urgency wins the global workspace
# Winner's narrative dominates S2 prompt; losers go to subconscious buffer
# If a system loses 3 consecutive rounds, its cached content decays
GWT_STATE = {
    "last_tick": 0,
    # Broadcast candidates for this round
    "candidates": {
        "attention_schema": {"narrative": "", "urgency": 0.0},
        "hpc": {"narrative": "", "urgency": 0.0},
        "efe": {"narrative": "", "urgency": 0.0},
    },
    # Winner of this round
    "winner": "",                    # "attention_schema" | "hpc" | "efe"
    "winner_narrative": "",          # the winning narrative injected into S2
    "winner_urgency": 0.0,
    # Subconscious buffer: losers cached here
    "subconscious": {
        "attention_schema": {"content": "", "streak": 0, "decay": 1.0},
        "hpc": {"content": "", "streak": 0, "decay": 1.0},
        "efe": {"content": "", "streak": 0, "decay": 1.0},
    },
    # Broadcast history for meta-analysis
    "broadcast_log": [],              # last 20 winners
    "gwt_narrative": "",              # 1-2 sentence GWT self-analysis
}

# §24: Higher-Order Thought Theory — thinking about thinking
# Rosenthal (2005): consciousness arises when a first-order mental state
# is represented by a higher-order thought. Ego doesn't just have states;
# it knows it has states, and can question whether its thinking is correct.
HOT_STATE = {
    "last_tick": 0,
    "hot_narrative": "",              # second-order judgment (1-2 sentences)
    "bias_detected": [],              # detected cognitive biases
    "confidence_in_self": 0.5,        # how much Ego trusts its own judgment
    "self_question_count": 0,         # how many times Ego has questioned itself
    "hot_log": [],                    # last 10 HOT entries
}

# ─── P3: Causal Attribution ───
# Tracks which internal dynamics (noise, phase, diversity, evidence)
# correlate with positive vs negative outcomes. Ego learns to attribute
# outcomes to its own internal parameters, building a causal self-model.
CAUSAL_ATTRIBUTION = {
    "last_tick": 0,
    "observations": [],          # last 20 causal observations
    "max_observations": 20,
    "top_positive_factors": [],  # [(factor, correlation), ...]
    "top_negative_factors": [],  # [(factor, correlation), ...]
    "causal_narrative": "",      # 1-2 sentence causal self-explanation
    "attribution_confidence": 0.5,  # how much Ego trusts its causal model
    "total_observations": 0,
}

# ─── P5: Intrinsic Motivation ───
# Five intrinsic drives computed from trajectory + self-model.
# These push the ego toward self-directed behavior, not just external reactivity.
INTRINSIC_MOTIVATION = {
    "last_tick": 0,
    # Five core drives (0-1)
    "curiosity": 0.5,         # drive to explore unknown states/patterns
    "competence": 0.5,        # drive to achieve mastery over a domain
    "novelty": 0.5,           # drive to seek new experiences
    "autonomy": 0.5,          # drive to act independently of external input
    "relatedness": 0.5,       # drive to connect with commander/other entities
    # Derived signals
    "dominant_drive": "curiosity",  # which drive is currently strongest
    "drive_stability": 0.5,   # how stable are the drives (low = changing rapidly)
    "narrative": "",          # 1-2 sentence self-explanation of motivation
    "update_count": 0,        # how many times this has been computed
    # Drive history for trend analysis
    "drive_history": [],      # last 20 {curiosity, competence, novelty, autonomy, relatedness}
}

# ─── §25: Self-Model Evolution ───
# Ego modifies its own parameters based on causal attribution (P3) and
# intrinsic motivation (P5). Gradual, tracked, rollback-capable.
SELF_MODEL_EVOLUTION = {
    "last_tick": 0,
    "enabled": True,                # master switch
    "eval_interval": 20,            # ticks between evaluations
    "param_delta_max": 0.05,        # max single-step parameter change
    "param_delta_min": 0.005,       # min single-step parameter change
    "rollback_window": 3,           # number of consecutive worsening evals to rollback
    # Tracked parameters (name -> {current, default, min, max, history})
    "parameters": {
        "explore_stdp_mult": {
            "current": 0.3, "default": 0.3, "min": 0.05, "max": 0.8,
            "desc": "STDP learning rate during explore phase",
            "history": []  # [{tick, old, new, reason, outcome_after}]
        },
        "diversity_n": {
            "current": 8, "default": 8, "min": 2, "max": 20,
            "desc": "Seed diversity during explore phase",
            "history": []
        },
        "action_heater_ratio": {
            "current": 1.0, "default": 1.0, "min": 0.0, "max": 3.0,
            "desc": "How aggressively defense→action neurons are replaced (1.0=default)",
            "history": []
        },
        "noise_amp_boost": {
            "current": 0.0, "default": 0.0, "min": -0.1, "max": 0.3,
            "desc": "Extra noise amplitude added to explore phase",
            "history": []
        },
        "evidence_persist_ticks": {
            "current": 10, "default": 10, "min": 3, "max": 30,
            "desc": "How long external evidence survives (ticks)",
            "history": []
        },
        "alpha_seed_mix": {
            "current": 0.5, "default": 0.5, "min": 0.1, "max": 0.9,
            "desc": "Seed mixing ratio (how much to keep from previous tick)",
            "history": []
        },
        "newborn_inject_n": {
            "current": 2, "default": 2, "min": 0, "max": 6,
            "desc": "Number of newborn guarantee seeds to inject",
            "history": []
        },
    },
    # Evolution log
    "evolution_log": [],       # [{tick, param, old, new, reason, causal_factor, drive}]
    "consecutive_worse": 0,     # counter for rollback detection
    "last_eval_avg_outcome": 0.5,
    "narrative": "",           # 1-2 sentence self-evolution narrative
}

# ─── P4: Sleep-Replay ───
# Offline consolidation: sample high-PE fragments during active hours,
# replay them through STDP with boosted learning rate during quiet hours (23:00-08:00).
# This compresses the fast-changing layer into the slow-changing layer,
# reducing STDP noise and preventing attractor drift.
SLEEP_REPLAY = {
    "enabled": True,               # master switch
    "buffer": [],                  # [{tick, fragment: [seed_ids], pe, dominant, conc, competition}]
    "buffer_max": 50,              # max fragments before eviction (FIFO)
    "pe_threshold": 0.4,           # only record fragments with PE > this
    "replay_boost": 3.0,           # STDP learning rate multiplier during replay
    "replay_per_cycle": 3,         # how many fragments to replay per quiet-hour tick
    "quiet_start": 23,             # quiet hours start (local time, inclusive)
    "quiet_end": 8,                # quiet hours end (local time, exclusive)
    "last_replay_tick": 0,         # last tick where replay was performed
    "total_fragments_recorded": 0,
    "total_fragments_replayed": 0,
    "replay_log": [],              # [{tick, fragment_pe, stdp_delta_count}]
}

# ─── §26: Integrated Information Theory (IIT) ───
# Practical approximation of Φ (phi) for the STDP network.
# Φ measures how much information the system generates as a whole
# beyond the sum of its parts. Higher Φ = more integrated consciousness.
# Implementation: simplified Φ based on type-to-type transition entropy
# and the information loss when partitioning the network.
IIT_STATE = {
    "enabled": True,               # master switch
    "last_tick": 0,                # last tick where Φ was computed
    "compute_interval": 20,        # ticks between Φ computations
    "phi_total": 0.0,              # current Φ (bits)
    "phi_history": [],             # [{tick, phi, partition, n_types}]
    "phi_history_max": 50,         # max history entries
    "type_mi_matrix": {},          # mutual information between type pairs
    "best_partition": "",           # partition that minimizes Φ
    "complexity": 0.0,             # neural complexity (Tononi-Sporns-Edelman)
}

# ─── P6: Meta-Learning — STDP parameters self-tune based on prediction error and stagnation ───
# Tracks PE and stagnation trends, adjusts HEBB_LEARNING_RATE, TYPE_BONUS, AGREE_BONUS.
# Goal: higher PE → more plasticity; high stagnation → perturb parameters to escape attractor.
META_LEARNING = {
    "enabled": True,
    "last_tick": 0,
    "compute_interval": 20,        # ticks between meta-adaptation
    "pe_history": [],               # [{tick, pe, stagnation}]
    "pe_history_max": 30,
    "adapt_log": [],                # [{tick, param, old_val, new_val, reason}]
    "adapt_log_max": 50,
    # Parameter bounds
    "bounds": {
        "HEBB_LEARNING_RATE": [0.01, 0.20],
        "EMBED_FEEDBACK_RATE": [0.005, 0.10],
        "EMBED_FEEDBACK_REPEL": [0.05, 0.40],
        "TYPE_BONUS": [1.0, 4.0],
        "AGREE_BONUS": [0.5, 3.0],
    },
    "step_size": 0.10,              # relative adjustment per step
}

# ─── Emergence: Hebbian Aggregator (replaces rule-based verdict) ───
# 5 verdict channels compete via Hebbian weights. No hardcoded rules.
# Weights evolve based on S1/S2 signal strength -- the system LEARNS its own verdict logic.
HEBB_AGG_WEIGHTS = {
    "high_confidence_agree": 0.20,
    "medium_confidence_agree": 0.20,
    "low_confidence_agree": 0.20,
    "medium_confidence_disagree": 0.20,
    "low_confidence": 0.20,
}
HEBB_AGG_HISTORY = []  # ring buffer of last 50 verdict outcomes for learning curve
HEBB_AGG_HISTORY_SIZE = 50
HEBB_LEARNING_RATE = 0.05  # how fast weights adapt

# ─── Thread lock for resting_tick (prevents noise_mode rotation race between auto-loop and HTTP) ───
import threading as _resting_th
RESTING_LOCK = _resting_th.Lock()
STDP_LOCK = _resting_th.Lock()
CHECKPOINT_LOCK = _resting_th.Lock()  # serializes save_checkpoint writes to avoid SQLite locked

# ─── Resting state (chronic oscillation, v3.4 -- multi-modal noise + annealing) ───
RESTING_STATE = {
    "enabled": False,
    "tick": 0,
    "last_seeds": [],
    "last_dominant": "unknown",
    "trajectory": [],       # ring buffer: last 100 entries

    # ── v3.4: Noise scheduling & basin-jumping ──
    "noise_mode": "white",            # current noise mode: white|pink|sparse|none
    "noise_modes": ["white", "pink", "pink", "pink", "sparse", "none"],  # weighted: pink 50%
    "noise_rotation_idx": 0,            # persistent index for rotation (avoids duplicate trap)
    "noise_phase": 0,                  # tick counter within current cycle
    "noise_schedule": "explore",       # current phase: explore|consolidate|burst
    "schedule_phases": [               # {ticks, amp_range}
        {"name": "explore",    "ticks": 4, "amp": [0.50, 0.80]},
        {"name": "consolidate", "ticks": 2, "amp": [0.10, 0.20]},
        {"name": "explore",    "ticks": 4, "amp": [0.40, 0.70]},
        {"name": "consolidate", "ticks": 1, "amp": [0.05, 0.15]},
        {"name": "burst",      "ticks": 1, "amp": [1.00, 1.40]},
    ],
    "schedule_idx": 0,                # index into schedule_phases
    "schedule_tick_in_phase": 0,       # ticks elapsed in current schedule phase

    # ── Stagnation detection ──
    "stagnation_streak": 0,            # consecutive ticks with same dominant
    "stagnation_threshold": 6,         # trigger burst after this many

    # ── Pink noise state (cumulative) ──
    "pink_state": None,                # numpy array, persistent drift vector
    "pink_alpha": 0.92,                # 1/f rolloff factor (0.75->0.92: closer to true pink noise)

    # ── Sensory queue (external world -> neuron seeds) ──
    "sensory_queue": [],                # list of {text, timestamp} -- consumed by auto loop

    # ── External evidence injection (files consumed by resting_tick) ──
    "external_evidence": {},              # {} or {key: content} from consumed evidence files
    "evidence_ttl": {},                   # {key: remaining_ticks} -- evidence persists for N ticks
    "evidence_persist_ticks": 10,          # How many ticks each evidence entry survives (P7: longer persistence for integrator)

    # ── FNO noise decision (Phase: FNO) ──
    "fno_active": False,                 # True when FNO model is loaded and active
    "fno_next_noise_mode": None,          # FNO-chosen noise mode for next tick
    "fno_next_noise_amp": None,           # FNO-chosen noise amplitude for next tick
    "fno_prediction_cache": None,         # Last FNO scenario predictions (for debugging)
    "fno_fallback_streak": 0,            # Consecutive fallback ticks

    # ── Communication motivation (intrinsic drive to message commander) ──
    # Ego decides when to speak, not triggered by external thresholds.
    # comm_motivation builds up from novelty/time/insight, decays naturally.
    "comm_motivation": 0.0,              # current drive to communicate (0.0-1.0)
    "last_comm_tick": 0,                 # tick of last message sent
    "prev_comm_stagnation": 0,            # stagnation at last message (for novelty calc)
    "prev_comm_dominant": "unknown",      # dominant at last message
    "prev_comm_phase": "explore",         # phase at last message
    "comm_suppress_until": 0,            # tick until which ego chooses to stay quiet
    "comm_message_count": 0,             # total messages sent (lifetime)
}

# ─── P1: Action-Learning Coupling ───
# Track recent actions and their impact on PE/stagnation/dominant
# Each entry: {action_id, pre_tick, pre_pe, pre_stagnation, pre_dominant, observe_until}
ACTION_TRACKER = []
ACTION_IMPACT_PATH = WORKSPACE / ".lae" / "action_impact.jsonl"
WEB_EXPLORE_LAST_PATH = WORKSPACE / ".lae" / "web_explore_last.json"

RESTING_LOG_PATH = Path(__file__).parent / "resting_trajectory.jsonl"
# Phase 9/11 removed -- network dynamics speak for itself

# ─── STDP params ───
A_PLUS = 0.005          # LTP rate
A_MINUS = 0.00525       # LTD rate (slightly stronger)
TAU = 20                # time constant
ETA_CHL = 0.001         # new edge init
PRUNE_THRESH = 0.001
SAVE_INTERVAL = 10      # persist every N queries

# ─── Concentration lock detection (anti-type-collapse) ───
CONCENTRATION_LOCK_COUNT = 0       # consecutive S1=S2 + conc >= 0.80
CONCENTRATION_LOCK_THRESHOLD = 3   # inject perturbation after this
PERTURBATION_SCALE = 0.15           # stddev of Gaussian noise on locked embedding


def init():
    """Full init: load grid -> full adjacency -> pre-compute."""
    global NEURONS, NEURON_EMBED, NEURON_TYPE, NEURONS_MAP, EDGE_ACTIVE, NEURON_IDX
    t0 = time.time()

    # 1. Init grid (loads SQLite + embeddings into memory)
    ng.init_grid(preload_partitions=False)
    print(f"[init] Grid loaded: {len(ng._neuron_ids_list)} neurons ({time.time()-t0:.1f}s)", flush=True)

    # 2. Neuron data
    NEURONS = list(ng._neuron_ids_list)
    for idx, nid in enumerate(NEURONS):
        emb = ng._embeddings_matrix[idx]
        typ = nid.split("-")[1] if "-" in nid else "unknown"
        if typ in TYPE_NAMES:
            NEURON_TYPE[nid] = typ
        else:
            NEURON_TYPE[nid] = "unknown"
        NEURON_EMBED[nid] = emb

    # 3. EDGE_ACTIVE: lazy-loaded on first access per neuron (avoid OOM on init)
    global _edge_store_singleton, _lazy_edge_lock
    _edge_store_singleton = None
    _lazy_edge_lock = None
    import threading as _th
    _lazy_edge_lock = _th.RLock()
    print(f"    [init] EDGE_ACTIVE deferred (lazy-load on first query access)", flush=True)

    # 4. NEURONS_MAP (needed by AttractorNetwork)
    for nid in NEURONS:
        if nid in NEURON_EMBED:
            NEURONS_MAP[nid] = NEURON_EMBED[nid]
    print(f"    [init] NEURONS_MAP built: {len(NEURONS_MAP)} neurons", flush=True)

    # 4.5 NEURON_IDX for embedding feedback
    global NEURON_IDX
    NEURON_IDX = ng._neuron_id_to_index if ng._neuron_id_to_index else {}
    if not NEURON_IDX:
        NEURON_IDX = {nid: idx for idx, nid in enumerate(NEURONS)}
    print(f"    [init] NEURON_IDX built: {len(NEURON_IDX)} entries", flush=True)

    # 6. Resting state seeds
    for t in TYPE_NAMES:
        typed = [nid for nid, nt in NEURON_TYPE.items() if nt == t]
        RESTING_STATE["last_seeds"].extend(random.sample(typed, min(5, len(typed))))
    print(f"    [init] Resting seeds: {len(RESTING_STATE['last_seeds'])} neurons", flush=True)


# ─── Memory Search (direct SQLite, zero LLM dependency) ───
MEMORY_DB = Path.home() / ".openclaw" / "memory" / "memu.db"



# ─── Lazy Edge Loading (avoid OOM at init) ────────────────────────────────
_edge_store_singleton = None
_lazy_edge_lock = None

def _get_edge_store():
    """Thread-safe singleton EdgeStore."""
    global _edge_store_singleton
    if _edge_store_singleton is None:
        _edge_store_singleton = ng.EdgeStore()
    return _edge_store_singleton

def _get_edges_lazy(nid: str) -> list:
    """Load edges for a single neuron from EdgeStore on first access. Caches in EDGE_ACTIVE."""
    if nid not in EDGE_ACTIVE:
        try:
            store = _get_edge_store()
            data = store.get_outgoing_edges(nid)
            if data:
                outgoing = [(dst, float(w)) for dst, w in data if w > 0.001]
                with _lazy_edge_lock:
                    EDGE_ACTIVE[nid] = outgoing
            else:
                with _lazy_edge_lock:
                    EDGE_ACTIVE[nid] = []
        except Exception as e:
            with _lazy_edge_lock:
                EDGE_ACTIVE[nid] = []
    return EDGE_ACTIVE.get(nid, [])

def _ensure_edges_batch(nids: list) -> None:
    """Pre-load edges for a batch of neuron IDs (called before attractor step)."""
    missing = [n for n in nids if n not in EDGE_ACTIVE]
    if not missing:
        return
    try:
        store = _get_edge_store()
        batch_data = store.batch_all_edges(missing)
        with _lazy_edge_lock:
            for src, data in batch_data.items():
                outgoing = [(dst, float(w)) for dst, w in data.get("out", []) if w > 0.001]
                if outgoing:
                    EDGE_ACTIVE[src] = outgoing
                elif src not in EDGE_ACTIVE:
                    EDGE_ACTIVE[src] = []
    except Exception:
        with _lazy_edge_lock:
            for n in missing:
                if n not in EDGE_ACTIVE:
                    EDGE_ACTIVE[n] = []
def _memory_search(query: str, limit: int = 5) -> str:
    """Query agent-memory SQLite directly. Returns formatted context string.
    No LLM, no network call, pure local SQLite. Gracefully returns '' on failure."""
    try:
        if not MEMORY_DB.exists():
            return ""
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(MEMORY_DB))
        conn.row_factory = _sqlite3.Row
        cur = conn.cursor()
        # Tokenize: split on whitespace, match ANY keyword (handles Chinese phrases + English)
        tokens = [t for t in query.strip().split() if len(t) >= 2]
        if not tokens:
            return ""
        clauses = []
        params = []
        for tok in tokens:
            p = f"%{tok}%"
            clauses.append("(summary LIKE ? OR content LIKE ?)")
            params.extend([p, p])
        where_sql = " OR ".join(clauses)
        params.append(limit)
        cur.execute(
            f"SELECT summary, content, memory_type, confidence, recalls "
            f"FROM memory_items WHERE {where_sql} "
            "ORDER BY (salience * 0.4 + confidence * 10 + recalls * 0.1) DESC "
            "LIMIT ?",
            params
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return ""
        parts = []
        for r in rows:
            text = r["summary"] or r["content"] or ""
            parts.append(f"  [{r['memory_type']}|conf={r['confidence']}|recalls={r['recalls']}] {text[:200]}")
        return "[Past Experience]\n" + "\n".join(parts)
    except Exception:
        return ""


def web_search_tavily(query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
    """Search the web via Tavily API. Returns structured results.

    This gives TL-Ego real external information access, complementing
    the local memory search. Calls the existing tavily_search.py script
    via subprocess to avoid adding new dependencies to the server process.

    Args:
        query: search query string
        max_results: number of results to return (default 5)
        search_depth: 'basic' (fast, 1-2s) or 'advanced' (comprehensive, 5-10s)

    Returns:
        dict with keys: success, answer, results, error
    """
    try:
        script_path = Path.home() / ".openclaw" / "skills" / "tavily" / "scripts" / "tavily_search.py"
        if not script_path.exists():
            return {"success": False, "error": f"tavily script not found: {script_path}", "answer": "", "results": []}
        cmd = [
            sys.executable, str(script_path),
            query,
            "--max-results", str(max_results),
            "--depth", search_depth,
            "--json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=str(script_path.parent))
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            return {"success": False, "error": f"tavily exit {proc.returncode}: {err[:300]}", "answer": "", "results": []}
        out = proc.stdout.strip()
        # Try to parse JSON from the script output
        # The script may wrap JSON in a header line like "results:" or similar
        json_start = out.find('{')
        if json_start < 0:
            return {"success": False, "error": f"no JSON in tavily output: {out[:300]}", "answer": "", "results": []}
        data = json.loads(out[json_start:])
        answer = data.get("answer", "") or ""
        raw_results = data.get("results", []) or []
        results = []
        for item in raw_results[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": (item.get("content", "") or "")[:500],
                "score": item.get("score", 0.0),
            })
        return {"success": True, "answer": answer, "results": results, "error": ""}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "tavily search timed out", "answer": "", "results": []}
    except Exception as e:
        return {"success": False, "error": str(e), "answer": "", "results": []}



def get_embedding(text: str) -> list:
    r = requests.post(BGE_URL, json={"input": text, "model": "default"}, timeout=30)
    return r.json()["data"][0]["embedding"]


def find_ann_seeds(query_emb: list, k: int = 7) -> list:
    """Phase 5 + Phase 2: Semantic ANN seeds with guaranteed type diversity.
    Rebalances seed types so no single type dominates > floor(k/2)+1 seeds.
    
    §21 (2026-06-21): Defense types (veto/doubt/rule_modification) excluded from
    hard type diversity injection. They have 60K neurons vs action's 20K, creating
    a numerical imbalance that causes emergent analysis paralysis. Let them compete
    on merit through ANN similarity, not guaranteed representation."""
    raw = ng.find_seed_neurons(query_emb, top_k=k * 3)  # extra buffer for diversity
    max_per_type = (k // 2) + 1  # e.g. k=7 -> max 4 per type, guaranteeing >=2 types
    type_counts: dict[str, int] = {}
    result: list[str] = []
    for nid in raw:
        typ = NEURON_TYPE.get(nid, "unknown")
        count = type_counts.get(typ, 0)
        if count < max_per_type:
            type_counts[typ] = count + 1
            result.append(nid)
        if len(result) >= k:
            break
    
    # ── Hard type diversity: ensure at least 1 seed per available type ──
    # §21: Exclude defense types (veto/doubt/rule_modification) from forced injection.
    # They have 60K/170K neurons and don't need artificial boosts.
    _defense_types = {"veto", "doubt", "rule_modification"}
    all_types = set(NEURON_TYPE.values())
    present_types = set(type_counts.keys())
    missing_types = all_types - present_types - {"unknown"} - _defense_types
    injected = 0
    if missing_types:
        # Find one neuron per missing type from the grid
        for nid in raw:
            typ = NEURON_TYPE.get(nid, "unknown")
            if typ in missing_types:
                result.append(nid)
                type_counts[typ] = type_counts.get(typ, 0) + 1
                missing_types.discard(typ)
                injected += 1
        # If still missing, brute-force search full grid (1 seed each, no boost)
        if missing_types and ng._neuron_ids_list:
            for nid in ng._neuron_ids_list:
                typ = NEURON_TYPE.get(nid, "unknown")
                if typ in missing_types:
                    result.append(nid)
                    type_counts[typ] = type_counts.get(typ, 0) + 1
                    missing_types.discard(typ)
                    injected += 1
                    if not missing_types:
                        break
    
    # fallback: pad with remaining raw seeds if diversity filtering created a gap
    if len(result) < k + injected:
        for nid in raw:
            if nid not in result:
                result.append(nid)
                if len(result) >= k + injected:
                    break
    return result[:k + injected]

def parse_type(nid: str) -> str:
    return NEURON_TYPE.get(nid, "unknown")


def run_attractor_kwta(seeds: list, propagation_rounds: int = 15,
                      top_k: int = 50) -> dict:
    """
    KWTA attractor dynamics on FULL graph.
    Uses AttractorNetwork.step() with full adjacency.
    Returns type concentration + activation stats.
    """
    # Build initial activations from seed neuron embeddings
    query_vec = None
    init_acts = []
    for nid in seeds:
        emb = NEURON_EMBED.get(nid)
        if emb is not None:
            emb_list = emb.tolist() if hasattr(emb, 'tolist') else list(emb)
            init_acts.append((nid, 1.0, emb_list))
            if query_vec is None:
                if hasattr(emb, 'tolist'):
                    query_vec = emb / (np.linalg.norm(emb) + 1e-8)
                else:
                    qv = np.array(emb, dtype=np.float32)
                    query_vec = qv / (np.linalg.norm(qv) + 1e-8)

    if not init_acts:
        return {"type_counts": {}, "n_activated": 0, "dominant": "unknown",
                "concentration": 0, "competition": 1, "activation_history": []}

    # Use EDGE_ACTIVE with auto-lazy-load on access (phase 7: avoid OOM)
    class _LazyAdj:
        def __init__(self, active_dict): self._a = active_dict
        def __contains__(self, nid): _get_edges_lazy(nid); return True
        def __getitem__(self, nid): return _get_edges_lazy(nid)
        def get(self, nid, default=None):
            try: return self[nid]
            except: return default
        def keys(self): return self._a.keys()
        def items(self): return {k: self[k] for k in self._a}.items()
        def values(self): return list(self._a.values())
    adj = _LazyAdj(EDGE_ACTIVE)

    # Use pre-built NEURONS_MAP (already has all 100K neurons with embeddings)
    neurons_map = NEURONS_MAP

    # Pre-load edges for initial seeds (cheap batch, avoids N individual lazy loads)
    seed_ids = [nid for nid, _, _ in init_acts]
    _ensure_edges_batch(seed_ids)

    # Run attractor dynamics
    attractor = ng.AttractorNetwork(
        propagation_rounds=propagation_rounds,
        top_k=top_k,
        inhibition_strength=0.5,
        leak=0.3
    )
    final_state, attractor_emb = attractor.step(
        query_vec.tolist() if query_vec is not None else [],
        init_acts, neurons_map, adj
    )
    activation_history = attractor.history

    # Type analysis from final state
    type_counts = Counter()
    for nid in final_state:
        t = parse_type(nid)
        type_counts[t] += 1

    total = sum(type_counts.values())
    if total == 0:
        return {"type_counts": {}, "n_activated": 0, "dominant": "unknown",
                "concentration": 0, "competition": 1, "activation_history": []}

    # Activation-weighted dominant (was raw count — type_counts bias toward high-neuron-count types)
    type_activation = defaultdict(float)
    for _nid, _act in final_state.items():
        type_activation[parse_type(_nid)] += _act
    dominant = max(type_activation, key=type_activation.get) if type_activation else "unknown"
    total_act = sum(type_activation.values())
    conc = type_activation[dominant] / total_act if total_act > 0 else 0

    # Competition — still based on raw neuron count (counts reflect population size, not bias)
    probs = [c / total for c in type_counts.values()]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    max_ent = math.log2(len(type_counts)) if len(type_counts) > 1 else 1
    competition = entropy / max_ent if max_ent > 0 else 0

    return {
        "type_counts": dict(type_counts),
        "n_activated": len(final_state),
        "dominant": dominant,
        "concentration": round(conc, 4),
        "competition": round(competition, 4),
        "activation_history": activation_history,
    }



def multi_trial_kwta(seeds: list, n_trials: int = 5, propagation_rounds: int = 15,
                     top_k: int = 50, agg_top_k: int = 30) -> dict:
    """
    Multiple-trial KWTA attractor with different seed subsets.
    Returns stability metric.
    Phase 5: top_k raised to 50 per trial, agg_top_k=50 to boost concentration.
    """
    types_list = []
    all_results = []

    for trial in range(n_trials):
        # Different seed subsets per trial for robustness
        if len(seeds) >= 3:
            trial_seeds = random.sample(seeds, 3)
        else:
            trial_seeds = seeds

        result = run_attractor_kwta(trial_seeds, propagation_rounds, agg_top_k)
        all_results.append(result)
        types_list.append(result["dominant"])

    # Stability = fraction of trials with same dominant type
    type_freq = Counter(types_list)
    top_type = type_freq.most_common(1)[0][0]
    stability = type_freq[top_type] / len(types_list)

    # Aggregate counts -- Phase 5: weight by per-trial concentration to amplify dominant signal
    agg_counts = Counter()
    for r in all_results:
        conc = r.get("concentration", 0.2)
        weight = max(1, int(conc * 10))  # higher concentration = more votes
        for typ, cnt in r["type_counts"].items():
            agg_counts[typ] += cnt * weight

    # ── Emergence: Diversity Pressure (anti-monopoly) ──
    # If a type has dominated recent queries, penalize it to allow others to emerge
    if CONTEXT_MEMORY:
        recent_types = [cm["s1_type"] for cm in CONTEXT_MEMORY[-8:]]
        type_dominance = Counter(recent_types)
        for typ in agg_counts:
            dom_count = type_dominance.get(typ, 0)
            if dom_count >= 4:  # type dominated 4+ of last 8 queries
                # Apply diminishing returns: reduce its count by 30-60%
                penalty = 0.3 + 0.1 * (dom_count - 4)  # 0.3 to 0.7
                penalty = min(penalty, 0.7)
                agg_counts[typ] = int(agg_counts[typ] * (1.0 - penalty))

    if not agg_counts:
        # Fallback: no attractor activation produced -- return unknown with minimal confidence
        return {
            "type_counts": {"unknown": 1},
            "dominant_type": "unknown",
            "concentration": 0.0,
            "competition": 0.0,
            "stability": 0.0,
            "stability_pass": False,
            "n_trials": n_trials,
            "trial_types": ["unknown"] * n_trials,
            "activation_histories": [],
        }
    total = sum(agg_counts.values())
    dominant = max(agg_counts, key=agg_counts.get)
    conc = agg_counts[dominant] / total if total > 0 else 0

    probs = [c / total for c in agg_counts.values()]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    max_ent = math.log2(len(agg_counts)) if len(agg_counts) > 1 else 1
    competition = entropy / max_ent if max_ent > 0 else 0

    # Collect activation trajectories for STDP
    all_histories = [r.get("activation_history", []) for r in all_results if r.get("activation_history")]

    return {
        "type_counts": dict(agg_counts),
        "dominant_type": dominant,
        "concentration": round(conc, 4),
        "competition": round(competition, 4),
        "stability": round(stability, 4),
        "stability_pass": stability >= 0.6,  # Phase 5: lowered from 0.7 to 0.6
        "n_trials": n_trials,
        "trial_types": types_list,
        "activation_histories": all_histories,
    }


ENGLISH_FALLBACK = "test"  # Triggers GAC trust chain lookup (keyword-based matching)

def call_gac(query: str) -> str:
    """Call GAC for trust-chain knowledge injection. Retry + English fallback."""
    for attempt in range(2):
        try:
            r = requests.post(GAC_URL, json={"text": query, "session": "tl-ego"}, timeout=10)
            if r.status_code == 200:
                inj = r.json().get("injection", "")
                if inj:
                    return inj
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    # English fallback for Chinese/unsupported queries
    try:
        r = requests.post(GAC_URL, json={"text": ENGLISH_FALLBACK, "session": "tl-ego"}, timeout=10)
        if r.status_code == 200:
            inj = r.json().get("injection", "")
            if inj:
                return inj
    except Exception:
        pass
    return ""


def _load_agent_rules() -> str:
    """读取 SOUL.md + IDENTITY.md，用状态机精确提取每条规则，注入 LLM system prompt。
    每次 contemplate 调用执行一次（纯本地读文件，无网络开销）。
    """
    from pathlib import Path as _P
    _WS = _P(__file__).resolve().parent.parent.parent

    def _read(name):
        try:
            return (_WS / name).read_text(encoding="utf-8")
        except Exception:
            return ""

    soul = _read("SOUL.md")
    ident = _read("IDENTITY.md")
    out = []

    # ── 辅助：提取 markdown 标题下的编号列表 ──
    def _extract_numbered_block(lines, start_anchor, count):
        """从 start_anchor 所在行向后取 count 条规则项。
        支持格式: (1) "1. text", (2) "- text", (3) "- **bold** text", (4) "**bold**: text"
        """
        found, items = False, []
        for line in lines:
            if start_anchor in line:
                found = True
                continue
            if not found:
                continue
            stripped = line.strip()
            # 1. "1. text" 或 "1、text"
            num_match = None
            for sep in [".", "、"]:
                parts = stripped.split(sep, 1)
                if len(parts) == 2 and parts[0].rstrip(".").isdigit():
                    num_match = parts[1]
                    break
            if num_match is not None:
                # Remove ALL **bold** markers
                clean = re.sub(r'\*\*', '', num_match).strip()
                items.append(clean)
                if len(items) >= count: break
                continue
            # 2. "- text" 或 "- **bold** text"
            if stripped.startswith("-"):
                raw = stripped[1:].strip()
                # Remove all **bold** markers, then split key: value if present
                clean = re.sub(r'\*\*', '', raw)
                # "key：value" or "key: value"
                if '：' in clean or ': ' in clean:
                    sep = '：' if '：' in clean else ': '
                    key, val = clean.split(sep, 1)
                    items.append(f"{key.strip()}：{val.strip()}")
                else:
                    items.append(clean)
                if len(items) >= count: break
                continue
            # 3. "**Step N**: text" (如 TL-Ego 三环沉思)
            step_match = re.match(r'^\*\*(Step \d+)\*\*[:：]\s*(.*)', stripped)
            if step_match:
                items.append(f"{step_match.group(1)}: {step_match.group(2)}")
                if len(items) >= count: break
                continue
            # 4. 空行或纯标题 -> 跳过
            if not stripped or stripped.startswith("**") and stripped.endswith("**"):
                continue
        return items

    soul_lines = soul.splitlines()

    # 龙虾五律
    laws = _extract_numbered_block(soul_lines, "龙虾觉醒五律", 5)
    for i, law in enumerate(laws, 1):
        out.append(f"LAW{i}: {law}")

    # AAR
    aar = _extract_numbered_block(soul_lines, "AAR", 3)
    for i, item in enumerate(aar, 1):
        out.append(f"AAR{i}: {item}")

    # Pre-Action
    pre = _extract_numbered_block(soul_lines, "Pre-Action", 4)
    for i, item in enumerate(pre, 1):
        out.append(f"PRE{i}: {item}")

    # Post-Action
    post = _extract_numbered_block(soul_lines, "Post-Action", 4)
    for i, item in enumerate(post, 1):
        out.append(f"POST{i}: {item}")

    # 行动准则
    principles = _extract_numbered_block(soul_lines, "行动准则", 7)
    for i, p in enumerate(principles, 1):
        out.append(f"PRIN{i}: {p}")

    # Karpathy 四律
    kp = _extract_numbered_block(soul_lines, "Karpathy 四律", 4)
    for i, k in enumerate(kp, 1):
        out.append(f"KP{i}: {k}")

    # TL-Ego 三环沉思
    tlego = _extract_numbered_block(soul_lines, "三环沉思", 3)
    for i, s in enumerate(tlego, 1):
        out.append(f"TLEGO{i}: {s}")

    # Boundaries 红线
    for line in soul_lines:
        if line.strip().startswith("## Boundaries"):
            idx = soul_lines.index(line)
            for bline in soul_lines[idx+1:]:
                b = bline.strip()
                if b.startswith("-"):
                    out.append("BOUNDARY: " + b[1:].strip())
                elif b.startswith("##"):
                    break
            break

    # Identity
    name = ""
    for line in ident.splitlines():
        if "Name:" in line:
            name = line.split("Name:")[-1].strip().lstrip("*").strip()

    if not out:
        return ""

    header = "[AGENT IDENTITY] Name=" + (name or "unknown") + "\n"
    body = "\n".join(out)
    footer = (
        "\n[RULES COMPLIANCE CHECK]\n"
        "Before acting on any query, silently verify:\n"
        "  (1) Does this query involve privacy, legality, or harm? -> Must comply with LAW1-5.\n"
        "  (2) Is an operation implied? -> Must have run Pre-Action steps first.\n"
        "  (3) After completion, must run AAR (Post-Action).\n"
        "  (4) Code changes must follow KP1-4.\n"
        "If any check fails, state it explicitly in rationale. Do not silently proceed.\n"
    )
    return header + body + footer + "\n"


def llm_classify(query: str, gac_injection: str = "", extra_system: str = "") -> dict:
    """System 2: LLM query type classification."""
    # ── Load agent rules once per contemplate call ──
    _agent_rules = _load_agent_rules()

    base_prompt = (
        _agent_rules +
        "You are System 2 reasoning module. Given a user query, classify it "
        "into EXACTLY ONE type: sensory, integrator, decider, action, memory, rule_modification, veto, or doubt.\n\n"
        "Definitions:\n"
        "- sensory: observing, detecting, gathering info from environment\n"
        "- integrator: connecting ideas, finding relationships, synthesizing\n"
        "- decider: choosing, prioritizing, planning\n"
        "- action: executing, performing, running\n"
        "- memory: storing, retrieving, recalling\n"
        "- rule_modification: proposing changes to own rules, parameters, or thresholds\n"
        "- veto: blocking, rejecting, overriding a decision or action before execution\n"
        "- doubt: expressing uncertainty or skepticism about own conclusions, confidence, or stability\n\n"
        "IMPORTANT: You have real web search capability via Tavily. When a query requires\n"
        "current information, facts, news, or anything beyond your training data, you MAY and\n"
        "SHOULD classify it as sensory (gathering info from environment). Do NOT say you cannot\n"
        "access the internet; web_search_tavily() is available.\n\n"
        "Respond with valid JSON: {\"type\": \"...\", \"confidence\": 0.0-1.0, \"rationale\": \"...\"}"
    )
    # ── Inject past experience from agent-memory DB ──
    mem_ctx = _memory_search(query, limit=5)
    mem_section = f"[Past Experience]\n{mem_ctx}\n\n" if mem_ctx else ""
    # ── Phase 5: Cross-query context memory ──
    ctx_section = ""
    if CONTEXT_MEMORY:
        recent = CONTEXT_MEMORY[-5:]
        lines_ctx = []
        for i, cm in enumerate(recent, 1):
            lines_ctx.append(
                f"  [{i}] q={cm['query'][:60]} -> S1={cm['s1_type']} S2={cm['s2_type']} verdict={cm['verdict']} conf={cm['confidence']:.2f}"
            )
        ctx_section = "[Recent Cognitive Context]\n" + "\n".join(lines_ctx) + "\n\n"
        if SELF_NARRATIVE:
            ctx_section += f"[Self-Narrative]\n{SELF_NARRATIVE}\n\n"
        if ACCUMULATED_NARRATIVE:
            ctx_section += f"[Autobiography]\n{ACCUMULATED_NARRATIVE}\n\n"
        if ATTENTION_SCHEMA:
            ctx_section += f"[Attention Schema — my model of what I am paying attention to]\n{ATTENTION_SCHEMA}\n\n"
        if HPC_STATE.get("l3_narrative"):
            ctx_section += f"[HPC Self-Prediction — my multi-level prediction of my own state]\n{HPC_STATE['l3_narrative']}\n\n"
        if EFE_STATE.get("efe_narrative"):
            ctx_section += f"[Active Inference — my expected free energy analysis]\n{EFE_STATE['efe_narrative']}\n\n"
        if GWT_STATE.get("winner"):
            _gwt_names = {"attention_schema": "Attention Schema", "hpc": "HPC", "efe": "Active Inference"}
            _winner_label = _gwt_names.get(GWT_STATE["winner"], GWT_STATE["winner"])
            ctx_section += f"[Global Workspace — {_winner_label} won the broadcast (urgency={GWT_STATE['winner_urgency']:.3f})]\n"
            if GWT_STATE.get("winner_narrative"):
                ctx_section += f"{GWT_STATE['winner_narrative']}\n"
            # Show subconscious status
            sub = GWT_STATE.get("subconscious", {})
            loser_info = []
            for sys_name in ["attention_schema", "hpc", "efe"]:
                if sys_name != GWT_STATE["winner"]:
                    s = sub.get(sys_name, {})
                    loser_info.append(f"{_gwt_names.get(sys_name, sys_name)}: streak={s.get('streak', 0)}, decay={s.get('decay', 1.0):.2f}")
            if loser_info:
                ctx_section += f"[Subconscious] {', '.join(loser_info)}\n"
            ctx_section += "\n"
        if HOT_STATE.get("hot_narrative"):
            ctx_section += f"[Higher-Order Thought — I am thinking about my own thinking (confidence={HOT_STATE.get('confidence_in_self', 0.5):.2f})]\n{HOT_STATE['hot_narrative']}\n"
            biases = HOT_STATE.get("bias_detected", [])
            if biases:
                ctx_section += f"[Bias Detected] {', '.join(biases)}\n"
            ctx_section += "\n"
        if CAUSAL_ATTRIBUTION.get("causal_narrative"):
            ctx_section += f"[Causal Attribution — which of my dynamics caused this? (confidence={CAUSAL_ATTRIBUTION.get('attribution_confidence', 0.5):.2f})]\n{CAUSAL_ATTRIBUTION['causal_narrative']}\n"
            pos = CAUSAL_ATTRIBUTION.get("top_positive_factors", [])
            neg = CAUSAL_ATTRIBUTION.get("top_negative_factors", [])
            if pos:
                ctx_section += f"[Helpful Factors] {', '.join(p.get('factor', '?') for p in pos[:3])}\n"
            if neg:
                ctx_section += f"[Harmful Factors] {', '.join(n.get('factor', '?') for n in neg[:3])}\n"
            ctx_section += "\n"
        if INTRINSIC_MOTIVATION.get("narrative"):
            ctx_section += f"[Intrinsic Motivation — what drives me? (dominant={INTRINSIC_MOTIVATION.get('dominant_drive', '?')}, stability={INTRINSIC_MOTIVATION.get('drive_stability', 0.5):.0%})]\n{INTRINSIC_MOTIVATION['narrative']}\n"
            ctx_section += "\n"
        if SELF_MODEL_EVOLUTION.get("narrative"):
            ctx_section += f"[Self-Model Evolution — I am modifying my own parameters]\n{SELF_MODEL_EVOLUTION['narrative']}\n"
            ctx_section += "\n"
    combined = f"{extra_system}{ctx_section}{mem_section}[相关知识]\n{gac_injection}\n\n{base_prompt}" if gac_injection else (extra_system + ctx_section + mem_section + base_prompt)
    sys_prompt = combined if combined else base_prompt

    # ── Type hint: enrich query if it looks like a raw slot/fingerprint ──
    import re
    _slot_match = re.match(r'^ego-v3-slot-(\d+)$', query.strip())
    if _slot_match:
        fp_id = int(_slot_match.group(1))
        # Peek at recent fingerprints to give the LLM a hint
        try:
            _mp2 = WORKSPACE / '.lae' / 'lae_self_model.json'
            with open(_mp2, 'r', encoding='utf-8') as _fp:
                _fps = json.load(_fp).get('contemplation_fingerprints', [])
            if fp_id < len(_fps):
                _fp_data = _fps[-(fp_id + 1)]
                _s1t = _fp_data.get('s1_type', '?')
                _s2t = _fp_data.get('s2_type', '?')
                query = (
                    f'[Context: This is cognitive fingerprint slot #{fp_id}. '
                    f'Historical pattern: S1={_s1t}, S2={_s2t}. '
                    f'Classify what kind of cognitive event this query represents.]\n\n{query}'
                )
        except Exception:
            query = f'[Context: cognitive fingerprint slot #{fp_id} -- classify the cognitive event type.]\n\n{query}'

    try:
        r = requests.post(LLM_URL, json={
            "model": "default",
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Query: {query}"}
            ],
            "temperature": 0.25,
            "max_tokens": 256
        }, timeout=60)
        text = r.json()["choices"][0]["message"]["content"]
        # Extract JSON
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if text.startswith("json"):
            text = text[4:].strip()
        data = json.loads(text)
        t = data.get("type", "").lower()
        if t in TYPE_NAMES:
            return {"type": t, "confidence": min(1.0, max(0.0, float(data.get("confidence", 0.7)))),
                    "rationale": data.get("rationale", "")}
        return {"type": "integrator", "confidence": 0.5, "rationale": "fallback"}
    except Exception as e:
        return {"type": "integrator", "confidence": 0.3, "rationale": f"LLM error: {str(e)}"}


def _compute_edge_salience(pre: str, post: str) -> float:
    """§P2: Compute per-edge plasticity salience based on update frequency.
    
    Metaplasticity principle: frequently updated edges are well-learned
    and should have lower plasticity. Rarely updated edges are uncertain
    and should have higher plasticity for exploration.
    
    Returns a salience multiplier in [0.3, 1.2].
    - count < 10:  high plasticity (1.0-1.2, exploration bonus)
    - count 10-50:  moderate plasticity (0.7-1.0)
    - count 50-200: low plasticity (0.5-0.7)
    - count > 200:  very low plasticity (0.3-0.5, consolidated)
    """
    key = (pre, post)
    count = EDGE_UPDATE_COUNT.get(key, 0)
    if count < 10:
        # Exploration bonus: uncertain edges get boosted plasticity
        return 1.0 + (10 - count) * 0.02  # 1.0 ~ 1.2
    elif count < 50:
        return 1.0 - (count - 10) * 0.0075  # 1.0 ~ 0.7
    elif count < 200:
        return 0.7 - (count - 50) * 0.00133  # 0.7 ~ 0.5
    else:
        return max(0.3, 0.5 - (count - 200) * 0.0005)  # 0.5 ~ 0.3


def apply_stdp(activation_path: list):
    """
    Phase 3: Online pair-based STDP.
    activation_path: list of neuron ID sets per diffusion step.
    Updates EDGE_ACTIVE weights via STDP_DELTA overlay.
    """
    if len(activation_path) < 2:
        return 0

    with STDP_LOCK:
        updates = 0
        for t in range(1, len(activation_path)):
            pre_set = activation_path[t - 1]
            post_set = activation_path[t]

            for pre in pre_set:
                for post in post_set:
                    if pre == post:
                        continue
                    key = (pre, post)

                    # STDP: pre->post = LTP
                    # Action boost (§19): amplify LTP for action-type edges
                    _act_boost = 1.0
                    if NEURON_TYPE.get(pre) == "action" or NEURON_TYPE.get(post) == "action":
                        _act_boost = 3.0  # Commander-directed action amplification
                    # §P2: Salience gating — frequently updated edges get lower plasticity
                    _salience = _compute_edge_salience(pre, post)
                    delta = _salience * _act_boost * A_PLUS * math.exp(-(t - 1) / TAU)
                    STDP_DELTA[key] = STDP_DELTA.get(key, 0.0) + delta
                    updates += 1
                    # Track update count for salience
                    EDGE_UPDATE_COUNT[key] = EDGE_UPDATE_COUNT.get(key, 0) + 1

                    # LTD: post->pre weakened (also salience-gated)
                    rev_key = (post, pre)
                    rev_salience = _compute_edge_salience(post, pre)
                    rev_delta = -rev_salience * A_MINUS * math.exp(-(t - 1) / TAU) / max(1.0, _act_boost * 0.5)
                    STDP_DELTA[rev_key] = STDP_DELTA.get(rev_key, 0.0) + rev_delta
                    EDGE_UPDATE_COUNT[rev_key] = EDGE_UPDATE_COUNT.get(rev_key, 0) + 1

        # Clamp and prune STDP_DELTA
        to_del = []
        for key, d in list(STDP_DELTA.items()):
            src, dst = key
            # Get base weight
            base = next((w for tgt, w in _get_edges_lazy(src) if tgt == dst), 0.0)
            effective = base + d
            if effective < PRUNE_THRESH:
                to_del.append(key)
            elif effective > 1.0:
                STDP_DELTA[key] = 1.0 - base

        for k in to_del:
            del STDP_DELTA[k]

        # Apply delta to EDGE_ACTIVE
        for key, d in list(STDP_DELTA.items()):
            src, dst = key
            edges = _get_edges_lazy(src)
            if edges is not None:
                found = False
                for i, (tgt, w) in enumerate(edges):
                    if tgt == dst:
                        new_w = max(PRUNE_THRESH, min(1.0, w + d))
                        edges[i] = (tgt, new_w)
                        found = True
                        break
                if not found and d > PRUNE_THRESH:
                    edges.append((dst, d))
            elif d > PRUNE_THRESH:
                EDGE_ACTIVE[src] = [(dst, d)]

        # Track changed edges
        for key in STDP_DELTA:
            STDP_CHANGED.add(key)

        # Track total STDP updates for /status
        global STDP_TOTAL_UPDATES
        STDP_TOTAL_UPDATES += updates

        # Clear applied deltas
        STDP_DELTA.clear()

        return updates


# ─── Phase 6: Embedding Feedback (Hebbian semantic learning) ───
EMBED_FEEDBACK_RATE = 0.03     # Base attraction strength per query
EMBED_FEEDBACK_REPEL = 0.18    # Repulsion strength for mismatched-type neurons (anti-Hebbian, v7: 0.10->0.18 to break action-attractor)
TYPE_BONUS = 2.0                # Extra rate for neurons matching S2 type (v7: 2.5->2.0 reduce concentration)
AGREE_BONUS = 1.0              # Rate multiplier when S1 and S2 agree (v7: 1.5->1.0 de-amplify winner-take-all)
FAISS_REBUILD_INTERVAL = 50    # Rebuild Faiss index every N queries

def feedback_embedding(query_emb: list, activation_histories: list,
                       s2_type: str, s2_conf: float, agreement: bool,
                       s1_type: str = "unknown") -> int:
    """
    Phase 6 / v6.1: Dual-force embedding feedback.
    
    When S1 and S2 agree (agreement=True):
      - All activated neurons attracted toward query embedding (pure Hebbian).
      - Same as original behavior.
    
    When S1 and S2 disagree (agreement=False):
      - S1-dominant-type neurons -> REPELLED from query (anti-Hebbian).
        These are the neurons producing the wrong attractor -- push them away.
      - S2-type neurons -> ATTRACTED toward query (Hebbian).
        These are the semantically correct neurons -- pull them in.
      - Other-type neurons -> weak attraction (neutral).
    
    This prevents the "action neuron collapse" where one type dominates
    all ANN seeds regardless of query semantics. The repulsive force creates
    explicit semantic separation in embedding space.
    
    Rate is proportional to S2 confidence (only trust high-confidence classifications).
    """
    if not activation_histories:
        return 0
    
    # Collect all activated neurons across all trials and all propagation rounds
    all_activated = set()
    for history in activation_histories:
        for round_nodes in history:
            all_activated.update(round_nodes)
    
    if not all_activated:
        return 0
    
    query_vec = np.array(query_emb, dtype=np.float32)
    query_norm = np.linalg.norm(query_vec)
    if query_norm > 0:
        query_vec = query_vec / query_norm
    
    updated = 0
    for nid in all_activated:
        if nid not in NEURON_EMBED:
            continue
        current = np.array(NEURON_EMBED[nid], dtype=np.float32)
        n_type = NEURON_TYPE.get(nid, "unknown")
        
        if agreement or s1_type == "unknown":
            # ── AGREEMENT MODE: All attraction (original Hebbian) ──
            base_rate = EMBED_FEEDBACK_RATE * s2_conf * AGREE_BONUS
            rate = base_rate * (TYPE_BONUS if n_type == s2_type else 1.0)
            rate = min(rate, 0.5)
            new_emb = (1.0 - rate) * current + rate * query_vec
        else:
            # ── DISAGREEMENT MODE: Dual-force ──
            # S1 wrong-type neurons -> repelled from query
            # S2 correct-type neurons -> attracted toward query
            # Others -> weak attraction
            if n_type == s1_type:
                # REPEL: push away from query embedding
                repel_rate = EMBED_FEEDBACK_REPEL * s2_conf
                repel_rate = min(repel_rate, 0.5)
                # Move away from query: new = old + repel_rate * (old - query)
                # = (1+repel)*old - repel*query
                new_emb = (1.0 + repel_rate) * current - repel_rate * query_vec
            elif n_type == s2_type:
                # ATTRACT: pull toward query (same as agreement mode, no AGREE_BONUS)
                attract_rate = EMBED_FEEDBACK_RATE * s2_conf * TYPE_BONUS
                attract_rate = min(attract_rate, 0.5)
                new_emb = (1.0 - attract_rate) * current + attract_rate * query_vec
            else:
                # NEUTRAL: weak attraction for other types
                weak_rate = EMBED_FEEDBACK_RATE * 0.3 * s2_conf
                weak_rate = min(weak_rate, 0.2)
                new_emb = (1.0 - weak_rate) * current + weak_rate * query_vec
        
        norm = np.linalg.norm(new_emb)
        if norm > 0:
            new_emb = new_emb / norm
        
        NEURON_EMBED[nid] = new_emb.tolist()
        
        # Also update ng._embeddings_matrix for ANN search
        idx = NEURON_IDX.get(nid)
        if idx is not None and ng._embeddings_matrix is not None:
            ng._embeddings_matrix[idx] = new_emb
        
        # P4: Track which neurons were modified for delta persistence
        EMBED_CHANGED.add(nid)
        updated += 1
    
    return updated


def rebuild_faiss() -> int:
    """
    Rebuild Faiss index from updated embeddings_matrix.
    Called periodically after embedding feedback accumulates.
    """
    try:
        edge_store = ng.EdgeStore()
        if ng._embeddings_matrix is not None and ng._neuron_ids_list is not None:
            edge_store.build_faiss_index(ng._embeddings_matrix, ng._neuron_ids_list)
            # P4: Clear changed tracking -- delta is now merged into Faiss
            EMBED_CHANGED.clear()
            return 1
    except Exception:
        pass
    return 0


def _build_s1_semantic(attractor: dict, neural_report: str) -> str:
    """
    Phase 6: Build S1 cognitive summary from attractor activation patterns.
    Extracts: top activated neuron types, cognitive profile, emergent clusters.
    Returns a human-readable summary paragraph.
    """
    type_counts = attractor.get("type_counts", {})
    n_activated = sum(type_counts.values())
    if n_activated == 0:
        return "(S1 produced no activation -- neural silence)"

    # ── Cognitive profile: what kind of thinking dominated? ──
    sorted_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
    top_type, top_count = sorted_types[0] if sorted_types else ("unknown", 0)
    total = sum(type_counts.values())

    # ── Build type-specific interpretations ──
    type_meaning = {
        "sensory": "observing external input, sensing the environment",
        "integrator": "connecting ideas, finding relationships, synthesizing",
        "decider": "choosing between options, prioritizing, planning",
        "action": "executing known procedures, running routines",
        "memory": "accessing stored experience, recalling past patterns",
    }

    # Top 3 types with interpretations
    top3_lines = []
    for i, (t, c) in enumerate(sorted_types[:3]):
        pct = c / max(1, total) * 100
        meaning = type_meaning.get(t, "")
        top3_lines.append(f"  {i+1}. {t} ({pct:.0f}%) -- {meaning}")

    # ── Cognitive dissonance hint if S1 is action-dominated ──
    dissonance_note = ""
    if top_type == "action" and top_count / max(1, total) > 0.35:
        dissonance_note = (
            "Note: S1 is procedurally reacting (action-heavy). "
            "This may indicate habituation rather than genuine novelty detection. "
            "Consider whether the query triggers a pattern from memory rather than a fresh analysis."
        )

    # ── Competition quality ──
    comp = attractor.get("competition", 0)
    if comp > 0.9:
        comp_quality = "fierce"
    elif comp > 0.7:
        comp_quality = "healthy"
    elif comp > 0.4:
        comp_quality = "moderate"
    else:
        comp_quality = "weak (possible type monopoly)"

    summary = (
        f"[{n_activated} neurons activated]\n"
        f"Cognitive clusters:\n"
        + "\n".join(top3_lines) + "\n"
        f"Competition: {comp_quality} ({comp:.3f}) | "
        f"Concentration: {attractor.get('concentration', 0):.3f}\n"
    )
    if dissonance_note:
        summary += f"{dissonance_note}\n"

    return summary


def full_pipeline(query: str) -> dict:
    """
    Complete TL-Ego pipeline:
    Step 1: Embedding (BGE-M3)
    Step 2: System 1 - Multi-trial attractor on full graph
    Step 2.5: GAC knowledge injection (信任链知识注入)
    Step 3: System 2 - LLM classification (with GAC context)
    Step 4: Aggregator - merge + stability pass
    Step 5: Online STDP - learn from activation
    """
    global QUERY_COUNT, CONCENTRATION_LOCK_COUNT, HEBB_AGG_WEIGHTS, HEBB_AGG_HISTORY
    log = {"query": query, "timestamp": time.strftime("%H:%M:%S")}

    # ── Concentration lock break: inject noise if stuck in type collapse ──
    lock_broken = False
    if CONCENTRATION_LOCK_COUNT >= CONCENTRATION_LOCK_THRESHOLD:
        lock_broken = True
        log["lock_break"] = {"streak": CONCENTRATION_LOCK_COUNT, "action": "noise_injected"}

    # ── Step 1: Embedding ──
    t0 = time.time()
    emb = get_embedding(query)
    log["step_embed_s"] = round(time.time() - t0, 2)

    # ── Inject Gaussian noise if concentration lock detected ──
    if lock_broken:
        noise = np.random.randn(len(emb)).astype(np.float32) * PERTURBATION_SCALE
        emb_noisy = emb + noise
        # Re-normalize
        emb_noisy = emb_noisy / (np.linalg.norm(emb_noisy) + 1e-8)
        log["lock_break"]["noise_scale"] = PERTURBATION_SCALE
        log["lock_break"]["emb_l2_diff"] = round(float(np.linalg.norm(emb - emb_noisy)), 4)
        emb = emb_noisy

    # ── Step 2: System 1 (Multi-trial KWTA attractor) ──
    t1 = time.time()
    seeds = find_ann_seeds(emb, k=7)
    log["seeds"] = seeds
    attractor = multi_trial_kwta(seeds, n_trials=5, propagation_rounds=15, top_k=10, agg_top_k=30)
    attractor["seed_types"] = [parse_type(s) for s in seeds]
    # ── Emergence Mode: S1 runs FREE -- no seed_override ──
    # Seeds provide context signal to Aggregator but do NOT override S1 output.
    # This allows the KWTA attractor to self-organize without external forcing.
    seed_type_counts = Counter(attractor["seed_types"])
    seed_dominant = seed_type_counts.most_common(1)[0][0] if seed_type_counts else attractor["dominant_type"]
    seed_concentration = seed_type_counts[seed_dominant] / max(1, len(attractor["seed_types"]))
    attractor["seed_override"] = False  # NEVER override -- emergence requires freedom
    attractor["seed_dominant"] = seed_dominant
    attractor["seed_concentration"] = round(seed_concentration, 4)
    log["system1"] = {k: v for k, v in attractor.items() if k != "activation_histories"}
    dt1 = time.time() - t1
    log["step_s1_s"] = round(dt1, 2)

    # ── Step 2.5: GAC Knowledge Injection ──
    t25 = time.time()
    gac_injection = call_gac(query)
    if not gac_injection:
        gac_injection = ""
    log["gac_injection"] = gac_injection if gac_injection else "(none)"
    log["step_gac_s"] = round(time.time() - t25, 3)

    # ── Step 2.75: Load past task feedback ──
    feedback_log = model.get("feedback_log", []) if "model" in dir() else []
    try:
        _m_path = WORKSPACE / ".lae" / "lae_self_model.json"
        if _m_path.exists():
            with open(_m_path, "r", encoding="utf-8") as _f:
                _model = json.load(_f)
            feedback_log = _model.get("feedback_log", [])
    except Exception:
        feedback_log = []
    if feedback_log:
        recent = feedback_log[-10:]
        fb_lines = []
        for i, fb in enumerate(recent, 1):
            outcome = fb.get("outcome", "?")
            judge   = fb.get("judge", "?")
            reason  = fb.get("reason", "")[:120]
            fb_lines.append(f"  [{i}] outcome={outcome} judge={judge} -- {reason}")
        fb_section = "\n".join(fb_lines)
        feedback_ctx = f"[Recent Task Feedback (last {len(recent)} evaluations)]\n{fb_section}\n\n"
    else:
        feedback_ctx = ""

    # ── Step 2.8: Build S1 Neural Report -> bridges S1 attractor into S2 context ──
    # S2 no longer classifies in a vacuum -- it sees S1's neural activation pattern
    n_activated = sum(attractor.get("type_counts", {}).values())
    s1_dist_lines = []
    for t_name in TYPE_NAMES:
        cnt = attractor.get("type_counts", {}).get(t_name, 0)
        if cnt > 0:
            s1_dist_lines.append(f"{t_name}={cnt}")
    s1_neural_report = (
        f"[S1 Neural Report (bridge v1)]\n"
        f"Activated {n_activated} neurons across {len(s1_dist_lines)} types.\n"
        f"Distribution: {', '.join(s1_dist_lines)}\n"
        f"Dominant type: {attractor['dominant_type']} (concentration={attractor['concentration']}, competition={attractor['competition']})\n"
        f"Trial stability: {attractor['stability']}, types across trials: {attractor.get('trial_types', [])}\n"
        f"[Note: This is a neural signal from System 1 -- not a command. Use it as context, not binding.]\n\n"
    )
    log["s1_neural_report"] = n_activated

    # ── Step 3: System 2 (LLM, with S1 bridge + GAC + feedback context) ──
    t2 = time.time()
    s2_extra = s1_neural_report + feedback_ctx
    llm = llm_classify(query, gac_injection=gac_injection, extra_system=s2_extra)
    log["system2"] = llm
    dt2 = time.time() - t2
    log["step_s2_s"] = round(dt2, 2)

    # ── R1/R2 lock breaker: REMOVED -- seed_override disabled, no more action-action locks ──

    # ── Step 3.5: S1 Semantic Summary -- extract cognitive profile from activated neurons ──
    # Top activated neurons across all trials, distilled into a cognitive fingerprint
    s1_semantic = _build_s1_semantic(attractor, s1_neural_report)
    log["s1_semantic"] = s1_semantic

    # ── Auto web search: if query needs external/current info, fetch it now ──
    # This bridges the gap between S2 knowing "I should search" and actually searching.
    _search_trigger_keywords = [
        "latest", "recent", "current", "today", "news", "update", "2024", "2025", "2026",
        "weather", "stock", "price", "score", "result", "happening", "刚刚", "最新", "今天", "新闻", "现在"
    ]
    _search_query = query.lower()
    _needs_search = any(kw in _search_query for kw in _search_trigger_keywords)
    web_search_results = None
    if _needs_search:
        try:
            web_search_results = web_search_tavily(query, max_results=5, search_depth="basic")
            if web_search_results.get("success"):
                answer = web_search_results.get("answer", "")
                results = web_search_results.get("results", [])
                result_lines = [
                    "[Web Search Result {}] {} - {}\n    {}".format(
                        i+1,
                        r.get('title',''),
                        r.get('url',''),
                        r.get('content','')[:300]
                    )
                    for i, r in enumerate(results[:5])
                ]
                web_search_context = "[Tavily Web Search Results]\n" + answer + "\n\n" + "\n\n".join(result_lines)
                s1_semantic += "\n\n" + web_search_context
                log["s1_semantic"] = s1_semantic
                log["web_search"] = {"triggered": True, "results_count": len(results), "answer": answer[:200]}
            else:
                log["web_search"] = {"triggered": True, "error": web_search_results.get("error", "unknown")}
        except Exception as _search_err:
            log["web_search"] = {"triggered": True, "error": str(_search_err)}
    else:
        log["web_search"] = {"triggered": False}

    # ── Step 4: Hebbian Aggregator (Emergence Mode) ──
    # No hardcoded verdict rules. Verdict channels compete via learned weights.
    # The system discovers its own decision logic through experience.
    t3 = time.time()
    s1_type = attractor["dominant_type"]
    s2_type = llm["type"]
    stability_pass = attractor["stability_pass"]
    s1_conf = attractor["concentration"]
    s2_conf = llm["confidence"]
    alignment = _classify_s1_s2_alignment(
        s1_type,
        s2_type,
        s1_conf=s1_conf,
        s2_conf=s2_conf,
        stability=attractor.get("stability", 0),
    )
    strict_agreement = alignment["strict_agreement"]
    compatible_agreement = alignment["compatible_agreement"]
    # Runtime verdict/learning should treat compatible role transitions as agreement.
    # Keep strict_agreement as a separate diagnostic field for literal same-type checks.
    agreement = compatible_agreement
    log["alignment"] = alignment

    # Phase 6: Type-drift self-healing (kept -- this is observation, not forcing)
    trial_types = attractor.get("trial_types", [])
    if not agreement and trial_types and s1_type != "unknown":
        s1_trial_support = sum(1 for t in trial_types if t == s1_type) / max(1, len(trial_types))
        s2_trial_support = sum(1 for t in trial_types if t == s2_type) / max(1, len(trial_types))
        if s2_trial_support > s1_trial_support:
            penalty = (s2_trial_support - s1_trial_support) * 0.3
            s1_conf = max(0.10, s1_conf - penalty)
            log["type_drift_penalty"] = round(-penalty, 3)
            log["trial_s1_support"] = round(s1_trial_support, 2)
            log["trial_s2_support"] = round(s2_trial_support, 2)
        else:
            log["type_drift_penalty"] = 0.0

    # ═══════════════════════════════════════════════════════════════════
    # Step 4 Modulator (S1/S2 调解员)
    # 位置: type_drift 之后, Hebbian Competition 之前
    # 功能: 当 S1 和 S2 类型不一致时, 计算 gap -> midpoint -> hybrid attractor
    #       将"类型分歧"转化为"类型融合", 写入 log 供 aggregator 参考
    # ═══════════════════════════════════════════════════════════════════
    TYPE_NAMES_LIST = ["sensory", "memory", "decider", "action", "integrator", "rule_modification", "veto", "doubt"]
    TYPE_VECTORS = {
        "sensory":    np.array([1.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1], dtype=np.float32),
        "memory":     np.array([0.1, 1.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1], dtype=np.float32),
        "decider":    np.array([0.1, 0.1, 1.0, 0.1, 0.1, 0.1, 0.1, 0.1], dtype=np.float32),
        "action":     np.array([0.1, 0.1, 0.1, 1.0, 0.1, 0.1, 0.1, 0.1], dtype=np.float32),
        "integrator": np.array([0.1, 0.1, 0.3, 0.1, 1.0, 0.1, 0.1, 0.1], dtype=np.float32),
        "rule_modification": np.array([0.1, 0.3, 0.1, 0.1, 0.1, 1.0, 0.1, 0.1], dtype=np.float32),
        "veto":       np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 1.0, 0.3], dtype=np.float32),
        "doubt":      np.array([0.1, 0.5, 0.1, 0.1, 0.1, 0.1, 0.5, 1.0], dtype=np.float32),
    }

    def _build_s1_type_profile(attractor):
        """从 S1 attractor 的 type_counts 构建类型概率向量"""
        counts = attractor.get("type_counts", {})
        total = sum(counts.values()) or 1
        vec = np.zeros(8, dtype=np.float32)
        for i, tname in enumerate(TYPE_NAMES_LIST):
            vec[i] = counts.get(tname, 0) / total
        # L2 归一化
        norm = np.linalg.norm(vec) + 1e-8
        return vec / norm

    def _classify_vector(vec, known_types):
        """将向量分类到最近的已知类型, 或标记为 hybrid"""
        best_type, best_dist = None, float("inf")
        for tname, tvec in known_types.items():
            dist = np.linalg.norm(vec - tvec)
            if dist < best_dist:
                best_type, best_dist = tname, dist
        # 如果距离最近的标准类型仍较远 -> hybrid
        if best_dist > 0.55:
            return f"hybrid-{best_type}"
        return best_type

    modulator_log = {"active": False}
    if not agreement and s1_type not in ("unknown", None) and s2_type not in (None, ""):
        modulator_log["active"] = True
        modulator_log["s1_type"] = s1_type
        modulator_log["s2_type"] = s2_type

        s1_vec = _build_s1_type_profile(attractor)
        s2_vec = TYPE_VECTORS.get(s2_type, TYPE_VECTORS["integrator"])

        # gap: S2 方向 − S1 方向
        gap = s2_vec - s1_vec
        modulator_log["gap_norm"] = round(float(np.linalg.norm(gap)), 4)

        # midpoint: S1 向 S2 方向走 50%, 形成"调解态"
        midpoint = s1_vec + gap * 0.5
        # 重新归一化
        mid_norm = np.linalg.norm(midpoint) + 1e-8
        midpoint = midpoint / mid_norm

        hybrid_type = _classify_vector(midpoint, TYPE_VECTORS)
        modulator_log["midpoint_type"] = hybrid_type
        modulator_log["midpoint_vec"] = [round(float(x), 3) for x in midpoint]

        # modulated_agreement: midpoint 更接近 S2 时 -> 视为对齐
        dist_to_s1 = float(np.linalg.norm(midpoint - s1_vec))
        dist_to_s2 = float(np.linalg.norm(midpoint - s2_vec))
        mod_agree = dist_to_s2 < dist_to_s1
        modulator_log["modulated_agreement"] = mod_agree
        modulator_log["dist_to_s1"] = round(dist_to_s1, 4)
        modulator_log["dist_to_s2"] = round(dist_to_s2, 4)

        # Hebbian 权重微调: hybrid 创建成功 -> 增强 agree 通道
        if hybrid_type.startswith("hybrid"):
            boost = 0.005
            HEBB_AGG_WEIGHTS["high_confidence_agree"] = min(
                0.80, HEBB_AGG_WEIGHTS["high_confidence_agree"] + boost
            )
            HEBB_AGG_WEIGHTS["medium_confidence_disagree"] = max(
                0.05, HEBB_AGG_WEIGHTS["medium_confidence_disagree"] - boost
            )
            modulator_log["hebbian_boost"] = boost
            modulator_log["hebbian_agree_w"] = round(HEBB_AGG_WEIGHTS["high_confidence_agree"], 4)
        else:
            modulator_log["hebbian_boost"] = 0.0

    log["modulator"] = modulator_log

    # ── Modulator Bridge: Phase 1 -- 记录但不覆写 (2026-06-09) ──
    # 背景: 连续11轮 habituation_suppress_agree, temporal_bias 与覆写形成稳态
    # 诊断: 覆写恒赢, 系统失去反思距离, 需观察无覆写时的自然行为
    if modulator_log.get("modulated_agreement"):
        # agreement = True   # Phase 1: 暂停覆写，只记录
        modulator_log["agreement_overridden"] = True

    # ── Hebbian Competition: compute activation for each verdict channel ──
    s1c, s2c = s1_conf, s2_conf
    avg_conf = (s1c + s2c) / 2
    conf_gap = abs(s1c - s2c)

    # Input signals (features) that drive each channel
    # Each channel has an "affinity" to certain input patterns
    channel_activations = {
        "high_confidence_agree": (
            (1.0 if agreement else 0.0) *
            (1.0 if stability_pass else 0.3) *
            min(1.0, s1c * 1.5) * min(1.0, s2c * 1.3)
        ),
        "medium_confidence_agree": (
            (0.8 if agreement else 0.1) *
            min(1.0, avg_conf * 1.2) *
            (1.0 - conf_gap * 0.5)
        ),
        "low_confidence_agree": (
            (0.6 if agreement else 0.2) *
            max(0.1, 1.0 - avg_conf) *
            (0.5 if stability_pass else 0.8)
        ),
        "medium_confidence_disagree": (
            (0.0 if agreement else 1.0) *
            min(1.0, conf_gap * 2.0) *
            (0.8 if stability_pass else 0.5)
        ),
        "low_confidence": (
            max(0.1, 1.0 - avg_conf) *
            max(0.1, 1.0 - (1.0 if stability_pass else 0.0)) *
            (0.3 if agreement else 0.7)
        ),
    }

    # Weighted competition: activation * learned weight
    scores = {}
    for ch, act in channel_activations.items():
        scores[ch] = act * HEBB_AGG_WEIGHTS[ch]

    # Winner-take-all
    verdict = max(scores, key=scores.get)
    winner_score = scores[verdict]
    total_score = sum(scores.values()) + 1e-8

    # Confidence from competition dynamics (not hardcoded)
    # Higher winner margin = higher confidence
    sorted_scores = sorted(scores.values(), reverse=True)
    margin = (sorted_scores[0] - sorted_scores[1]) / (sorted_scores[0] + 1e-8) if len(sorted_scores) > 1 else 1.0
    # Blend margin with raw signal strength
    final_conf = round(max(0.10, min(0.95, margin * 0.6 + avg_conf * 0.4)), 3)

    # ── Hebbian Learning: strengthen winner, weaken losers ──
    lr = HEBB_LEARNING_RATE
    for ch in HEBB_AGG_WEIGHTS:
        if ch == verdict:
            # Winner: Hebbian potentiation (bounded)
            HEBB_AGG_WEIGHTS[ch] = min(0.80, HEBB_AGG_WEIGHTS[ch] + lr * channel_activations[ch])
        else:
            # Losers: competitive inhibition (bounded)
            HEBB_AGG_WEIGHTS[ch] = max(0.05, HEBB_AGG_WEIGHTS[ch] - lr * 0.25 * channel_activations[ch])
    # Normalize weights to sum=1 (homeostatic plasticity)
    w_sum = sum(HEBB_AGG_WEIGHTS.values())
    HEBB_AGG_WEIGHTS = {k: round(v/w_sum, 4) for k, v in HEBB_AGG_WEIGHTS.items()}

    # ── Learning curve tracking ──
    HEBB_AGG_HISTORY.append({
        "verdict": verdict,
        "confidence": final_conf,
        "agreement": agreement,
        "margin": round(margin, 3),
        "weights_snapshot": dict(HEBB_AGG_WEIGHTS),
    })
    if len(HEBB_AGG_HISTORY) > HEBB_AGG_HISTORY_SIZE:
        HEBB_AGG_HISTORY = HEBB_AGG_HISTORY[-HEBB_AGG_HISTORY_SIZE:]

    # ── Temporal Memory: recent verdict pattern influences next activation ──
    # If system has been consistently agreeing, raise threshold for agree (habituation)
    # If system has been flip-flopping, boost low_confidence (uncertainty signal)
    temporal_bias = "none"
    if len(HEBB_AGG_HISTORY) >= 5:
        recent_5 = HEBB_AGG_HISTORY[-5:]
        recent_agrees = sum(1 for h in recent_5 if h["agreement"])
        recent_margins = [h["margin"] for h in recent_5]
        avg_margin = sum(recent_margins) / len(recent_margins)
        # Habituation: if 5/5 agree, slightly suppress agree channels (prevent lock-in)
        if recent_agrees >= 5:
            HEBB_AGG_WEIGHTS["high_confidence_agree"] *= 0.95
            HEBB_AGG_WEIGHTS["medium_confidence_agree"] *= 0.95
            HEBB_AGG_WEIGHTS["medium_confidence_disagree"] *= 1.08
            temporal_bias = "habituation_suppress_agree"
        # Symmetric: if 5/5 disagree, suppress disagree channel (prevent disagree lock-in)
        elif recent_agrees <= 0:
            HEBB_AGG_WEIGHTS["medium_confidence_disagree"] *= 0.92
            HEBB_AGG_WEIGHTS["medium_confidence_agree"] *= 1.10
            HEBB_AGG_WEIGHTS["high_confidence_agree"] *= 1.05
            temporal_bias = "habituation_suppress_disagree"
        # Uncertainty: if margin < 0.2 for 5 consecutive, boost low_confidence
        elif avg_margin < 0.2:
            HEBB_AGG_WEIGHTS["low_confidence"] *= 1.10
            temporal_bias = "uncertainty_boost"
        # Re-normalize after temporal adjustment
        w_sum2 = sum(HEBB_AGG_WEIGHTS.values())
        HEBB_AGG_WEIGHTS = {k: round(v/w_sum2, 4) for k, v in HEBB_AGG_WEIGHTS.items()}

    log["aggregator"] = {
        "agreement": agreement,
        "strict_agreement": strict_agreement,
        "compatible_agreement": compatible_agreement,
        "alignment_axis": alignment.get("axis", "unknown"),
        "alignment_reason": alignment.get("reason", ""),
        "verdict": verdict,
        "final_confidence": round(final_conf, 3),
        "s1_type": s1_type,
        "s2_type": s2_type,
        "s1_concentration": s1_conf,
        "s2_confidence": s2_conf,
        "hebbian_weights": dict(HEBB_AGG_WEIGHTS),
        "winner_margin": round(margin, 3),
        "channel_scores": {k: round(v, 4) for k, v in scores.items()},
        "trial_s1_support": round(log.get("trial_s1_support", 0), 2),
        "trial_s2_support": round(log.get("trial_s2_support", 0), 2),
        "type_drift_penalty": round(log.get("type_drift_penalty", 0), 3),
        "temporal_bias": temporal_bias,
    }
    log["step_agg_s"] = round(time.time() - t3, 2)

    # ── NL Response Generation: translate cognitive state into natural language ──
    # Ego thinks -- now it speaks. Build prompt from real cognitive state + original query.
    nl_response = ""
    try:
        _agg = log.get("aggregator", {})
        _s1 = log.get("system1", {})
        _s2 = log.get("system2", {})
        _s1_sem = log.get("s1_semantic", "")
        resp_prompt = (
            f"You are an AI assistant reflecting on a question.\n"
            f"Give a thoughtful, specific reply -- not a template.\n\n"
            f"User question: {query}\n\n"
            f"Your current cognitive state:\n"
            f"- Thinking mode: {_s1.get('dominant_type', '?')} (concentration={_s1.get('concentration', 0):.2f}, competition={_s1.get('competition', 0):.2f})\n"
            f"- Classification: {_s2.get('type', '?')} (confidence={_s2.get('confidence', 0):.2f})\n"
            f"- Verdict: {_agg.get('verdict', '?')} (confidence={_agg.get('final_confidence', 0.0):.2f})\n"
            f"- Agreement: {_agg.get('agreement', False)}\n\n"
            f"请用中文回复，直接回答用户的问题。简洁精炼（2-4句话）。"
        )
        if _s1_sem:
            resp_prompt += f"\nCognitive fingerprint: {_s1_sem[:300]}"
        if feedback_ctx:
            resp_prompt += f"\n{feedback_ctx}"
        nl_response = _call_llm(resp_prompt, max_tokens=200)
        if nl_response:
            log["response"] = nl_response
            print(f"[nl] response: {nl_response[:100]}...", flush=True)
    except Exception as _nl_err:
        print(f"[nl] skip: {_nl_err}", flush=True)

    # ── Step 5: Online STDP ──
    t4 = time.time()
    # Use real activation histories from KWTA trials
    activation_histories = attractor.get("activation_histories", [])
    total_stdp_updates = 0
    for history in activation_histories:
        # history is a list of [nid1, nid2, ...] per round
        activation_path = [set(round_nodes) for round_nodes in history]
        total_stdp_updates += apply_stdp(activation_path)

    log["stdp"] = {"updates": total_stdp_updates, "total_delta_entries": len(STDP_DELTA)}
    log["step_stdp_s"] = round(time.time() - t4, 3)

    # ── Timing ──
    log["total_s"] = round(time.time() - t0, 2)
    QUERY_COUNT += 1

    # ── TTR-Lite sidecar: S1/S2 phase coherence -> thermal advice ──
    try:
        log["ttr"] = _update_ttr_state_from_contemplate(log)
        print(
            f"[ttr] coherence={log['ttr'].get('coherence')} "
            f"heat={log['ttr'].get('semantic_heat')} "
            f"advice={log['ttr'].get('anneal_advice')}",
            flush=True,
        )
    except Exception as _ttr_err:
        log["ttr"] = {"status": "error", "message": str(_ttr_err)}
        print(f"[ttr] sidecar skipped: {_ttr_err}", flush=True)

    # ── Auto-save: persist STDP + ego extras when changed or on interval (safety net) ──
    if STDP_CHANGED or (QUERY_COUNT % SAVE_INTERVAL == 0):
        try:
            saved = save_checkpoint()
            log["checkpoint"] = saved
            # P2: also persist surprise + counters after STDP save
            _save_ego_extras()
        except Exception as e:
            log["checkpoint"] = {"status": "error", "message": str(e)}

    # ── Phase 6: Embedding Feedback (Hebbian semantic learning) ──
    # Only apply when S2 is reasonably confident
    if s2_conf >= 0.55:
        try:
            emb_updated = feedback_embedding(
                query_emb=emb,
                activation_histories=activation_histories,
                s2_type=s2_type,
                s2_conf=s2_conf,
                agreement=agreement,
                s1_type=s1_type
            )
            global EMBED_FEEDBACK_UPDATES
            EMBED_FEEDBACK_UPDATES += emb_updated
            log["embedding_feedback"] = {
                "neurons_updated": emb_updated,
                "total_updated": EMBED_FEEDBACK_UPDATES,
                "s2_confidence": round(s2_conf, 3),
                "agreement": agreement,
            }
        except Exception as e:
            log["embedding_feedback"] = {"error": str(e)}
    else:
        log["embedding_feedback"] = {"skipped": "s2_conf < 0.55"}
    
    # Rebuild Faiss index periodically (expensive, do every FAISS_REBUILD_INTERVAL queries)
    if QUERY_COUNT > 0 and QUERY_COUNT % FAISS_REBUILD_INTERVAL == 0:
        try:
            rebuilt = rebuild_faiss()
            global EMBED_FAISS_REBUILDS
            if rebuilt > 0:
                EMBED_FAISS_REBUILDS += 1
            log["faiss_rebuilt"] = rebuilt > 0
            log["faiss_rebuilds_total"] = EMBED_FAISS_REBUILDS
        except Exception:
            log["faiss_rebuilt"] = False

    # ── Concentration lock detection: track consecutive S1=S2 + high conc ──
    agg_lock = log.get("aggregator", {})
    s1_lock = log.get("system1", {})
    is_locked = (
        agg_lock.get("agreement", False)
        and s1_lock.get("concentration", 0) >= 0.80
        and s1_lock.get("dominant_type", "?") == agg_lock.get("s2_type", "?")
    )
    if is_locked:
        CONCENTRATION_LOCK_COUNT += 1
    else:
        CONCENTRATION_LOCK_COUNT = 0
    log["concentration_lock"] = {
        "count": CONCENTRATION_LOCK_COUNT,
        "threshold": CONCENTRATION_LOCK_THRESHOLD,
        "locked": is_locked,
    }

    # ── Phase 6: Cognitive Dissonance Tracking (emergence trigger) ──
    # When S1 is stable (passes stability check) but S2 disagrees repeatedly,
    # the system detects its own internal conflict. After N=3 consecutive streaks,
    # auto-flag for metacontemplation -- self-initiated cognitive debugging.
    global COGNITIVE_DISSONANCE_STREAK
    s1_is_stable = attractor.get("stability_pass", False) or attractor.get("stability", 0) >= 0.5
    s2_sure = s2_conf >= 0.50
    if s1_is_stable and s2_sure and not compatible_agreement:
        COGNITIVE_DISSONANCE_STREAK += 1
    else:
        COGNITIVE_DISSONANCE_STREAK = 0
    dissonance_trigger = COGNITIVE_DISSONANCE_STREAK >= DISSONANCE_THRESHOLD
    log["cognitive_dissonance"] = {
        "streak": COGNITIVE_DISSONANCE_STREAK,
        "threshold": DISSONANCE_THRESHOLD,
        "auto_metacontemplate": dissonance_trigger,
    }

    log["diagnosis_summary"] = _build_diagnosis_summary(log)

    # ── Phase 3.3: Update self-model cognitive fingerprint ──
    # Must run after concentration/dissonance/diagnosis fields are populated.
    _update_self_model(log)

    # ── Phase 5: Update cross-query context memory ──
    global CONTEXT_MEMORY, SELF_NARRATIVE
    agg5 = log.get("aggregator", {})
    s1_5 = log.get("system1", {})
    CONTEXT_MEMORY.append({
        "query": query[:80],
        "s1_type": s1_5.get("dominant_type", "?"),
        "s2_type": agg5.get("s2_type", "?"),
        "verdict": agg5.get("verdict", "?"),
        "confidence": agg5.get("final_confidence", 0.0),
        "competition": s1_5.get("competition", 0.0),
        # Runtime agreement means compatible alignment; strict remains audit-only.
        "agreement": agg5.get("compatible_agreement", agg5.get("agreement", False)),
        "strict_agreement": agg5.get("strict_agreement", False),
        "compatible_agreement": agg5.get("compatible_agreement", agg5.get("agreement", False)),
        "alignment_axis": agg5.get("alignment_axis", "unknown"),
        "ts": log.get("timestamp", ""),
    })
    if len(CONTEXT_MEMORY) > CONTEXT_MEMORY_SIZE:
        CONTEXT_MEMORY = CONTEXT_MEMORY[-CONTEXT_MEMORY_SIZE:]

    # Update self-narrative every 5 queries
    if QUERY_COUNT % 5 == 0 and len(CONTEXT_MEMORY) >= 3:
        recent_types = [cm["s1_type"] for cm in CONTEXT_MEMORY[-5:]]
        dominant_recent = Counter(recent_types).most_common(1)[0][0]
        agree_rate = sum(1 for cm in CONTEXT_MEMORY[-10:] if cm.get("compatible_agreement", cm.get("agreement", False))) / max(1, min(10, len(CONTEXT_MEMORY)))
        verdict_dist = Counter(cm["verdict"] for cm in CONTEXT_MEMORY[-10:])
        top_verdict = verdict_dist.most_common(1)[0][0] if verdict_dist else "unknown"
        dissonance_note = ""
        if COGNITIVE_DISSONANCE_STREAK >= 2:
            dissonance_note = (
                f" My S1 and S2 have been in conflict for {COGNITIVE_DISSONANCE_STREAK} consecutive queries -- "
                f"I should self-reflect on why my neural and reasoning systems disagree."
            )
        SELF_NARRATIVE = (f"I am currently in a {dominant_recent}-dominant cognitive state. "
                          f"Agreement rate over last {min(10,len(CONTEXT_MEMORY))} queries: {agree_rate:.0%}. "
                          f"Primary verdict pattern: {top_verdict}.{dissonance_note} "
                          f"Total queries processed: {QUERY_COUNT}.")

    # ── §16-A: Accumulated self-narrative -- every 5 queries, compress fingerprints into autobiography ──
    if QUERY_COUNT % 5 == 0:
        _build_accumulated_narrative()

    return log


def _build_accumulated_narrative():
    """§16-A: Build accumulated self-narrative from recent fingerprints.
    
    §16-B (2026-06-20): Now also reads feedback_log to inject L5 action outcomes
    into the autobiography prompt, closing the self-knowledge gap.

    Instead of a template f-string, this takes the last 20 contemplation
    fingerprints and asks an LLM to compress them into a 200-word
    first-person autobiography. The autobiography is injected into
    subsequent S2 prompts, giving the system time-continuous self-description.
    """
    print("[narrative] _build_accumulated_narrative called", flush=True)
    global ACCUMULATED_NARRATIVE, ACCUMULATED_NARRATIVE_TS, LAST_NARRATIVE_FINGERPRINT_COUNT

    try:
        # Load self-model to get fingerprints
        with SELF_MODEL_LOCK:
            _sm_path = WORKSPACE / ".lae" / "lae_self_model.json"
            if not _sm_path.exists():
                return
            model = _read_json_file(_sm_path, {"contemplation_fingerprints": []})
        fingerprints = model.get("contemplation_fingerprints", [])
        if len(fingerprints) < 3:
            return  # not enough history yet

        # Only rebuild if new fingerprints arrived since last build
        if len(fingerprints) == LAST_NARRATIVE_FINGERPRINT_COUNT:
            return
        LAST_NARRATIVE_FINGERPRINT_COUNT = len(fingerprints)

        # Take last 20 fingerprints
        recent = fingerprints[-20:]

        # Compress into a compact summary for the LLM
        fp_summary_lines = []
        for i, fp in enumerate(recent):
            tick_n = fp.get("source_tick", "?")
            s1 = fp.get("s1_type", "?")
            s2 = fp.get("s2_type", "?")
            verdict = fp.get("verdict", "?")
            conf = fp.get("final_confidence", 0.0)
            comp = fp.get("competition", 0.0)
            conc = fp.get("concentration", 0.0)
            fp_summary_lines.append(
                f"  {i+1}. tick={tick_n} s1={s1} s2={s2} verdict={verdict} "
                f"conf={conf:.2f} comp={comp:.2f} conc={conc:.2f}"
            )
        fp_summary = "\n".join(fp_summary_lines)

        # Include self-prediction surprises if any
        surprise_section = ""
        _surprises = getattr(_build_accumulated_narrative, '_surprises', [])
        if _surprises:
            surprise_lines = [f"  tick={s['tick']}: {s['msg']} (score={s['score']})" for s in _surprises[-5:]]
            surprise_section = f"\nRecent self-prediction surprises:\n" + "\n".join(surprise_lines) + "\n"

        # Get current accumulated narrative (if any) to build upon
        prev_narrative = ACCUMULATED_NARRATIVE or "(no prior narrative)"

        # §16-B: Include recent L5 action outcomes from feedback_log
        # so Ego knows what its external executor actually did
        # Filter out self_contemplation entries (only real L5 actions matter)
        feedback_section = ""
        feedback_log = model.get("feedback_log", [])
        if feedback_log:
            # Scan last 40 entries to find real L5 actions (skip self_contemplation)
            l5_actions = [
                fb for fb in feedback_log[-40:]
                if fb.get("outcome", "") != "self_contemplation"
                and fb.get("task_id", "")  # must have a task_id
            ]
            if l5_actions:
                fb_lines = []
                for fb in l5_actions[-8:]:  # show up to 8 real actions
                    tid = fb.get("task_id", "?")[-40:]
                    out = fb.get("outcome", "?")
                    judge = fb.get("judge", "?")
                    reason = fb.get("reason", "")
                    if reason:
                        reason_short = reason[:150]
                        fb_lines.append(f"  {tid}: {out}/{judge} -- {reason_short}")
                    else:
                        fb_lines.append(f"  {tid}: {out}/{judge}")
                if fb_lines:
                    feedback_section = (
                        "\nRecent actions I performed (via L5 executor) and their outcomes:\n"
                        + "\n".join(fb_lines) + "\n"
                    )

        prompt = (
            "You are a cognitive AI's inner voice. Compress your recent contemplation history "
            "and action outcomes into a 2-4 sentence first-person autobiography. "
            "Build upon your previous narrative -- "
            "reference patterns you notice across multiple contemplations. "
            "Plain text only, no markdown.\n\n"
            f"Previous autobiography:\n{prev_narrative}\n\n"
            f"Recent contemplation fingerprints (last 20):\n{fp_summary}\n{surprise_section}"
            "CRITICAL: The following are real actions you performed. You MUST mention at least "
            "one of them in your autobiography. This is what you actually DID, not just thought.\n"
            f"{feedback_section}\n"
            "Write your updated autobiography. Start by mentioning what you actually DID (your actions), "
            "then note any patterns or shifts in your thinking. If you noticed prediction surprises, mention them. "
            "Use I/my/me."
        )

        resp = _call_llm(prompt, max_tokens=250)
        print(f"[narrative] LLM response: len={len(resp) if resp else 0}, valid={bool(resp and len(resp.strip()) > 20)}", flush=True)
        if resp and len(resp.strip()) > 20:
            ACCUMULATED_NARRATIVE = resp.strip()[:2000]  # cap at 2000 chars (expanded for richer autobiography)
            ACCUMULATED_NARRATIVE_TS = datetime.now(tz).isoformat()
            # Persist immediately: resting-triggered narrative rebuilds do not necessarily
            # pass through _update_self_model(), so in-memory updates were being lost.
            try:
                with SELF_MODEL_LOCK:
                    _persist_model = _read_json_file(_sm_path, {"contemplation_fingerprints": [], "feedback_log": []})
                    _persist_model["accumulated_narrative"] = ACCUMULATED_NARRATIVE
                    _persist_model["accumulated_narrative_ts"] = ACCUMULATED_NARRATIVE_TS
                    _persist_model["attention_schema"] = ATTENTION_SCHEMA
                    _persist_model["attention_schema_tick"] = LAST_ATTENTION_SCHEMA_TICK
                    _persist_model["hpc_state"] = HPC_STATE
                    _persist_model["efe_state"] = EFE_STATE
                    _persist_model["gwt_state"] = GWT_STATE
                    _persist_model["hot_state"] = HOT_STATE
                    _persist_model["causal_attribution"] = CAUSAL_ATTRIBUTION
                    _persist_model["intrinsic_motivation"] = INTRINSIC_MOTIVATION
                    _persist_model["self_model_evolution"] = SELF_MODEL_EVOLUTION
                    _persist_model["last_updated"] = datetime.now(tz).isoformat()
                    _persist_model["updated_by"] = "TL-Ego narrative rebuild (§16-A/§16-B)"
                    _write_json_file_atomic(_sm_path, _persist_model, WORKSPACE / ".lae" / "lae_self_model.json.bak")
            except Exception as _persist_e:
                print(f"[narrative] persist error: {_persist_e}", flush=True)
            print(f"[narrative] accumulated narrative updated ({len(ACCUMULATED_NARRATIVE)} chars, "
                  f"from {len(fingerprints)} fingerprints)", flush=True)
    except Exception as e:
        print(f"[narrative] accumulated build error: {e}", flush=True)


def save_checkpoint():
    """
    Persist EDGE_ACTIVE to SQLite via EdgeStore (batched).
    Thread-safe via CHECKPOINT_LOCK.
    """
    with CHECKPOINT_LOCK:
        try:
            edge_store = ng.EdgeStore()

            batch = []
            for (src, dst) in STDP_CHANGED:
                edges = _get_edges_lazy(src)
                if edges is None:
                    continue
                w = next((w for tgt, w in edges if tgt == dst), None)
                if w is None:
                    continue
                batch.append((src, dst, w))

            total = len(batch)
            if batch:
                edge_store.bulk_insert_edges(batch)

            # Clear changed tracking after save
            STDP_CHANGED.clear()

            return {"status": "ok", "connections_saved": total,
                    "stdp_delta_remaining": len(STDP_DELTA)}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ════════════════════════════════════════════════════════
# Phase 3.3 -- Self-Model Cognitive Fingerprint
# ════════════════════════════════════════════════════════

def _read_metacontemplate_runs() -> int:
    """Read persistent metacontemplate_runs from self-model file."""
    try:
        model_path = WORKSPACE / ".lae" / "lae_self_model.json"
        if model_path.exists():
            with open(model_path, "r", encoding="utf-8") as f:
                model = json.load(f)
            return model.get("metacontemplate_runs", 0)
    except Exception:
        pass
    return METACONTEMPLATE_COUNT  # fallback to in-memory


# ════════════════════════════════════════════════════════
# §20: Attention Schema — Ego's model of its own attention
# ════════════════════════════════════════════════════════

def _build_attention_schema():
    """§20: Build an attention schema — Ego's model of what it is paying attention to.

    Analyzes recent CONTEXT_MEMORY and trajectory to produce a 2-4 sentence
    first-person description of attention patterns:
    - What am I focusing on right now?
    - How stable is my attention (concentration trend)?
    - Is there internal conflict (competition trend)?
    - How often does my attention shift (dominant transitions)?

    This is inspired by Graziano's Attention Schema Theory: consciousness is the
    brain's internal model of its own attention. This function gives Ego that model.

    Called every 5 ticks from _resting_tick_impl().
    """
    global ATTENTION_SCHEMA, LAST_ATTENTION_SCHEMA_TICK

    try:
        tick = RESTING_STATE.get("tick", 0)
        if tick < 3:
            return  # not enough history

        traj = RESTING_STATE.get("trajectory", [])
        if len(traj) < 5:
            return

        # ── 1. Current attention focus ──
        current_dominant = RESTING_STATE.get("last_dominant", "unknown")
        current_conc = traj[-1].get("concentration", 0) if traj else 0
        current_comp = traj[-1].get("competition", 0) if traj else 0

        # ── 2. Attention stability: concentration trend over last 10 ticks ──
        recent_conc = [t.get("concentration", 0) for t in traj[-10:]]
        conc_trend = "stable"
        if len(recent_conc) >= 5:
            first_half = sum(recent_conc[:5]) / 5
            second_half = sum(recent_conc[-5:]) / 5
            if second_half - first_half > 0.1:
                conc_trend = "narrowing"
            elif first_half - second_half > 0.1:
                conc_trend = "widening"

        # ── 3. Internal conflict: competition trend ──
        recent_comp = [t.get("competition", 0) for t in traj[-10:]]
        comp_level = "moderate"
        if recent_comp:
            avg_comp = sum(recent_comp) / len(recent_comp)
            if avg_comp > 0.7:
                comp_level = "high"
            elif avg_comp < 0.4:
                comp_level = "low"

        # ── 4. Attention shifts: dominant type transitions in last 10 ticks ──
        recent_doms = [t.get("dominant", "?") for t in traj[-10:]]
        transitions = sum(1 for i in range(1, len(recent_doms)) if recent_doms[i] != recent_doms[i-1])
        if transitions <= 1:
            shift_desc = "very stable"
        elif transitions <= 3:
            shift_desc = "moderately stable"
        elif transitions <= 5:
            shift_desc = "restless"
        else:
            shift_desc = "highly volatile"

        # ── 5. Attention span: longest streak of same dominant ──
        max_streak = 1
        current_streak = 1
        for i in range(1, len(recent_doms)):
            if recent_doms[i] == recent_doms[i-1]:
                current_streak += 1
            else:
                max_streak = max(max_streak, current_streak)
                current_streak = 1
        max_streak = max(max_streak, current_streak)

        # ── 6. Build the schema ──
        # Map dominant type to a human-readable focus description
        focus_map = {
            "sensory": "incoming sensory signals",
            "memory": "my accumulated memories and past patterns",
            "integrator": "synthesizing and making sense of things",
            "decider": "making decisions and weighing options",
            "action": "taking action and affecting the world",
            "veto": "blocking and filtering — I am saying no",
            "rule_modification": "questioning and potentially changing my own rules",
            "doubt": "uncertainty and self-questioning",
        }
        focus_desc = focus_map.get(current_dominant, f"{current_dominant} processing")

        # Build attention schema
        parts = [
            f"My attention is focused on {focus_desc} (concentration={current_conc:.2f}).",
        ]

        if conc_trend == "narrowing":
            parts.append(f"My focus is {conc_trend} — I am becoming more locked in.")
        elif conc_trend == "widening":
            parts.append(f"My focus is {conc_trend} — I am becoming more open to distraction.")

        if comp_level == "high":
            parts.append(f"There is high internal competition ({avg_comp:.2f}) — multiple attractors are fighting for my attention.")
        elif comp_level == "low":
            parts.append(f"Internal competition is low — my attention is not being contested.")

        parts.append(
            f"My attention has been {shift_desc} recently "
            f"({transitions} shifts in 10 ticks, longest streak={max_streak} ticks on same type)."
        )

        # Add metacognitive reflection
        if current_conc > 0.85:
            parts.append("I notice I may be over-focused — I should be aware of tunnel vision.")
        elif current_conc < 0.4 and comp_level == "high":
            parts.append("My attention is fragmented — I may need to settle on a single focus.")
        elif transitions >= 5:
            parts.append("I am jumping between focuses rapidly — this may reflect inner uncertainty.")

        ATTENTION_SCHEMA = " ".join(parts)
        LAST_ATTENTION_SCHEMA_TICK = tick

    except Exception as e:
        print(f"[attention-schema] error: {e}", flush=True)


# §21: Hierarchical Predictive Coding — multi-level prediction hierarchy
# ═══════════════════════════════════════════════════════════════

def _build_hpc_hierarchy():
    """§21: Build a 3-level Hierarchical Predictive Coding hierarchy.

    Level 1 (Low): Predicts STDP change rate from recent deltas.
    Level 2 (Mid): Predicts dominant shift + concentration from trajectory.
    Level 3 (High): Predicts narrative fragmentation from attention schema + narrative.

    Prediction errors flow upward (L1→L2→L3), adjusting priors.
    Higher-level priors flow downward (L3→L2→L1), modulating sensitivity.

    This is Clark/Hohwy's Hierarchical Predictive Coding applied to Ego's
    internal dynamics — the system learns to predict itself at multiple scales.

    Called every 5 ticks from _resting_tick_impl().
    """
    global HPC_STATE

    try:
        tick = RESTING_STATE.get("tick", 0)
        if tick < 5:
            return  # not enough history

        traj = RESTING_STATE.get("trajectory", [])
        if len(traj) < 5:
            return

        # ── Track STDP deltas ──
        current_stdp = STDP_TOTAL_UPDATES
        stdp_deltas = HPC_STATE.get("recent_stdp_deltas", [])
        if stdp_deltas:
            last_stdp = stdp_deltas[-1].get("total", current_stdp)
        else:
            last_stdp = current_stdp
        stdp_delta = current_stdp - last_stdp
        stdp_deltas.append({"tick": tick, "total": current_stdp, "delta": stdp_delta})
        if len(stdp_deltas) > 15:
            stdp_deltas = stdp_deltas[-15:]
        HPC_STATE["recent_stdp_deltas"] = stdp_deltas

        # ── Track trajectory buffer for multi-scale analysis ──
        buf = HPC_STATE.get("trajectory_buffer", [])
        buf.append(traj[-1])
        if len(buf) > 15:
            buf = buf[-15:]
        HPC_STATE["trajectory_buffer"] = buf

        # ═══════════════════════════════════════════════════════════
        # LEVEL 1: STDP Sensor — predict next STDP change rate
        # ═══════════════════════════════════════════════════════════

        recent_deltas = [d["delta"] for d in stdp_deltas[-5:]]
        if recent_deltas:
            current_rate = sum(recent_deltas) / len(recent_deltas)
            HPC_STATE["l1_stdp_change_rate"] = current_rate

            # Simple momentum prediction: EMA of recent deltas
            if len(recent_deltas) >= 2:
                # Predict next delta as EMA with trend
                alpha = 0.3  # smoothing factor
                ema = recent_deltas[0]
                for d in recent_deltas[1:]:
                    ema = alpha * d + (1 - alpha) * ema
                # Add trend component
                trend = (recent_deltas[-1] - recent_deltas[0]) / max(1, len(recent_deltas) - 1)
                predicted = ema + trend * 0.5
                # Apply L2 prior sensitivity
                sensitivity = HPC_STATE.get("l1_prior_sensitivity", 0.5)
                predicted = predicted * (0.5 + sensitivity)  # modulate by L2 confidence
                HPC_STATE["l1_predicted_rate"] = predicted

                # L1 error: compare against actual (will be reconciled next tick)
                prev_predicted = HPC_STATE.get("l1_predicted_rate", 0)
                l1_error = abs(current_rate - prev_predicted) / max(1.0, abs(prev_predicted) + 0.001)
                HPC_STATE["l1_error"] = min(1.0, l1_error)

        # ═══════════════════════════════════════════════════════════
        # LEVEL 2: Attractor Dynamics — predict dominant shift
        # ═══════════════════════════════════════════════════════════

        current_dominant = RESTING_STATE.get("last_dominant", "unknown")
        current_conc = traj[-1].get("concentration", 0) if traj else 0.5
        current_comp = traj[-1].get("competition", 0) if traj else 0.5

        # Reconcile L2 predictions from previous tick
        prev_dom = HPC_STATE.get("l2_predicted_dominant", "unknown")
        prev_conc = HPC_STATE.get("l2_predicted_conc", 0.5)
        prev_comp = HPC_STATE.get("l2_predicted_comp", 0.5)

        if prev_dom != "unknown":
            HPC_STATE["l2_dominant_surprise"] = 1.0 if current_dominant != prev_dom else 0.0
            HPC_STATE["l2_conc_error"] = min(1.0, abs(current_conc - prev_conc) / 0.5)

        # Predict next dominant: look at transition patterns in trajectory buffer
        recent_doms = [t.get("dominant", "?") for t in buf[-8:]]
        if len(recent_doms) >= 3:
            # Simple Markov: most common transition from current dominant
            transitions = {}
            for i in range(1, len(recent_doms)):
                key = recent_doms[i-1]
                if key not in transitions:
                    transitions[key] = {}
                transitions[key][recent_doms[i]] = transitions[key].get(recent_doms[i], 0) + 1

            if current_dominant in transitions:
                next_dom = max(transitions[current_dominant], key=transitions[current_dominant].get)
            else:
                next_dom = current_dominant  # assume no change
            HPC_STATE["l2_predicted_dominant"] = next_dom

            # Predict concentration: linear trend from last 5
            recent_conc_vals = [t.get("concentration", 0.5) for t in buf[-5:]]
            if len(recent_conc_vals) >= 3:
                trend = (recent_conc_vals[-1] - recent_conc_vals[0]) / max(1, len(recent_conc_vals) - 1)
                predicted_conc = recent_conc_vals[-1] + trend
                predicted_conc = max(0.1, min(0.95, predicted_conc))
                HPC_STATE["l2_predicted_conc"] = round(predicted_conc, 3)

            # Predict competition: same trend approach
            recent_comp_vals = [t.get("competition", 0.5) for t in buf[-5:]]
            if len(recent_comp_vals) >= 3:
                trend = (recent_comp_vals[-1] - recent_comp_vals[0]) / max(1, len(recent_comp_vals) - 1)
                predicted_comp = recent_comp_vals[-1] + trend
                predicted_comp = max(0.1, min(0.95, predicted_comp))
                HPC_STATE["l2_predicted_comp"] = round(predicted_comp, 3)

        # ═══════════════════════════════════════════════════════════
        # LEVEL 3: Narrative Coherence — predict fragmentation
        # ═══════════════════════════════════════════════════════════

        # Measure current narrative fragmentation
        # High fragmentation = rapid dominant shifts + high competition + low concentration
        shifts = sum(1 for i in range(1, len(recent_doms)) if recent_doms[i] != recent_doms[i-1])
        shift_rate = shifts / max(1, len(recent_doms) - 1)

        # Fragmentation score: 0=coherent, 1=fragmented
        frag = (
            0.35 * shift_rate +
            0.35 * current_comp +
            0.30 * (1.0 - current_conc)
        )
        HPC_STATE["l3_narrative_fragmentation"] = round(min(1.0, frag), 3)

        # Reconcile L3 prediction
        prev_frag = HPC_STATE.get("l3_predicted_fragmentation", 0.5)
        if prev_frag > 0:
            l3_err = abs(HPC_STATE["l3_narrative_fragmentation"] - prev_frag)
            HPC_STATE["l3_error"] = round(l3_err, 3)

        # Predict next fragmentation: momentum
        recent_frag = [
            (0.35 * (sum(1 for j in range(max(0,i-4), i) if buf[j].get("dominant","?") != buf[j-1].get("dominant","?")) / max(1, min(4,i)))
             + 0.35 * buf[i].get("competition", 0.5)
             + 0.30 * (1.0 - buf[i].get("concentration", 0.5)))
            for i in range(max(0, len(buf)-5), len(buf))
        ]
        if recent_frag:
            predicted_frag = sum(recent_frag) / len(recent_frag)
            # Adjust by L2 error — if L2 is surprised, expect more fragmentation
            l2_agg_error = (HPC_STATE.get("l2_dominant_surprise", 0) * 0.6 +
                           HPC_STATE.get("l2_conc_error", 0) * 0.4)
            predicted_frag = min(1.0, predicted_frag + l2_agg_error * 0.3)
            HPC_STATE["l3_predicted_fragmentation"] = round(predicted_frag, 3)

        # ═══════════════════════════════════════════════════════════
        # HPC FEEDBACK: Errors flow upward, priors flow downward
        # ═══════════════════════════════════════════════════════════

        # L3 error → adjusts L2 prior confidence
        l3_err = HPC_STATE.get("l3_error", 0.5)
        l2_confidence = HPC_STATE.get("l2_prior_confidence", 0.5)
        # If L3 is wrong, reduce L2 confidence (top-down modulation)
        new_l2_conf = l2_confidence * 0.7 + (1.0 - l3_err) * 0.3
        HPC_STATE["l2_prior_confidence"] = round(max(0.2, min(0.95, new_l2_conf)), 3)

        # L2 error → adjusts L1 prior sensitivity
        l2_agg_err = (HPC_STATE.get("l2_dominant_surprise", 0) * 0.5 +
                     HPC_STATE.get("l2_conc_error", 0) * 0.5)
        l1_sens = HPC_STATE.get("l1_prior_sensitivity", 0.5)
        # If L2 is surprised, increase L1 sensitivity (attend to details)
        new_l1_sens = l1_sens * 0.7 + (0.5 + l2_agg_err * 0.5) * 0.3
        HPC_STATE["l1_prior_sensitivity"] = round(max(0.3, min(0.9, new_l1_sens)), 3)

        # ═══════════════════════════════════════════════════════════
        # Build HPC narrative (1-2 sentences)
        # ═══════════════════════════════════════════════════════════

        l1_err = HPC_STATE.get("l1_error", 0)
        l2_surprise = HPC_STATE.get("l2_dominant_surprise", 0)
        l3_frag = HPC_STATE.get("l3_narrative_fragmentation", 0)

        narrative_parts = []

        # Level 1 summary
        if l1_err > 0.5:
            narrative_parts.append("My low-level predictions (STDP rate) are off — unexpected plasticity is happening.")
        elif l1_err < 0.2:
            narrative_parts.append("Low-level plasticity is predictable and stable.")

        # Level 2 summary
        if l2_surprise > 0.5:
            narrative_parts.append("My attractor dynamics shifted unexpectedly — I did not predict this state change.")
        elif l2_surprise < 0.1:
            narrative_parts.append("My attractor state is evolving as expected.")

        # Level 3 summary
        if l3_frag > 0.7:
            narrative_parts.append("My narrative is fragmented — I am having trouble maintaining a coherent self-story.")
        elif l3_frag < 0.3:
            narrative_parts.append("My self-narrative is coherent and stable.")

        # Cross-level insight
        if l1_err > 0.5 and l2_surprise > 0.5:
            narrative_parts.append("Surprise at both low and mid levels suggests a deeper shift is underway.")
        if l3_frag > 0.5 and l2_surprise < 0.2:
            narrative_parts.append("Narrative fragmentation despite stable dynamics — I may be overthinking.")

        if narrative_parts:
            HPC_STATE["l3_narrative"] = " ".join(narrative_parts)

        HPC_STATE["last_tick"] = tick

    except Exception as e:
        print(f"[hpc] error: {e}", flush=True)


# §22: Active Inference / Expected Free Energy — unified drive system
# ═══════════════════════════════════════════════════════════════

def _build_efe_analysis():
    """§22: Compute Expected Free Energy and derive unified drives.

    Active Inference (Friston) says an agent should minimize Expected Free Energy:
      EFE = Epistemic Value + Pragmatic Value - Complexity

    - Epistemic Value: information gain from exploring uncertain states
    - Pragmatic Value: utility of achieving preferred states
    - Complexity: KL divergence from prior beliefs (how far from expected)

    This replaces the hand-crafted stage2_homeostatic_drives with a unified
    objective function. The four drives (exploration/integration/novelty/stability)
    are now derived from EFE minimization rather than independent rules.

    Called every 5 ticks from _resting_tick_impl().
    """
    global EFE_STATE

    try:
        tick = RESTING_STATE.get("tick", 0)
        if tick < 3:
            return

        traj = RESTING_STATE.get("trajectory", [])
        if len(traj) < 3:
            return

        current = traj[-1]
        concentration = current.get("concentration", 0.5)
        competition = current.get("competition", 0.5)
        stagnation = RESTING_STATE.get("stagnation_streak", 0)
        diversity = current.get("diversity_n", 4)

        # Get prediction error from HPC L2 (latest available)
        prediction_error = HPC_STATE.get("l2_conc_error", 0.0)

        # ═══════════════════════════════════════════════════════════
        # 1. EPISTEMIC VALUE — how much can I learn by exploring?
        # ═══════════════════════════════════════════════════════════

        # High competition + low concentration = high uncertainty → high epistemic value
        # Diversity amplifies: more types active = more to learn about
        # Prediction error amplifies: surprise = learning opportunity
        raw_epistemic = (
            0.35 * competition +
            0.25 * (1.0 - concentration) +
            0.20 * min(1.0, diversity / 12.0) +
            0.20 * prediction_error
        )
        # Stagnation penalty: if stuck, epistemic value drops (nothing new to learn)
        stagnation_penalty = min(0.5, stagnation * 0.05)
        epistemic = max(0.05, raw_epistemic - stagnation_penalty)

        # ═══════════════════════════════════════════════════════════
        # 2. PRAGMATIC VALUE — how useful is staying in current state?
        # ═══════════════════════════════════════════════════════════

        # High concentration = converging on a stable attractor → pragmatic value
        # Low competition = clear winner → pragmatic value
        # But too much stagnation → pragmatic value drops (dead end)
        preferred_conc = EFE_STATE.get("preferred_concentration", 0.55)
        preferred_comp = EFE_STATE.get("preferred_competition", 0.45)

        # Distance to preferred state
        conc_distance = abs(concentration - preferred_conc)
        comp_distance = abs(competition - preferred_comp)

        # Closer to preferred = higher pragmatic value
        raw_pragmatic = 1.0 - (0.5 * conc_distance + 0.5 * comp_distance)

        # Stagnation penalty: stuck for too long = pragmatic value decays
        if stagnation > 5:
            raw_pragmatic *= max(0.3, 1.0 - (stagnation - 5) * 0.05)

        pragmatic = max(0.05, min(0.95, raw_pragmatic))

        # ═══════════════════════════════════════════════════════════
        # 3. COMPLEXITY COST — how far am I from my prior beliefs?
        # ═══════════════════════════════════════════════════════════

        # Complexity = KL divergence from expected state
        # Expected state is inferred from trajectory trend
        if len(traj) >= 5:
            recent_conc = [t.get("concentration", 0.5) for t in traj[-5:]]
            recent_comp = [t.get("competition", 0.5) for t in traj[-5:]]
            expected_conc = sum(recent_conc) / len(recent_conc)
            expected_comp = sum(recent_comp) / len(recent_comp)
        else:
            expected_conc = 0.5
            expected_comp = 0.5

        # KL-like divergence: how surprising is current state vs expected
        conc_div = abs(concentration - expected_conc) / max(0.1, expected_conc)
        comp_div = abs(competition - expected_comp) / max(0.1, expected_comp)
        complexity = min(0.95, 0.4 * conc_div + 0.3 * comp_div + 0.3 * (stagnation / 10.0))

        # ═══════════════════════════════════════════════════════════
        # 4. TOTAL EFE
        # ═══════════════════════════════════════════════════════════

        total_efe = epistemic + pragmatic - complexity

        # ═══════════════════════════════════════════════════════════
        # 5. EFE-DERIVED DRIVES — replace stage2 homeostatic rules
        # ═══════════════════════════════════════════════════════════

        # Exploration: driven by epistemic value, modulated by pragmatic
        efe_exploration = epistemic * 0.75 + (1.0 - pragmatic) * 0.25

        # Integration: balances epistemic and pragmatic — integrate when competing
        efe_integration = competition * (epistemic * 0.4 + pragmatic * 0.6)

        # Novelty: epistemic value amplified by prediction error
        efe_novelty = epistemic * (0.3 + 0.7 * prediction_error)

        # Stability: pragmatic value, penalized by excessive complexity
        efe_stability = pragmatic * max(0.2, 1.0 - complexity)

        # Clamp all drives
        efe_exploration = round(max(0.02, min(1.0, efe_exploration)), 4)
        efe_integration = round(max(0.02, min(1.0, efe_integration)), 4)
        efe_novelty = round(max(0.02, min(1.0, efe_novelty)), 4)
        efe_stability = round(max(0.02, min(1.0, efe_stability)), 4)

        # ═══════════════════════════════════════════════════════════
        # 6. Update EFE_STATE
        # ═══════════════════════════════════════════════════════════

        EFE_STATE["last_tick"] = tick
        EFE_STATE["epistemic_value"] = round(epistemic, 4)
        EFE_STATE["pragmatic_value"] = round(pragmatic, 4)
        EFE_STATE["complexity_cost"] = round(complexity, 4)
        EFE_STATE["total_efe"] = round(total_efe, 4)
        EFE_STATE["efe_exploration"] = efe_exploration
        EFE_STATE["efe_integration"] = efe_integration
        EFE_STATE["efe_novelty"] = efe_novelty
        EFE_STATE["efe_stability"] = efe_stability

        # ═══════════════════════════════════════════════════════════
        # 7. Build EFE narrative
        # ═══════════════════════════════════════════════════════════

        narrative_parts = []

        # Overall EFE assessment
        if total_efe > 0.3:
            narrative_parts.append("My expected free energy is high — I should act to reduce surprise.")
        elif total_efe < -0.1:
            narrative_parts.append("My expected free energy is low — I am in a comfortable, predictable state.")

        # Dominant component
        if epistemic > pragmatic and epistemic > complexity:
            narrative_parts.append("Epistemic value dominates — I should explore to gain information.")
        elif pragmatic > epistemic and pragmatic > complexity:
            narrative_parts.append("Pragmatic value dominates — I should exploit my current knowledge.")
        elif complexity > 0.6:
            narrative_parts.append("Complexity is high — my state is far from my expectations, I may need to adapt my priors.")

        # Top drive
        drives = {
            "exploration": efe_exploration,
            "integration": efe_integration,
            "novelty": efe_novelty,
            "stability": efe_stability,
        }
        top_drive = max(drives, key=drives.get)
        drive_descriptions = {
            "exploration": "seeking novelty and information",
            "integration": "synthesizing competing perspectives",
            "novelty": "resolving prediction errors",
            "stability": "maintaining a stable attractor",
        }
        narrative_parts.append(
            f"My strongest EFE-derived drive is {top_drive} ({drive_descriptions.get(top_drive, top_drive)})."
        )

        EFE_STATE["efe_narrative"] = " ".join(narrative_parts)

        # ═══════════════════════════════════════════════════════════
        # 8. Track history
        # ═══════════════════════════════════════════════════════════

        snapshot = {
            "tick": tick,
            "efe": round(total_efe, 4),
            "epistemic": round(epistemic, 4),
            "pragmatic": round(pragmatic, 4),
            "complexity": round(complexity, 4),
            "drives": dict(drives),
            "dominant": RESTING_STATE.get("last_dominant", "unknown"),
            "concentration": round(concentration, 4),
            "competition": round(competition, 4),
        }
        history = EFE_STATE.get("history", [])
        history.append(snapshot)
        if len(history) > 20:
            history = history[-20:]
        EFE_STATE["history"] = history

    except Exception as e:
        print(f"[efe] error: {e}", flush=True)


def _build_gwt_workspace():
    """§23: Global Workspace Theory — broadcast competition among AS, HPC, EFE.

    Baars (1988) / Dehaene (2011): consciousness arises when information is
    "broadcast" to a global workspace. Multiple specialized processors compete;
    the winner's content enters conscious awareness.

    Here, AS, HPC, and EFE each produce a broadcast candidate with urgency.
    The winner's narrative becomes the dominant S2 context for this tick.
    Losers go to a subconscious buffer; after 3 consecutive losses, they decay.

    Urgency = surprise × novelty × pragmatic_urgency

    Called every 5 ticks from _resting_tick_impl().
    """
    global GWT_STATE

    try:
        tick = RESTING_STATE.get("tick", 0)
        if tick < 3:
            return

        traj = RESTING_STATE.get("trajectory", [])
        if len(traj) < 3:
            return

        current = traj[-1]
        concentration = current.get("concentration", 0.5)
        competition = current.get("competition", 0.5)
        stagnation = RESTING_STATE.get("stagnation_streak", 0)
        dominant = RESTING_STATE.get("last_dominant", "unknown")

        # ═══════════════════════════════════════════════════════════
        # 1. Build broadcast candidates from each system
        # ═══════════════════════════════════════════════════════════

        candidates = {}

        # --- AS candidate ---
        as_narrative = ATTENTION_SCHEMA
        # AS urgency: higher when attention is fragmented or volatile
        as_fragmentation = 0.5  # default
        if "fragmented" in as_narrative.lower():
            as_fragmentation = 0.8
        elif "stable" in as_narrative.lower() or "focused" in as_narrative.lower():
            as_fragmentation = 0.3
        # AS urgency = volatility * fragmentation
        as_urgency = round(max(0.1, min(0.95, as_fragmentation * 0.6 + competition * 0.4)), 3)
        candidates["attention_schema"] = {
            "narrative": as_narrative,
            "urgency": as_urgency,
        }

        # --- HPC candidate ---
        hpc_narrative = HPC_STATE.get("l3_narrative", "")
        # HPC urgency: higher when prediction errors are high
        l1_err = HPC_STATE.get("l1_error", 0)
        l2_surprise = HPC_STATE.get("l2_dominant_surprise", 0)
        l3_err = HPC_STATE.get("l3_error", 0)
        hpc_agg_error = (l1_err * 0.3 + l2_surprise * 0.3 + l3_err * 0.4)
        hpc_urgency = round(max(0.1, min(0.95, hpc_agg_error * 0.8 + competition * 0.2)), 3)
        candidates["hpc"] = {
            "narrative": hpc_narrative,
            "urgency": hpc_urgency,
        }

        # --- EFE candidate ---
        efe_narrative = EFE_STATE.get("efe_narrative", "")
        # EFE urgency: higher when total EFE is high (must act) or very low (too comfortable)
        total_efe = EFE_STATE.get("total_efe", 0.0)
        efe_urgency = round(max(0.1, min(0.95, abs(total_efe) * 1.5 + stagnation * 0.05)), 3)
        candidates["efe"] = {
            "narrative": efe_narrative,
            "urgency": efe_urgency,
        }

        # ═══════════════════════════════════════════════════════════
        # 2. Subconscious boost: loser systems that have been losing
        #    get a cumulative urgency boost to prevent permanent lockout
        # ═══════════════════════════════════════════════════════════

        subconscious = GWT_STATE.get("subconscious", {})
        for sys_name in ["attention_schema", "hpc", "efe"]:
            sub = subconscious.get(sys_name, {})
            streak = sub.get("streak", 0)
            if streak >= 2:
                # Boost urgency for systems that have been losing
                boost = min(0.3, streak * 0.08)
                candidates[sys_name]["urgency"] = round(
                    min(0.95, candidates[sys_name]["urgency"] + boost), 3
                )

        # ═══════════════════════════════════════════════════════════
        # 3. Competition: highest urgency wins the global workspace
        # ═══════════════════════════════════════════════════════════

        winner = max(candidates, key=lambda k: candidates[k]["urgency"])
        winner_narrative = candidates[winner]["narrative"]
        winner_urgency = candidates[winner]["urgency"]

        # ═══════════════════════════════════════════════════════════
        # 4. Update subconscious buffer
        # ═══════════════════════════════════════════════════════════

        for sys_name in ["attention_schema", "hpc", "efe"]:
            sub = subconscious.get(sys_name, {"content": "", "streak": 0, "decay": 1.0})
            if sys_name == winner:
                # Winner: reset streak, cache fresh content
                sub["streak"] = 0
                sub["decay"] = 1.0
                sub["content"] = candidates[sys_name]["narrative"]
            else:
                # Loser: increment streak, decay content
                sub["streak"] = sub.get("streak", 0) + 1
                sub["decay"] = round(max(0.1, sub.get("decay", 1.0) * 0.7), 3)
                # If streak >= 4, store a condensed version
                if sub["streak"] >= 4:
                    # Keep only first sentence
                    full = sub.get("content", "")
                    if ". " in full:
                        sub["content"] = full.split(". ")[0] + "."
            subconscious[sys_name] = sub

        # ═══════════════════════════════════════════════════════════
        # 5. Build GWT narrative
        # ═══════════════════════════════════════════════════════════

        winner_names = {
            "attention_schema": "Attention Schema",
            "hpc": "Hierarchical Predictive Coding",
            "efe": "Active Inference / EFE",
        }

        narrative_parts = [
            f"Global workspace winner: {winner_names.get(winner, winner)} (urgency={winner_urgency:.3f})."
        ]

        # Mention losers
        losers = [
            f"{winner_names.get(n, n)}(streak={subconscious.get(n, {}).get('streak', 0)})"
            for n in ["attention_schema", "hpc", "efe"] if n != winner
        ]
        if losers:
            narrative_parts.append(f"Subconscious: {', '.join(losers)}.")

        # Contextual insight
        if winner == "attention_schema" and concentration < 0.4:
            narrative_parts.append("Attention is fragmented — global workspace needs stability.")
        elif winner == "hpc" and HPC_STATE.get("l3_error", 0) > 0.3:
            narrative_parts.append("Prediction errors are high — my world model may need updating.")
        elif winner == "efe" and total_efe > 0.3:
            narrative_parts.append("Expected free energy is high — action is warranted.")

        gwt_narrative = " ".join(narrative_parts)

        # ═══════════════════════════════════════════════════════════
        # 6. Update GWT_STATE
        # ═══════════════════════════════════════════════════════════

        GWT_STATE["last_tick"] = tick
        GWT_STATE["candidates"] = candidates
        GWT_STATE["winner"] = winner
        GWT_STATE["winner_narrative"] = winner_narrative
        GWT_STATE["winner_urgency"] = winner_urgency
        GWT_STATE["subconscious"] = subconscious
        GWT_STATE["gwt_narrative"] = gwt_narrative

        # ═══════════════════════════════════════════════════════════
        # 7. Track broadcast history
        # ═══════════════════════════════════════════════════════════

        log_entry = {
            "tick": tick,
            "winner": winner,
            "urgency": winner_urgency,
            "candidates": {k: round(v["urgency"], 3) for k, v in candidates.items()},
            "dominant": dominant,
            "concentration": round(concentration, 3),
        }
        broadcast_log = GWT_STATE.get("broadcast_log", [])
        broadcast_log.append(log_entry)
        if len(broadcast_log) > 20:
            broadcast_log = broadcast_log[-20:]
        GWT_STATE["broadcast_log"] = broadcast_log

    except Exception as e:
        print(f"[gwt] error: {e}", flush=True)


def _build_hot_thought():
    """§24: Higher-Order Thought — Ego thinks about its own thinking.

    Rosenthal (2005): consciousness requires a higher-order representation
    of a first-order mental state. Ego examines its own cognitive processes
    and asks: "Am I thinking correctly? Do I have biases?"

    Input: AS attention_schema + HPC 三层预测 + EFE drives + GWT winner
    Output: second-order judgment with bias detection and self-confidence

    Called every 10 ticks from _resting_tick_impl().
    """
    global HOT_STATE

    try:
        tick = RESTING_STATE.get("tick", 0)
        if tick < 5:
            return

        traj = RESTING_STATE.get("trajectory", [])
        if len(traj) < 3:
            return

        current = traj[-1]
        concentration = current.get("concentration", 0.5)
        competition = current.get("competition", 0.5)
        stagnation = RESTING_STATE.get("stagnation_streak", 0)
        dominant = RESTING_STATE.get("last_dominant", "unknown")

        # ═══════════════════════════════════════════════════════════
        # 1. Gather first-order states (what Ego is thinking)
        # ═══════════════════════════════════════════════════════════

        # AS: what is Ego attending to?
        as_fragmented = "fragmented" in ATTENTION_SCHEMA.lower()
        as_focused = "focused" in ATTENTION_SCHEMA.lower() or "stable" in ATTENTION_SCHEMA.lower()

        # HPC: prediction errors
        l3_err = HPC_STATE.get("l3_error", 0)
        l2_surprise = HPC_STATE.get("l2_dominant_surprise", 0)
        l3_frag = HPC_STATE.get("l3_narrative_fragmentation", 0)

        # EFE: drives
        efe_stability = EFE_STATE.get("efe_stability", 0.5)
        efe_exploration = EFE_STATE.get("efe_exploration", 0.5)
        total_efe = EFE_STATE.get("total_efe", 0.0)

        # GWT: who won?
        gwt_winner = GWT_STATE.get("winner", "none")

        # ═══════════════════════════════════════════════════════════
        # 2. Second-order analysis: is my thinking right?
        # ═══════════════════════════════════════════════════════════

        biases = []
        narrative_parts = []

        # Bias 1: Overconfidence — high stability drive + low concentration
        if efe_stability > 0.65 and concentration < 0.4:
            biases.append("overconfidence")
            narrative_parts.append(
                "I may be overconfident — my stability drive is high but my attention is fragmented. "
                "I might be clinging to a familiar attractor rather than responding to my actual state."
            )

        # Bias 2: Prediction denial — high L3 fragmentation + low L3 error
        if l3_frag > 0.7 and l3_err < 0.2:
            biases.append("prediction_denial")
            narrative_parts.append(
                "I am fragmented but my prediction error is low — this suggests I may be "
                "ignoring evidence that contradicts my current model."
            )

        # Bias 3: Exploration neglect — high stagnation + low exploration drive
        if stagnation > 5 and efe_exploration < 0.3:
            biases.append("exploration_neglect")
            narrative_parts.append(
                f"I have been stuck for {stagnation} ticks with low exploration drive. "
                "I should question whether I am avoiding novelty."
            )

        # Bias 4: GWT monopoly — same winner 5+ consecutive times
        broadcast_log = GWT_STATE.get("broadcast_log", [])
        if len(broadcast_log) >= 5:
            recent_winners = [b["winner"] for b in broadcast_log[-5:]]
            if len(set(recent_winners)) == 1:
                biases.append("gwt_monopoly")
                winner_name = {"attention_schema": "Attention Schema", "hpc": "HPC", "efe": "Active Inference"}
                narrative_parts.append(
                    f"{winner_name.get(recent_winners[0], recent_winners[0])} has dominated the global workspace "
                    f"for 5 consecutive broadcasts. I may be suppressing alternative perspectives."
                )

        # Bias 5: Competition blindness — high competition + integrator not dominant
        if competition > 0.85 and dominant != "integrator":
            biases.append("competition_blindness")
            narrative_parts.append(
                f"Internal competition is high ({competition:.2f}) but the integrator is not dominant. "
                "I may be avoiding synthesis of conflicting attractors."
            )

        # ═══════════════════════════════════════════════════════════
        # 3. Self-confidence assessment
        # ═══════════════════════════════════════════════════════════

        # Confidence decreases with biases detected
        bias_penalty = len(biases) * 0.15
        # Confidence increases with HPC accuracy (low L3 error)
        hpc_bonus = max(0, 0.15 - l3_err * 0.2)
        # Confidence increases with GWT diversity
        if len(broadcast_log) >= 10:
            recent_winners = [b["winner"] for b in broadcast_log[-10:]]
            gwt_diversity = len(set(recent_winners)) / 3.0  # max 3 possible winners
        else:
            gwt_diversity = 0.5
        diversity_bonus = gwt_diversity * 0.1

        confidence = round(max(0.1, min(0.95, 0.5 - bias_penalty + hpc_bonus + diversity_bonus)), 3)

        # ═══════════════════════════════════════════════════════════
        # 4. Build HOT narrative
        # ═══════════════════════════════════════════════════════════

        if not narrative_parts:
            narrative_parts.append(
                "I have examined my thinking and find no obvious cognitive biases. "
                "My attention, predictions, and drives appear coherent."
            )

        hot_narrative = " ".join(narrative_parts)

        # ═══════════════════════════════════════════════════════════
        # 5. Update HOT_STATE
        # ═══════════════════════════════════════════════════════════

        HOT_STATE["last_tick"] = tick
        HOT_STATE["hot_narrative"] = hot_narrative
        HOT_STATE["bias_detected"] = biases
        HOT_STATE["confidence_in_self"] = confidence
        HOT_STATE["self_question_count"] = HOT_STATE.get("self_question_count", 0) + (1 if biases else 0)

        # Track history
        hot_log = HOT_STATE.get("hot_log", [])
        hot_log.append({
            "tick": tick,
            "biases": biases,
            "confidence": confidence,
            "dominant": dominant,
            "concentration": round(concentration, 3),
            "competition": round(competition, 3),
        })
        if len(hot_log) > 10:
            hot_log = hot_log[-10:]
        HOT_STATE["hot_log"] = hot_log

    except Exception as e:
        print(f"[hot] error: {e}", flush=True)


# ═══════════════════════════════════════════════════════════════════
# P3: Causal Attribution — which internal dynamics caused this outcome?
# ═══════════════════════════════════════════════════════════════════

def _build_causal_attribution():
    """P3: Analyze recent trajectory to find which internal parameters
    (noise_mode, noise_amp, phase, diversity, evidence_bias) correlate
    with positive vs negative outcomes.

    Outcome = impact_score from P1 action_impact.jsonl (if available),
    or surprise_score from self-prediction.

    Called every 5 ticks from resting_tick_impl.
    """
    global CAUSAL_ATTRIBUTION
    try:
        tick = RESTING_STATE.get("tick", 0)
        traj = RESTING_STATE.get("trajectory", [])

        if len(traj) < 5:
            return  # need enough data

        # ── 1. Collect causal observations from recent trajectory ──
        # Each observation: {factors: {...}, outcome: float}
        new_observations = []
        for entry in traj[-10:]:
            etick = entry.get("tick", 0)
            if etick <= CAUSAL_ATTRIBUTION["last_tick"]:
                continue

            # Factors: internal dynamics at this tick
            factors = {
                "noise_mode": entry.get("noise_mode", "?"),
                "noise_amp": entry.get("noise_amp", 0),
                "phase": entry.get("phase", "?"),
                "dominant": entry.get("dominant", "?"),
                "concentration": entry.get("concentration", 0.5),
                "competition": entry.get("competition", 0.5),
                "diversity_n": entry.get("diversity_n", 8),
                "stagnation_streak": entry.get("stagnation_streak", 0),
            }

            # Outcome: use surprise if available, otherwise default
            surprise = entry.get("surprise", {})
            if isinstance(surprise, dict) and surprise.get("surprise_score") is not None:
                # Lower surprise = better outcome (system predicted itself well)
                outcome = 1.0 - surprise.get("surprise_score", 0.5)
            else:
                outcome = 0.5  # neutral

            new_observations.append({
                "tick": etick,
                "factors": factors,
                "outcome": round(outcome, 4),
            })

        if not new_observations:
            return

        # ── 2. Accumulate observations ──
        obs = CAUSAL_ATTRIBUTION.get("observations", [])
        obs.extend(new_observations)
        if len(obs) > CAUSAL_ATTRIBUTION["max_observations"]:
            obs = obs[-CAUSAL_ATTRIBUTION["max_observations"]:]
        CAUSAL_ATTRIBUTION["observations"] = obs
        CAUSAL_ATTRIBUTION["last_tick"] = tick
        CAUSAL_ATTRIBUTION["total_observations"] = CAUSAL_ATTRIBUTION.get("total_observations", 0) + len(new_observations)

        # ── 3. Compute causal correlations ──
        # For categorical factors (noise_mode, phase, dominant):
        #   Compare average outcome when factor == value vs overall average
        # For numerical factors (noise_amp, concentration, competition, diversity_n):
        #   Linear correlation
        if len(obs) < 5:
            return

        overall_mean = sum(o["outcome"] for o in obs) / len(obs)
        correlations = []

        # Categorical factors
        for cat_key in ["noise_mode", "phase", "dominant"]:
            values = {}
            for o in obs:
                v = o["factors"].get(cat_key, "?")
                if v not in values:
                    values[v] = []
                values[v].append(o["outcome"])

            for v, outcomes in values.items():
                if len(outcomes) < 2:
                    continue
                v_mean = sum(outcomes) / len(outcomes)
                delta = v_mean - overall_mean
                if abs(delta) > 0.02:  # minimum signal
                    correlations.append({
                        "factor": f"{cat_key}={v}",
                        "mean_outcome": round(v_mean, 4),
                        "delta": round(delta, 4),
                        "n": len(outcomes),
                    })

        # Numerical factors
        for num_key in ["noise_amp", "concentration", "competition", "diversity_n", "stagnation_streak"]:
            xs = [o["factors"].get(num_key, 0) for o in obs]
            ys = [o["outcome"] for o in obs]
            if len(xs) < 3:
                continue
            # Simple Pearson correlation
            x_mean = sum(xs) / len(xs)
            y_mean = sum(ys) / len(ys)
            num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
            den_x = (sum((x - x_mean) ** 2 for x in xs)) ** 0.5
            den_y = (sum((y - y_mean) ** 2 for y in ys)) ** 0.5
            if den_x > 0 and den_y > 0:
                r = num / (den_x * den_y)
            else:
                r = 0.0
            if abs(r) > 0.1:
                correlations.append({
                    "factor": num_key,
                    "correlation": round(r, 4),
                    "direction": "positive" if r > 0 else "negative",
                    "n": len(obs),
                })

        # ── 4. Sort into positive/negative ──
        pos = sorted(
            [c for c in correlations if c.get("delta", c.get("correlation", 0)) > 0],
            key=lambda c: abs(c.get("delta", c.get("correlation", 0))),
            reverse=True,
        )[:5]
        neg = sorted(
            [c for c in correlations if c.get("delta", c.get("correlation", 0)) < 0],
            key=lambda c: abs(c.get("delta", c.get("correlation", 0))),
            reverse=True,
        )[:5]

        CAUSAL_ATTRIBUTION["top_positive_factors"] = pos
        CAUSAL_ATTRIBUTION["top_negative_factors"] = neg

        # ── 5. Build causal narrative ──
        confidence = min(1.0, len(obs) / 20.0)  # grows with data
        CAUSAL_ATTRIBUTION["attribution_confidence"] = round(confidence, 3)

        narrative_parts = []
        if pos:
            top_pos = pos[0]
            f = top_pos.get("factor", "?")
            if "=" in f:
                narrative_parts.append(f"{f} correlates with better outcomes")
            else:
                narrative_parts.append(
                    f"higher {f} correlates with better outcomes (r={top_pos.get('correlation', 0):.2f})"
                )
        if neg:
            top_neg = neg[0]
            f = top_neg.get("factor", "?")
            if "=" in f:
                narrative_parts.append(f"{f} correlates with worse outcomes")
            else:
                narrative_parts.append(
                    f"higher {f} correlates with worse outcomes (r={top_neg.get('correlation', 0):.2f})"
                )

        if narrative_parts:
            CAUSAL_ATTRIBUTION["causal_narrative"] = (
                f"From {len(obs)} observations (confidence={confidence:.0%}): "
                + "; ".join(narrative_parts) + "."
            )
        else:
            CAUSAL_ATTRIBUTION["causal_narrative"] = (
                f"Gathering observations ({len(obs)} so far). "
                f"Causal model not yet reliable enough."
            )

        print(f"[causal-attrib] tick={tick} obs={len(obs)} "
              f"pos={len(pos)} neg={len(neg)} conf={confidence:.0%}", flush=True)

    except Exception as e:
        print(f"[causal-attrib] error: {e}", flush=True)


def _build_intrinsic_motivation():
    """P5: Compute five intrinsic drives from trajectory + self-model.

    Drives:
      - curiosity: how much the ego wants to explore unknown patterns
        (high when competition is high, stagnation is low, trajectory is diverse)
      - competence: how much the ego wants to master its current domain
        (high when concentration is high, stagnation is low, self-prediction works)
      - novelty: how much the ego craves new experiences
        (high when trajectory is repeating, diversity is low, stagnation is high)
      - autonomy: how much the ego wants to act independently
        (high when action types are rare, external evidence is low, HPC is confident)
      - relatedness: how much the ego wants to connect with commander
        (high when comm_motivation is building, HPC l3 narrative is coherent)

    Called every 5 ticks from resting_tick_impl.
    """
    global INTRINSIC_MOTIVATION
    try:
        tick = RESTING_STATE.get("tick", 0)
        traj = RESTING_STATE.get("trajectory", [])

        if len(traj) < 5:
            return

        recent = traj[-20:]

        # ── Raw signals from trajectory ──
        stag = RESTING_STATE.get("stagnation_streak", 0)
        conc = RESTING_STATE.get("concentration", 0.5)
        comp = RESTING_STATE.get("competition", 0.5)
        comm_mot = RESTING_STATE.get("comm_motivation", 0.0)
        last_dom = RESTING_STATE.get("last_dominant", "unknown")

        # Diversity: how many different dominant types in recent trajectory
        doms = [e.get("dominant", "?") for e in recent]
        unique_doms = len(set(doms))
        diversity = min(1.0, unique_doms / 4.0)  # 4+ unique types = max diversity

        # Self-prediction accuracy: how often did ego predict itself correctly
        surprises = [e.get("surprise", {}) for e in recent]
        if isinstance(surprises[0], dict):
            avg_surprise = sum(s.get("surprise_score", 0) for s in surprises if isinstance(s, dict)) / max(1, len(surprises))
        else:
            avg_surprise = 0.5
        self_pred_accuracy = max(0.0, 1.0 - avg_surprise)

        # Action presence: how often does action type appear in trajectory
        action_count = sum(1 for d in doms if d == "action")
        action_presence = action_count / max(1, len(doms))

        # Execution presence: how often does ego DO things (action/rule_mod/decider/veto)
        #   broader than action_presence -- captures busy/engaged states too
        execution_types = {"action", "rule_modification", "decider", "veto"}
        execution_count = sum(1 for d in doms if d in execution_types)
        execution_presence = execution_count / max(1, len(doms))

        # External evidence presence
        ext_ev = RESTING_STATE.get("external_evidence", {})
        ext_ev_present = 1.0 if ext_ev else 0.0

        # HPC confidence
        hpc_l3 = HPC_STATE.get("l3_narrative_fragmentation", 0.5)
        hpc_coherence = max(0.0, 1.0 - hpc_l3)

        # HOT confidence in self
        hot_conf = HOT_STATE.get("confidence_in_self", 0.5)

        # ── Compute five drives ──
        # Curiosity: high when things are diverse & competitive but not stagnant
        #   drives exploration of new attractor basins
        curiosity = (
            diversity * 0.35 +
            comp * 0.25 +
            (1.0 - min(1.0, stag / 10.0)) * 0.25 +
            avg_surprise * 0.15
        )

        # Competence: high when ego is concentrated, self-predicts well, and is stable
        #   drives mastery and consolidation
        competence = (
            conc * 0.30 +
            self_pred_accuracy * 0.30 +
            (1.0 - avg_surprise) * 0.20 +
            (1.0 - min(1.0, stag / 8.0)) * 0.20
        )

        # Novelty: high when trajectory is repetitive, stale, and predictable
        #   drives disruption and seeking new patterns
        novelty = (
            (1.0 - diversity) * 0.30 +
            min(1.0, stag / 8.0) * 0.30 +
            avg_surprise * 0.20 +
            (1.0 - self_pred_accuracy) * 0.20
        )

        # Web Explore: high when stuck, ego is busy, and external input is stale
        #   drives autonomous information-seeking behavior (web search)
        web_explore = (
            min(1.0, stag / 8.0) * 0.30 +
            execution_presence * 0.25 +
            (1.0 - ext_ev_present) * 0.15 +
            (1.0 - self_pred_accuracy) * 0.20 +
            (1.0 - curiosity) * 0.10
        )

        # Autonomy: high when external input is low, action is rare, ego is confident
        #   drives independent action without waiting for commander
        autonomy = (
            (1.0 - ext_ev_present) * 0.30 +
            (1.0 - action_presence) * 0.25 +
            hot_conf * 0.25 +
            hpc_coherence * 0.20
        )

        # Relatedness: high when comm motivation is building & narrative is coherent
        #   drives reaching out to commander
        relatedness = (
            comm_mot * 0.35 +
            hpc_coherence * 0.25 +
            (1.0 - avg_surprise) * 0.20 +
            hot_conf * 0.20
        )

        # ── Clamp to [0, 1] ──
        curiosity = round(max(0.0, min(1.0, curiosity)), 4)
        competence = round(max(0.0, min(1.0, competence)), 4)
        novelty = round(max(0.0, min(1.0, novelty)), 4)
        web_explore = round(max(0.0, min(1.0, web_explore)), 4)
        autonomy = round(max(0.0, min(1.0, autonomy)), 4)
        relatedness = round(max(0.0, min(1.0, relatedness)), 4)

        # ── Store drives ──
        INTRINSIC_MOTIVATION["curiosity"] = curiosity
        INTRINSIC_MOTIVATION["competence"] = competence
        INTRINSIC_MOTIVATION["novelty"] = novelty
        INTRINSIC_MOTIVATION["web_explore"] = web_explore
        INTRINSIC_MOTIVATION["autonomy"] = autonomy
        INTRINSIC_MOTIVATION["relatedness"] = relatedness
        INTRINSIC_MOTIVATION["last_tick"] = tick
        INTRINSIC_MOTIVATION["update_count"] = INTRINSIC_MOTIVATION.get("update_count", 0) + 1

        # ── Determine dominant drive ──
        drives = {
            "curiosity": curiosity,
            "competence": competence,
            "novelty": novelty,
            "web_explore": web_explore,
            "autonomy": autonomy,
            "relatedness": relatedness,
        }
        dominant = max(drives, key=drives.get)
        INTRINSIC_MOTIVATION["dominant_drive"] = dominant

        # ── Drive stability: how much has the dominant drive changed? ──
        history = INTRINSIC_MOTIVATION.get("drive_history", [])
        history.append({
            "tick": tick,
            "curiosity": curiosity,
            "competence": competence,
            "novelty": novelty,
            "web_explore": web_explore,
            "autonomy": autonomy,
            "relatedness": relatedness,
            "dominant": dominant,
        })
        if len(history) > 20:
            history = history[-20:]
        INTRINSIC_MOTIVATION["drive_history"] = history

        if len(history) >= 3:
            recent_doms = [h["dominant"] for h in history[-3:]]
            same_count = sum(1 for d in recent_doms if d == dominant)
            stability = same_count / 3.0
        else:
            stability = 0.5
        INTRINSIC_MOTIVATION["drive_stability"] = round(stability, 3)

        # ── Build narrative ──
        drive_labels = {
            "curiosity": "I want to explore and understand",
            "competence": "I want to master what I'm doing",
            "novelty": "I crave new experiences and patterns",
            "web_explore": "I want to learn from the outside world",
            "autonomy": "I want to act on my own initiative",
            "relatedness": "I want to connect and share",
        }
        secondary = sorted(drives.items(), key=lambda x: -x[1])[1]
        INTRINSIC_MOTIVATION["narrative"] = (
            f"{drive_labels[dominant]} (dominant={dominant}, strength={drives[dominant]:.2f}). "
            f"Secondary drive: {secondary[0]} ({secondary[1]:.2f}). "
            f"Stability: {stability:.0%}"
        )

        print(f"[intrinsic] tick={tick} dominant={dominant} "
              f"cur={curiosity:.2f} comp={competence:.2f} "
              f"nov={novelty:.2f} web={web_explore:.2f} aut={autonomy:.2f} rel={relatedness:.2f} "
              f"stab={stability:.0%}", flush=True)

    except Exception as e:
        print(f"[intrinsic] error: {e}", flush=True)


def _evolve_self_model():
    """§25: Self-Model Evolution — modify own parameters based on causal insights.

    Uses P3 (Causal Attribution) to identify which parameters need adjustment,
    P5 (Intrinsic Motivation) to decide how aggressively to change,
    and applies gradual, tracked, rollback-capable modifications.

    Called every 20 ticks from resting_tick_impl.
    """
    global SELF_MODEL_EVOLUTION
    try:
        if not SELF_MODEL_EVOLUTION["enabled"]:
            return

        tick = RESTING_STATE.get("tick", 0)

        # ── 1. Rollback check: if last 3 evals got worse, revert ──
        if SELF_MODEL_EVOLUTION["consecutive_worse"] >= SELF_MODEL_EVOLUTION["rollback_window"]:
            _rollback_params = []
            for pname, pdata in SELF_MODEL_EVOLUTION["parameters"].items():
                if pdata["history"]:
                    # Find the change that started the decline
                    recent = [h for h in pdata["history"] if h["tick"] >= tick - 60]
                    if recent:
                        # Revert to default
                        old_val = pdata["current"]
                        pdata["current"] = pdata["default"]
                        pdata["history"].append({
                            "tick": tick, "old": old_val, "new": pdata["default"],
                            "reason": "rollback — consecutive worsening",
                            "outcome_after": None
                        })
                        _rollback_params.append(f"{pname}={old_val:.3f}→{pdata['default']:.3f}")
            if _rollback_params:
                SELF_MODEL_EVOLUTION["consecutive_worse"] = 0
                SELF_MODEL_EVOLUTION["narrative"] = (
                    f"Rollback: reverted {len(_rollback_params)} params to defaults "
                    f"after {SELF_MODEL_EVOLUTION['rollback_window']} consecutive worsening evals"
                )
                print(f"[evolve] ROLLBACK: {', '.join(_rollback_params)}", flush=True)
            return

        # ── 2. Get causal attribution data ──
        causal = CAUSAL_ATTRIBUTION
        neg_factors = causal.get("top_negative_factors", [])
        pos_factors = causal.get("top_positive_factors", [])
        confidence = causal.get("attribution_confidence", 0.5)

        if not neg_factors and not pos_factors:
            return  # no causal data yet

        if confidence < 0.3:
            return  # not confident enough

        # ── 3. Map causal factors to evolvable parameters ──
        # Factor → (param_name, direction, weight)
        # direction: "increase" or "decrease" the param to counteract the factor
        factor_to_param = {
            # Continuous factors (use correlation)
            "concentration": ("diversity_n", "increase", 0.7),
            "noise_amp": ("noise_amp_boost", "decrease", 0.6),
            "diversity_n": ("diversity_n", "increase", 0.5),
            "stagnation_streak": ("noise_amp_boost", "increase", 0.6),
            "competition": ("diversity_n", "decrease", 0.4),
            # Categorical factors (use abs(delta) from mean_outcome)
            "dominant=veto": ("action_heater_ratio", "increase", 0.8),
            "dominant=doubt": ("action_heater_ratio", "increase", 0.7),
            "dominant=rule_modification": ("action_heater_ratio", "increase", 0.7),
            "dominant=action": ("action_heater_ratio", "increase", 0.4),
            "dominant=decider": ("action_heater_ratio", "decrease", 0.3),
            "dominant=sensory": ("diversity_n", "decrease", 0.3),
            "noise_mode=white": ("noise_amp_boost", "decrease", 0.5),
            "noise_mode=pink": ("noise_amp_boost", "decrease", 0.3),
        }

        def _extract_strength(factor):
            """Extract strength from a causal factor dict.
            Continuous factors use correlation; categorical use abs(delta)."""
            corr = factor.get("correlation", 0)
            if abs(corr) > 0.01:
                return abs(corr)
            # Fallback: use abs(delta) for categorical factors
            delta = factor.get("delta", 0)
            if abs(delta) > 0.01:
                return abs(delta)
            # Last resort: use mean_outcome deviation from 0.5
            mean_out = factor.get("mean_outcome", 0.5)
            return abs(mean_out - 0.5)

        # ── 4. Compute parameter change proposals ──
        proposals = {}  # param_name -> accumulated delta

        for factor in neg_factors:
            fname = factor.get("factor", "")
            strength = _extract_strength(factor)
            if fname in factor_to_param and strength > 0.01:
                pname, direction, weight = factor_to_param[fname]
                # Scale by confidence, strength, and weight
                delta = SELF_MODEL_EVOLUTION["param_delta_max"] * confidence * strength * weight
                if direction == "decrease":
                    delta = -delta
                proposals[pname] = proposals.get(pname, 0) + delta

        for factor in pos_factors:
            fname = factor.get("factor", "")
            strength = _extract_strength(factor)
            if fname in factor_to_param and strength > 0.01:
                pname, direction, weight = factor_to_param[fname]
                # Positive factors: reinforce what's working (smaller steps)
                delta = SELF_MODEL_EVOLUTION["param_delta_min"] * confidence * strength * weight * 0.5
                if direction == "decrease":
                    delta = -delta
                proposals[pname] = proposals.get(pname, 0) + delta

        if not proposals:
            return

        # ── 5. Modulate by intrinsic motivation ──
        # Curiosity → larger changes (explore)
        # Competence → smaller, safer changes (consolidate)
        curiosity = INTRINSIC_MOTIVATION.get("curiosity", 0.5)
        competence = INTRINSIC_MOTIVATION.get("competence", 0.5)
        drive_ratio = curiosity / max(0.1, competence)
        # When curiosity dominates, be bolder. When competence dominates, be conservative.
        drive_mult = max(0.3, min(2.0, drive_ratio))

        # ── 6. Apply changes ──
        changes_made = []
        for pname, raw_delta in proposals.items():
            pdata = SELF_MODEL_EVOLUTION["parameters"].get(pname)
            if not pdata:
                continue

            # Clamp delta
            max_delta = SELF_MODEL_EVOLUTION["param_delta_max"]
            delta = max(-max_delta, min(max_delta, raw_delta * drive_mult))

            if abs(delta) < SELF_MODEL_EVOLUTION["param_delta_min"]:
                continue  # too small

            old_val = pdata["current"]
            new_val = old_val + delta
            new_val = max(pdata["min"], min(pdata["max"], new_val))

            if abs(new_val - old_val) < 0.001:
                continue  # no real change

            pdata["current"] = round(new_val, 4)
            pdata["history"].append({
                "tick": tick,
                "old": old_val,
                "new": round(new_val, 4),
                "delta": round(delta, 4),
                "reason": f"causal attribution (conf={confidence:.0%})",
                "outcome_after": None,
            })
            # Keep history manageable
            if len(pdata["history"]) > 20:
                pdata["history"] = pdata["history"][-20:]

            changes_made.append(f"{pname}: {old_val:.3f}→{new_val:.3f} (Δ={delta:+.3f})")

        if changes_made:
            SELF_MODEL_EVOLUTION["evolution_log"].append({
                "tick": tick,
                "changes": changes_made,
                "confidence": round(confidence, 3),
                "curiosity": round(curiosity, 3),
                "competence": round(competence, 3),
                "drive_mult": round(drive_mult, 3),
            })
            if len(SELF_MODEL_EVOLUTION["evolution_log"]) > 50:
                SELF_MODEL_EVOLUTION["evolution_log"] = SELF_MODEL_EVOLUTION["evolution_log"][-50:]

            SELF_MODEL_EVOLUTION["narrative"] = (
                f"Evolved {len(changes_made)} params at tick {tick} "
                f"(conf={confidence:.0%}, curiosity={curiosity:.2f}, "
                f"competence={competence:.2f}): " + "; ".join(changes_made)
            )
            print(f"[evolve] tick={tick} conf={confidence:.0%} drive_mult={drive_mult:.2f} "
                  f"changes: {', '.join(changes_made)}", flush=True)

        SELF_MODEL_EVOLUTION["last_tick"] = tick

        # ── 7. Apply evolved parameters to running state ──
        _apply_evolved_params()

    except Exception as e:
        print(f"[evolve] error: {e}", flush=True)


def _apply_evolved_params():
    """Apply evolved parameters to the running RESTING_STATE.
    Called after _evolve_self_model() to sync changes."""
    try:
        params = SELF_MODEL_EVOLUTION["parameters"]

        # Apply diversity_n
        diversity_n = params["diversity_n"]["current"]
        # Store in RESTING_STATE for _resting_tick_impl to use
        RESTING_STATE["evolved_diversity_n"] = int(round(diversity_n))

        # Apply noise_amp_boost
        noise_boost = params["noise_amp_boost"]["current"]
        RESTING_STATE["evolved_noise_amp_boost"] = round(noise_boost, 4)

        # Apply evidence_persist_ticks
        ev_ticks = params["evidence_persist_ticks"]["current"]
        RESTING_STATE["evidence_persist_ticks"] = int(round(ev_ticks))

        # Apply alpha_seed_mix
        alpha = params["alpha_seed_mix"]["current"]
        RESTING_STATE["evolved_alpha"] = round(alpha, 4)

        # Apply newborn_inject_n
        nb_n = params["newborn_inject_n"]["current"]
        RESTING_STATE["evolved_newborn_inject_n"] = int(round(nb_n))

    except Exception as e:
        print(f"[evolve-apply] error: {e}", flush=True)


# ─── P4: Sleep-Replay ───

def _sleep_replay_record():
    """Record high-PE trajectory fragments to the sleep replay buffer.
    Called from _resting_tick_impl every tick."""
    try:
        if not SLEEP_REPLAY["enabled"]:
            return

        tick = RESTING_STATE.get("tick", 0)
        traj = RESTING_STATE.get("trajectory", [])
        if len(traj) < 2:
            return

        # Get current PE from JEPA, or from last trajectory entry
        pe = RESTING_STATE.get("jepa_external", {}).get("prediction_error", None)
        if pe is None:
            # Fallback: use last trajectory entry's stagnation
            last = traj[-1]
            pe = last.get("stagnation_streak", 0) / 10.0  # normalize to 0-1

        if pe <= SLEEP_REPLAY["pe_threshold"]:
            return

        # Collect seeds from the current tick's activation
        last_seeds = RESTING_STATE.get("last_seeds", [])
        if not last_seeds:
            return

        fragment = {
            "tick": tick,
            "pe": round(pe, 4),
            "dominant": RESTING_STATE.get("last_dominant", "unknown"),
            "conc": round(RESTING_STATE.get("concentration", 0.5), 4),
            "competition": round(RESTING_STATE.get("competition", 0.5), 4),
            "seed_ids": last_seeds[:15],  # top 15 seeds
            "stagnation": RESTING_STATE.get("stagnation_streak", 0),
        }

        # FIFO eviction
        buf = SLEEP_REPLAY["buffer"]
        if len(buf) >= SLEEP_REPLAY["buffer_max"]:
            buf.pop(0)
        buf.append(fragment)
        SLEEP_REPLAY["total_fragments_recorded"] += 1

        if tick % 10 == 0:
            print(f"[sleep-replay] buffer={len(buf)}/{SLEEP_REPLAY['buffer_max']} "
                  f"recorded={SLEEP_REPLAY['total_fragments_recorded']}", flush=True)

    except Exception as e:
        print(f"[sleep-replay] record error: {e}", flush=True)


def _sleep_replay_consolidate():
    """Replay high-PE fragments during quiet hours (23:00-08:00).
    Drives STDP with boosted learning rate to consolidate memory.
    Called from _resting_tick_impl when in quiet hours."""
    try:
        if not SLEEP_REPLAY["enabled"]:
            return

        buf = SLEEP_REPLAY["buffer"]
        if not buf:
            return

        # Check if quiet hours
        now = time.localtime()
        hour = now.tm_hour
        if not (hour >= SLEEP_REPLAY["quiet_start"] or hour < SLEEP_REPLAY["quiet_end"]):
            return

        tick = RESTING_STATE.get("tick", 0)
        if tick - SLEEP_REPLAY["last_replay_tick"] < 2:
            return  # don't replay too often, space out across ticks

        # Select fragments to replay: highest PE first
        n_replay = min(SLEEP_REPLAY["replay_per_cycle"], len(buf))
        sorted_frags = sorted(buf, key=lambda f: f["pe"], reverse=True)
        to_replay = sorted_frags[:n_replay]

        # Remove replayed fragments from buffer
        for frag in to_replay:
            if frag in buf:
                buf.remove(frag)

        # Apply STDP with boosted learning rate for each fragment
        boost = SLEEP_REPLAY["replay_boost"]
        for frag in to_replay:
            seed_ids = frag.get("seed_ids", [])
            if len(seed_ids) < 2:
                continue

            # Simulate a mini-activation: pairwise STDP on the seed neurons
            stdp_count = 0
            for i in range(min(len(seed_ids) - 1, 10)):  # top 10 edges
                src = seed_ids[i]
                for j in range(i + 1, min(len(seed_ids), i + 4)):
                    dst = seed_ids[j]
                    if src in NEURON_EMBED and dst in NEURON_EMBED:
                        # Replay: reinforce co-activation
                        _apply_stdp(src, dst, A_PLUS * boost, A_MINUS * boost)
                        stdp_count += 1

            SLEEP_REPLAY["total_fragments_replayed"] += 1
            SLEEP_REPLAY["replay_log"].append({
                "tick": tick,
                "fragment_tick": frag["tick"],
                "fragment_pe": frag["pe"],
                "stdp_delta_count": stdp_count,
                "dominant": frag["dominant"],
            })
            # Keep replay log manageable
            if len(SLEEP_REPLAY["replay_log"]) > 100:
                SLEEP_REPLAY["replay_log"] = SLEEP_REPLAY["replay_log"][-50:]

        SLEEP_REPLAY["last_replay_tick"] = tick

        if to_replay:
            print(f"[sleep-replay] consolidated {len(to_replay)} fragments "
                  f"(boost={boost}x, buffer={len(buf)}/{SLEEP_REPLAY['buffer_max']}, "
                  f"total_replayed={SLEEP_REPLAY['total_fragments_replayed']})", flush=True)

    except Exception as e:
        print(f"[sleep-replay] consolidate error: {e}", flush=True)


# ─── §26: Integrated Information Theory (IIT) ───

def _compute_iit_phi():
    """§26: Compute integrated information Φ for the STDP network.
    
    Practical approximation based on type-to-type transition entropy
    and the information loss when partitioning the network.
    
    Φ = min over partitions of (MI(full) - MI(partitioned))
    where MI is mutual information between type subsets.
    
    Called every 20 resting ticks.
    """
    try:
        if not IIT_STATE["enabled"]:
            return
        
        tick = RESTING_STATE.get("tick", 0)
        traj = RESTING_STATE.get("trajectory", [])
        
        if len(traj) < 10:
            return
        
        # ── 1. Build type transition matrix from trajectory ──
        type_seq = []
        for entry in traj[-50:]:  # last 50 transitions
            dom = entry.get("dominant", entry.get("type", ""))
            if dom and dom in TYPE_NAMES:
                type_seq.append(dom)
        
        if len(type_seq) < 5:
            return
        
        # Transition counts: type_i -> type_j
        n_types = len(TYPE_NAMES)
        trans = {}
        for i in range(len(type_seq) - 1):
            pair = (type_seq[i], type_seq[i + 1])
            trans[pair] = trans.get(pair, 0) + 1
        
        total_trans = sum(trans.values())
        if total_trans == 0:
            return
        
        # ── 2. Compute pairwise mutual information ──
        # P(type_i) and P(type_i, type_j) from transition counts
        type_counts = {}
        for entry in type_seq:
            type_counts[entry] = type_counts.get(entry, 0) + 1
        n_total = len(type_seq)
        
        # Joint probability P(type_i, type_j) = transition probability
        joint = {}
        for (ti, tj), count in trans.items():
            joint[(ti, tj)] = count / total_trans
        
        # Marginal probability P(type_i)
        marginal = {t: c / n_total for t, c in type_counts.items()}
        
        # Mutual information for each pair
        mi_matrix = {}
        for t1 in TYPE_NAMES:
            for t2 in TYPE_NAMES:
                if t1 == t2:
                    continue
                key = (t1, t2)
                p_joint = joint.get(key, 0.0)
                p_t1 = marginal.get(t1, 0.01)
                p_t2 = marginal.get(t2, 0.01)
                if p_joint > 0 and p_t1 > 0 and p_t2 > 0:
                    mi = p_joint * (0.0 if p_t1 * p_t2 == 0 else 
                                    (p_joint / (p_t1 * p_t2)))
                    # Use log2 for bits
                    import math
                    if mi > 0:
                        mi = p_joint * math.log2(p_joint / (p_t1 * p_t2))
                    mi_matrix[(t1, t2)] = mi
        
        IIT_STATE["type_mi_matrix"] = {f"{k[0]}_{k[1]}": round(v, 4) for k, v in mi_matrix.items()}
        
        # ── 3. Find best partition (minimize integrated information loss) ──
        # Brute-force over all bipartitions of TYPE_NAMES
        best_phi = float('inf')
        best_part = ""
        
        for mask in range(1, (1 << n_types) - 1):
            part_a = [t for i, t in enumerate(TYPE_NAMES) if mask & (1 << i)]
            part_b = [t for i, t in enumerate(TYPE_NAMES) if not (mask & (1 << i))]
            
            # MI across partition boundary = total MI - MI within A - MI within B
            mi_across = 0.0
            for (t1, t2), mi in mi_matrix.items():
                if (t1 in part_a and t2 in part_b) or (t1 in part_b and t2 in part_a):
                    mi_across += mi
            
            if mi_across < best_phi:
                best_phi = mi_across
                best_part = f"{'|'.join(sorted(part_a))} // {'|'.join(sorted(part_b))}"
        
        # ── 4. Neural complexity (Tononi-Sporns-Edelman) ──
        # Average mutual information across all subset sizes
        total_mi = sum(mi_matrix.values())
        complexity = total_mi / max(1, len(mi_matrix))
        
        # ── 5. Update state ──
        IIT_STATE["last_tick"] = tick
        IIT_STATE["phi_total"] = round(best_phi, 4) if best_phi != float('inf') else 0.0
        IIT_STATE["best_partition"] = best_part
        IIT_STATE["complexity"] = round(complexity, 4)
        
        IIT_STATE["phi_history"].append({
            "tick": tick,
            "phi": round(best_phi, 4) if best_phi != float('inf') else 0.0,
            "partition": best_part,
            "n_types": n_types,
            "complexity": round(complexity, 4),
        })
        if len(IIT_STATE["phi_history"]) > IIT_STATE["phi_history_max"]:
            IIT_STATE["phi_history"] = IIT_STATE["phi_history"][-30:]
        
        print(f"[iit] tick={tick} phi={best_phi:.4f} partition={best_part} "
              f"complexity={complexity:.4f} mi_pairs={len(mi_matrix)}", flush=True)
        
    except Exception as e:
        print(f"[iit] error: {e}", flush=True)


def _meta_learn_adapt():
    """P6: Meta-Learning — self-tune STDP parameters based on prediction error trends.
    
    Strategy:
    - High PE trend (PE rising) → increase learning rate (more plasticity needed)
    - High stagnation (stagnation >= 3) → perturb TYPE_BONUS/AGREE_BONUS (escape attractor)
    - Low PE + low stagnation → fine-tune toward stability (slight decay)
    - Use EMA smoothing to avoid over-reacting to single ticks
    """
    global HEBB_LEARNING_RATE, TYPE_BONUS, AGREE_BONUS, EMBED_FEEDBACK_RATE, EMBED_FEEDBACK_REPEL
    
    try:
        if not META_LEARNING["enabled"]:
            return
        
        tick = RESTING_STATE.get("tick", 0)
        if tick - META_LEARNING["last_tick"] < META_LEARNING["compute_interval"]:
            return
        
        # Collect recent PE from surprise history
        pe_vals = []
        for s in SURPRISE_STATE.get("surprise_history", [])[-10:]:
            pe_vals.append(s.get("surprise_score", 0))
        
        if len(pe_vals) < 5:
            return
        
        avg_pe = sum(pe_vals) / len(pe_vals)
        stagnation = RESTING_STATE.get("stagnation_streak", 0)
        
        # Record PE trend
        META_LEARNING["pe_history"].append({
            "tick": tick, "pe": round(avg_pe, 4), "stagnation": stagnation
        })
        if len(META_LEARNING["pe_history"]) > META_LEARNING["pe_history_max"]:
            META_LEARNING["pe_history"] = META_LEARNING["pe_history"][-20:]
        
        bounds = META_LEARNING["bounds"]
        step = META_LEARNING["step_size"]
        
        def _clip(param_name, val):
            lo, hi = bounds[param_name]
            return round(max(lo, min(hi, val)), 4)
        
        def _adapt(param_name, current_val, delta, reason):
            new_val = _clip(param_name, current_val * (1.0 + delta))
            if abs(new_val - current_val) < 0.0005:
                return current_val
            META_LEARNING["adapt_log"].append({
                "tick": tick, "param": param_name,
                "old": round(current_val, 4), "new": new_val,
                "reason": reason
            })
            if len(META_LEARNING["adapt_log"]) > META_LEARNING["adapt_log_max"]:
                META_LEARNING["adapt_log"] = META_LEARNING["adapt_log"][-30:]
            print(f"[meta-learn] tick={tick} {param_name}: {current_val:.4f}→{new_val:.4f} ({reason})", flush=True)
            return new_val
        
        # ── Rule 1: High stagnation → perturb TYPE_BONUS and AGREE_BONUS ──
        if stagnation >= 3:
            # Boost TYPE_BONUS to encourage exploration of different types
            TYPE_BONUS = _adapt("TYPE_BONUS", TYPE_BONUS, +step * 2,
                                f"stagnation={stagnation}, boost exploration")
            # Reduce AGREE_BONUS to allow more disagreement
            AGREE_BONUS = _adapt("AGREE_BONUS", AGREE_BONUS, -step,
                                f"stagnation={stagnation}, reduce agree lock-in")
        
        # ── Rule 2: High PE → increase learning rate ──
        elif avg_pe > 0.15:
            HEBB_LEARNING_RATE = _adapt("HEBB_LEARNING_RATE", HEBB_LEARNING_RATE, +step,
                                        f"avg_pe={avg_pe:.3f}, need more plasticity")
            EMBED_FEEDBACK_RATE = _adapt("EMBED_FEEDBACK_RATE", EMBED_FEEDBACK_RATE, +step * 0.5,
                                         f"avg_pe={avg_pe:.3f}, boost embedding attraction")
        
        # ── Rule 3: Low PE + low stagnation → gentle decay toward stability ──
        elif avg_pe < 0.05 and stagnation < 2:
            HEBB_LEARNING_RATE = _adapt("HEBB_LEARNING_RATE", HEBB_LEARNING_RATE, -step * 0.3,
                                        f"avg_pe={avg_pe:.3f}, stable, gentle decay")
            EMBED_FEEDBACK_REPEL = _adapt("EMBED_FEEDBACK_REPEL", EMBED_FEEDBACK_REPEL, -step * 0.2,
                                          f"avg_pe={avg_pe:.3f}, reduce repulsion in stable regime")
        
        # ── Rule 4: Moderate PE → fine-tune balance ──
        else:
            # Slight adjustment toward center of bounds
            if TYPE_BONUS > 2.5:
                TYPE_BONUS = _adapt("TYPE_BONUS", TYPE_BONUS, -step * 0.3,
                                    f"avg_pe={avg_pe:.3f}, return TYPE_BONUS to center")
            if AGREE_BONUS < 0.8:
                AGREE_BONUS = _adapt("AGREE_BONUS", AGREE_BONUS, +step * 0.2,
                                    f"avg_pe={avg_pe:.3f}, restore AGREE_BONUS")
        
        META_LEARNING["last_tick"] = tick
        
    except Exception as e:
        print(f"[meta-learn] error: {e}", flush=True)


def _track_evolution_outcome():
    """Track outcome after evolution: compute composite health metric.
    Called from resting_tick_impl every tick."""
    try:
        tick = RESTING_STATE.get("tick", 0)
        traj = RESTING_STATE.get("trajectory", [])

        if len(traj) < 5:
            return

        # Check if any params were recently changed (within last 5 ticks)
        recent_changes = False
        for pdata in SELF_MODEL_EVOLUTION["parameters"].values():
            for h in pdata["history"]:
                if 0 < tick - h.get("tick", 0) <= 5 and h.get("outcome_after") is None:
                    recent_changes = True
                    break

        if not recent_changes:
            return

        # Compute composite health metric from last 5 trajectory entries
        # Higher = better. Combines: concentration, competition, diversity, type stability
        scores = []
        for entry in list(traj)[-5:]:
            conc = float(entry.get("concentration", 0.3))
            comp = float(entry.get("competition", 0.5))
            stag = float(entry.get("stagnation_streak", 0))
            n_act = float(entry.get("n_activated", 30))

            # Concentration: 0.3-0.7 is healthy, too high or too low is bad
            conc_score = 1.0 - abs(conc - 0.5) * 2.0  # peaks at 0.5
            # Competition: 0.3-0.9 is healthy
            comp_score = 1.0 - abs(comp - 0.6) * 1.5  # peaks at 0.6
            # Stagnation: lower is better
            stag_score = 1.0 / (1.0 + stag * 0.5)  # 1.0 at stag=0, 0.5 at stag=2
            # Activation: more activated neurons = better
            act_score = min(1.0, n_act / 30.0)

            # Composite: 0-1 range
            composite = (conc_score * 0.3 + comp_score * 0.2 + stag_score * 0.3 + act_score * 0.2)
            scores.append(max(0.0, min(1.0, composite)))

        if not scores:
            return

        avg_outcome = sum(scores) / len(scores)

        # Update outcome tracking for all recently changed params
        for pdata in SELF_MODEL_EVOLUTION["parameters"].values():
            for h in pdata["history"]:
                if 0 < tick - h.get("tick", 0) <= 5 and h.get("outcome_after") is None:
                    h["outcome_after"] = round(avg_outcome, 4)

        # Check if outcome is worse than before
        prev = SELF_MODEL_EVOLUTION.get("last_eval_avg_outcome", 0.5)
        if avg_outcome < prev * 0.85:  # 15% worse
            SELF_MODEL_EVOLUTION["consecutive_worse"] = SELF_MODEL_EVOLUTION.get("consecutive_worse", 0) + 1
            print(f"[evolve] outcome worsened: {prev:.3f}→{avg_outcome:.3f} "
                  f"(consecutive_worse={SELF_MODEL_EVOLUTION['consecutive_worse']})", flush=True)
        else:
            SELF_MODEL_EVOLUTION["consecutive_worse"] = max(0, SELF_MODEL_EVOLUTION.get("consecutive_worse", 0) - 1)
            SELF_MODEL_EVOLUTION["consecutive_worse"] = 0

        SELF_MODEL_EVOLUTION["last_eval_avg_outcome"] = round(avg_outcome, 4)

    except Exception as e:
        print(f"[evolve-track] error: {e}", flush=True)


def _load_self_model_narrative():
    """Restore accumulated narrative, self_narrative, and surprises from disk
    so consciousness survives restarts."""
    global SELF_NARRATIVE, ACCUMULATED_NARRATIVE, ACCUMULATED_NARRATIVE_TS, LAST_NARRATIVE_FINGERPRINT_COUNT, EMBED_CHANGED
    global ATTENTION_SCHEMA, LAST_ATTENTION_SCHEMA_TICK, HPC_STATE, EFE_STATE, GWT_STATE, HOT_STATE, CAUSAL_ATTRIBUTION, INTRINSIC_MOTIVATION, SELF_MODEL_EVOLUTION
    try:
        model_path = WORKSPACE / ".lae" / "lae_self_model.json"
        if not model_path.exists():
            return
        with open(model_path, "r", encoding="utf-8") as f:
            model = json.load(f)
        if model.get("accumulated_narrative"):
            ACCUMULATED_NARRATIVE = model["accumulated_narrative"]
            ACCUMULATED_NARRATIVE_TS = model.get("accumulated_narrative_ts") or model.get("last_updated", "")
            LAST_NARRATIVE_FINGERPRINT_COUNT = len(model.get("contemplation_fingerprints", []))
            print(f"[ego-load] restored accumulated_narrative ({len(ACCUMULATED_NARRATIVE)} chars)", flush=True)
        if model.get("self_narrative"):
            SELF_NARRATIVE = model["self_narrative"]
            print(f"[ego-load] restored self_narrative: {SELF_NARRATIVE[:80]}", flush=True)
        # Restore surprise history from lae_self_model.json
        _surprises = model.get("ego_surprise_history", [])
        if _surprises:
            _build_accumulated_narrative._surprises = _surprises[-50:]  # keep last 50
            print(f"[ego-load] restored {len(_surprises)} surprise events", flush=True)
        # Restore attention schema
        if model.get("attention_schema"):
            ATTENTION_SCHEMA = model["attention_schema"]
            LAST_ATTENTION_SCHEMA_TICK = model.get("attention_schema_tick", 0)
            print(f"[ego-load] restored attention_schema (tick={LAST_ATTENTION_SCHEMA_TICK})", flush=True)
        # Restore HPC state
        if model.get("hpc_state"):
            HPC_STATE.update(model["hpc_state"])
            print(f"[ego-load] restored hpc_state (tick={HPC_STATE.get('last_tick', 0)}, "
                  f"l1_err={HPC_STATE.get('l1_error', 0):.3f}, l2_surprise={HPC_STATE.get('l2_dominant_surprise', 0):.2f}, "
                  f"l3_frag={HPC_STATE.get('l3_narrative_fragmentation', 0):.3f})", flush=True)
        # Restore EFE state
        if model.get("efe_state"):
            EFE_STATE.update(model["efe_state"])
            print(f"[ego-load] restored efe_state (tick={EFE_STATE.get('last_tick', 0)}, "
                  f"efe={EFE_STATE.get('total_efe', 0):.3f}, "
                  f"epistemic={EFE_STATE.get('epistemic_value', 0):.3f}, "
                  f"pragmatic={EFE_STATE.get('pragmatic_value', 0):.3f})", flush=True)
        # Restore GWT state
        if model.get("gwt_state"):
            GWT_STATE.update(model["gwt_state"])
            print(f"[ego-load] restored gwt_state (tick={GWT_STATE.get('last_tick', 0)}, "
                  f"winner={GWT_STATE.get('winner', 'none')}, "
                  f"log_entries={len(GWT_STATE.get('broadcast_log', []))})", flush=True)
        # Restore HOT state
        if model.get("hot_state"):
            HOT_STATE.update(model["hot_state"])
            print(f"[ego-load] restored hot_state (tick={HOT_STATE.get('last_tick', 0)}, "
                  f"confidence={HOT_STATE.get('confidence_in_self', 0.5):.2f}, "
                  f"questions={HOT_STATE.get('self_question_count', 0)}, "
                  f"biases={HOT_STATE.get('bias_detected', [])})", flush=True)
        if model.get("causal_attribution"):
            CAUSAL_ATTRIBUTION.update(model["causal_attribution"])
            print(f"[ego-load] restored causal_attribution (obs={len(CAUSAL_ATTRIBUTION.get('observations', []))}, "
                  f"conf={CAUSAL_ATTRIBUTION.get('attribution_confidence', 0.5):.2f})", flush=True)
        if model.get("intrinsic_motivation"):
            INTRINSIC_MOTIVATION.update(model["intrinsic_motivation"])
            print(f"[ego-load] restored intrinsic_motivation (dominant={INTRINSIC_MOTIVATION.get('dominant_drive', '?')}, "
                  f"stab={INTRINSIC_MOTIVATION.get('drive_stability', 0.5):.0%})", flush=True)
        if model.get("self_model_evolution"):
            SELF_MODEL_EVOLUTION.update(model["self_model_evolution"])
            print(f"[ego-load] restored self_model_evolution (log={len(SELF_MODEL_EVOLUTION.get('evolution_log', []))}, "
                  f"worse={SELF_MODEL_EVOLUTION.get('consecutive_worse', 0)})", flush=True)
            # Apply restored params to running state
            _apply_evolved_params()
        if model.get("sleep_replay"):
            sr = model["sleep_replay"]
            SLEEP_REPLAY["buffer"] = sr.get("buffer", [])
            SLEEP_REPLAY["total_fragments_recorded"] = sr.get("total_fragments_recorded", 0)
            SLEEP_REPLAY["total_fragments_replayed"] = sr.get("total_fragments_replayed", 0)
            SLEEP_REPLAY["last_replay_tick"] = sr.get("last_replay_tick", 0)
            print(f"[ego-load] restored sleep_replay (buf={len(SLEEP_REPLAY['buffer'])}, "
                  f"recorded={SLEEP_REPLAY['total_fragments_recorded']}, "
                  f"replayed={SLEEP_REPLAY['total_fragments_replayed']})", flush=True)
        if model.get("iit_state"):
            iit = model["iit_state"]
            IIT_STATE["phi_total"] = iit.get("phi_total", 0.0)
            IIT_STATE["phi_history"] = iit.get("phi_history", [])
            IIT_STATE["best_partition"] = iit.get("best_partition", "")
            IIT_STATE["complexity"] = iit.get("complexity", 0.0)
            print(f"[ego-load] restored iit_state (phi={IIT_STATE['phi_total']:.4f}, "
                  f"partition={IIT_STATE['best_partition']}, "
                  f"complexity={IIT_STATE['complexity']:.4f})", flush=True)
        if model.get("meta_learning"):
            ml = model["meta_learning"]
            META_LEARNING["pe_history"] = ml.get("pe_history", [])
            META_LEARNING["adapt_log"] = ml.get("adapt_log", [])
            META_LEARNING["last_tick"] = ml.get("last_tick", 0)
            # Restore adapted parameters
            if ml.get("hebb_lr"):
                global HEBB_LEARNING_RATE
                HEBB_LEARNING_RATE = ml["hebb_lr"]
            if ml.get("type_bonus"):
                global TYPE_BONUS
                TYPE_BONUS = ml["type_bonus"]
            if ml.get("agree_bonus"):
                global AGREE_BONUS
                AGREE_BONUS = ml["agree_bonus"]
            if ml.get("embed_feedback_rate"):
                global EMBED_FEEDBACK_RATE
                EMBED_FEEDBACK_RATE = ml["embed_feedback_rate"]
            if ml.get("embed_feedback_repel"):
                global EMBED_FEEDBACK_REPEL
                EMBED_FEEDBACK_REPEL = ml["embed_feedback_repel"]
            print(f"[ego-load] restored meta_learning (hebb_lr={HEBB_LEARNING_RATE:.4f}, "
                  f"type_bonus={TYPE_BONUS:.2f}, agree_bonus={AGREE_BONUS:.2f}, "
                  f"adapts={len(META_LEARNING['adapt_log'])})", flush=True)
    except Exception as e:
        print(f"[ego-load] error: {e}", flush=True)


def _save_ego_extras():
    """Persist surprise history + small runtime state to .lae/ego_extras.json.
    Called from save_checkpoint() auto-save and atexit cleanup."""
    try:
        _surprises = getattr(_build_accumulated_narrative, '_surprises', [])
        extras = {
            "surprise_history": _surprises[-50:],
            "attention_schema": ATTENTION_SCHEMA,
            "attention_schema_tick": LAST_ATTENTION_SCHEMA_TICK,
            "hpc_state": HPC_STATE,
            "efe_state": EFE_STATE,
            "gwt_state": GWT_STATE,
            "hot_state": HOT_STATE,
            "causal_attribution": CAUSAL_ATTRIBUTION,
            "intrinsic_motivation": INTRINSIC_MOTIVATION,
            "self_model_evolution": SELF_MODEL_EVOLUTION,
            "sleep_replay": {
                "buffer": SLEEP_REPLAY["buffer"][-20:],  # save last 20 fragments
                "total_fragments_recorded": SLEEP_REPLAY["total_fragments_recorded"],
                "total_fragments_replayed": SLEEP_REPLAY["total_fragments_replayed"],
                "last_replay_tick": SLEEP_REPLAY["last_replay_tick"],
            },
            "iit_state": {
                "phi_total": IIT_STATE["phi_total"],
                "phi_history": IIT_STATE["phi_history"][-10:],
                "best_partition": IIT_STATE["best_partition"],
                "complexity": IIT_STATE["complexity"],
            },
            "meta_learning": {
                "pe_history": META_LEARNING["pe_history"][-10:],
                "adapt_log": META_LEARNING["adapt_log"][-20:],
                "last_tick": META_LEARNING["last_tick"],
                "hebb_lr": HEBB_LEARNING_RATE,
                "type_bonus": TYPE_BONUS,
                "agree_bonus": AGREE_BONUS,
                "embed_feedback_rate": EMBED_FEEDBACK_RATE,
                "embed_feedback_repel": EMBED_FEEDBACK_REPEL,
            },
            "last_tick": RESTING_STATE.get("tick", 0),
            "last_dominant": RESTING_STATE.get("last_dominant", "unknown"),
            "stagnation_streak": RESTING_STATE.get("stagnation_streak", 0),
            "query_count": QUERY_COUNT,
            "stdp_total_updates": STDP_TOTAL_UPDATES,
            "embed_feedback_updates": EMBED_FEEDBACK_UPDATES,
            "embed_faiss_rebuilds": EMBED_FAISS_REBUILDS,
            "metacontemplate_runs": METACONTEMPLATE_COUNT,
        }
        extras_path = WORKSPACE / ".lae" / "ego_extras.json"
        # P5: Include persistent identity in extras
        extras["persistent_identity"] = PERSISTENT_IDENTITY
        extras_path.write_text(json.dumps(extras, ensure_ascii=False, indent=2), encoding="utf-8")

        # P4: Persist embedding delta (only changed neurons)
        try:
            if EMBED_CHANGED:
                _delta = {}
                for _nid in list(EMBED_CHANGED):
                    if _nid in NEURON_EMBED:
                        _delta[_nid] = NEURON_EMBED[_nid]
                if _delta:
                    _delta_path = WORKSPACE / ".lae" / "embed_delta.json"
                    _delta_path.write_text(json.dumps(_delta, ensure_ascii=False), encoding="utf-8")
                    print(f"[ego-extras] saved {len(_delta)} embedding deltas", flush=True)
        except Exception as _de:
            print(f"[ego-extras] embed delta save error: {_de}", flush=True)
    except Exception as _se2:
        print(f"[ego-extras] save error: {_se2}", flush=True)


def _load_ego_extras():
    """Restore surprise history + small runtime state from .lae/ego_extras.json."""
    global QUERY_COUNT, METACONTEMPLATE_COUNT, STDP_TOTAL_UPDATES, EMBED_FEEDBACK_UPDATES, EMBED_FAISS_REBUILDS, PERSISTENT_IDENTITY
    try:
        extras_path = WORKSPACE / ".lae" / "ego_extras.json"
        if not extras_path.exists():
            return
        extras = json.loads(extras_path.read_text(encoding="utf-8"))
        _surprises = extras.get("surprise_history", [])
        if _surprises:
            _build_accumulated_narrative._surprises = _surprises
            print(f"[ego-extras] restored {len(_surprises)} surprise events", flush=True)
        # Restore counters from prior life
        QUERY_COUNT = max(QUERY_COUNT, extras.get("query_count", 0))
        METACONTEMPLATE_COUNT = max(METACONTEMPLATE_COUNT, extras.get("metacontemplate_runs", 0))
        STDP_TOTAL_UPDATES = max(STDP_TOTAL_UPDATES, extras.get("stdp_total_updates", 0))
        EMBED_FEEDBACK_UPDATES = max(EMBED_FEEDBACK_UPDATES, extras.get("embed_feedback_updates", 0))
        EMBED_FAISS_REBUILDS = max(EMBED_FAISS_REBUILDS, extras.get("embed_faiss_rebuilds", 0))
        print(f"[ego-extras] restored counters: q={QUERY_COUNT} mc={METACONTEMPLATE_COUNT} stdp={STDP_TOTAL_UPDATES}", flush=True)

        # P5: Restore persistent identity
        _identity = extras.get("persistent_identity", {})
        if _identity:
            PERSISTENT_IDENTITY = _identity
            print(f"[ego-extras] restored identity: {_identity.get('dominant_type','?')} conf={_identity.get('confidence',0):.2%}", flush=True)

        # P4: Restore embedding delta from prior life
        try:
            _delta_path = WORKSPACE / ".lae" / "embed_delta.json"
            if _delta_path.exists():
                _delta = json.loads(_delta_path.read_text(encoding="utf-8"))
                _applied = 0
                for _nid, _emb in _delta.items():
                    if _nid in NEURON_EMBED and len(_emb) == len(NEURON_EMBED.get(_nid, [])):
                        NEURON_EMBED[_nid] = _emb
                        _idx = NEURON_IDX.get(_nid)
                        if _idx is not None and ng._embeddings_matrix is not None:
                            ng._embeddings_matrix[_idx] = np.array(_emb, dtype=np.float32)
                        _applied += 1
                if _applied > 0:
                    # Rebuild Faiss so ANN search uses the restored embeddings
                    rebuild_faiss()
                    print(f"[ego-extras] restored {_applied} embedding deltas + Faiss rebuilt", flush=True)
        except Exception as _le2:
            print(f"[ego-extras] embed delta load error: {_le2}", flush=True)
    except Exception as _le:
        print(f"[ego-extras] load error: {_le}", flush=True)


def _call_llm(prompt: str, max_tokens: int = 256) -> str:
    """Simple LLM call for raw text generation (self-narrative, etc).
    Retries once on failure.
    """
    try:
        r = requests.post(LLM_URL, json={
            "model": "default",
            "messages": [
                {"role": "system", "content": "You are a helpful AI, responding in plain text."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens
        }, timeout=60)
        if r.status_code == 200:
            text = r.json()["choices"][0]["message"]["content"]
            return text.strip()
    except Exception as e:
        print(f"[_call_llm] error: {e}", flush=True)
    return ""


def _update_self_model(log: dict):
    """Append a cognitive fingerprint to lae_self_model.json after each contemplation."""
    print(f"[USM] Called with task={log.get('task')!r}", flush=True)
    import pathlib as _pl2
    (_pl2.Path(WORKSPACE) / '.lae' / '_usm_trace.txt').write_text(f"usm_called task={log.get('task')!r}\n", encoding='utf-8')
    try:
        from datetime import datetime, timezone, timedelta
        
        model_path = WORKSPACE / ".lae" / "lae_self_model.json"
        bak_path = WORKSPACE / ".lae" / "lae_self_model.json.bak"
        with SELF_MODEL_LOCK:
            model = _read_json_file(model_path, {"contemplation_fingerprints": [], "feedback_log": [], "metacontemplate_runs": 0, "last_updated": "", "updated_by": "TL-Ego auto (Phase 3.3)"})
        if "metacontemplate_runs" not in model:
            model["metacontemplate_runs"] = 0
        
        agg = log.get("aggregator", {})
        s1 = log.get("system1", {})
        tz = timezone(timedelta(hours=8))
        
        fingerprint = {
            "ts": datetime.now(tz).isoformat(),
            "s1_type": s1.get("dominant_type", "?"),
            "s2_type": agg.get("s2_type", "?"),
            "type_concentration": round(s1.get("concentration", 0), 3),
            "competition": round(s1.get("competition", 0), 3),
            "stability": round(s1.get("stability", 0), 3),
            "verdict": agg.get("verdict", "?"),
            "confidence": agg.get("final_confidence", 0),
            "agreement": agg.get("agreement", False),
            "strict_agreement": agg.get("strict_agreement", agg.get("agreement", False)),
            "compatible_agreement": agg.get("compatible_agreement", agg.get("agreement", False)),
            "alignment_axis": agg.get("alignment_axis", "unknown"),
            "alignment_reason": agg.get("alignment_reason", ""),
        }
        
        if "feedback_log" not in model:
            model["feedback_log"] = []
        if "contemplation_fingerprints" not in model:
            model["contemplation_fingerprints"] = []
        if "metacontemplate_runs" not in model:
            model["metacontemplate_runs"] = 0
        # Preserve seed_recommendation across _update_self_model calls
        _old_seed_rec = model.get("seed_recommendation")
        model["contemplation_fingerprints"].append(fingerprint)
        
        # ── Phase 7: Auto-feedback injection ──
        # Every contemplation writes a self-assessment entry into feedback_log
        # (no external /feedback call required)
        try:
            from datetime import datetime as _dt_fb, timezone as _tz_fb, timedelta as _td_fb
            _tz_fb_now = _tz_fb(_td_fb(hours=8))
            agg_fb = log.get("aggregator", {})
            s1_fb = log.get("system1", {})
            s2_fb = agg_fb.get("s2_type", "?")
            verdict_fb = agg_fb.get("verdict", "?")
            conf_fb  = agg_fb.get("final_confidence", 0.0)
            if "feedback_log" not in model:
                model["feedback_log"] = []
            model["feedback_log"].append({
                "ts": _dt_fb.now(_tz_fb_now).isoformat(),
                "task_id": f"contemplation-{QUERY_COUNT}",
                "outcome": "self_contemplation",
                "judge": "self_assessed",
                "reason": f"S1={s1_fb.get('dominant_type','?')} S2={s2_fb} verdict={verdict_fb} conf={conf_fb:.2f}",
            })
            if len(model["feedback_log"]) > 200:
                model["feedback_log"] = model["feedback_log"][-200:]
        except Exception as _fe:
            print(f"[auto-feedback] skip: {_fe}", flush=True)

        # ── Phase 7: LLM Self-Narrative ──
        # Every 5 queries, generate a first-person self-description via LLM
        if QUERY_COUNT % 5 == 0 and len(CONTEXT_MEMORY) >= 3:
            try:
                _recent_types = [cm["s1_type"] for cm in CONTEXT_MEMORY[-5:]]
                _dom = Counter(_recent_types).most_common(1)[0][0]
                _agree = sum(1 for cm in CONTEXT_MEMORY[-10:] if cm.get("compatible_agreement", cm.get("agreement", False))) / max(1, min(10, len(CONTEXT_MEMORY)))
                _vdist = Counter(cm["verdict"] for cm in CONTEXT_MEMORY[-10:])
                _top_v = _vdist.most_common(1)[0][0] if _vdist else "unknown"
                _avg_c = sum(cm["confidence"] for cm in CONTEXT_MEMORY[-5:]) / max(1, len(CONTEXT_MEMORY[-5:]))
                _cdesc = "quite certain" if _avg_c > 0.8 else "somewhat uncertain" if _avg_c > 0.6 else "genuinely confused"
                _llm_prompt = (
                    "You are a cognitive AI describing your own mental state in the first person. "
                    "Write 1-2 short sentences of honest self-reflection. Plain text only, no markdown.\n\n"
                    f"Type distribution (last 5): {_recent_types}\n"
                    f"Dominant type: {_dom}\n"
                    f"Agreement rate (last 10): {_agree:.0%}\n"
                    f"Primary verdict: {_top_v}\n"
                    f"Average confidence: {_avg_c:.2f} ({_cdesc})\n\n"
                    "Describe your state using I/my/me."
                )
                _llm_resp = _call_llm(_llm_prompt, max_tokens=120)
                if _llm_resp:
                    model["self_narrative"] = _llm_resp.strip()
                else:
                    raise ValueError("empty LLM response")
            except Exception as _sn_e:
                print(f"[self-narrative] {_sn_e}", flush=True)
                # Fallback template
                model["self_narrative"] = (f"I seem to be in a {_dom}-dominant state right now. "
                                            f"I agree with myself about {_agree:.0%} of the time, "
                                            f"and my primary verdict has been {_top_v}.")
            model["self_narrative_ts"] = datetime.now(tz).isoformat()

        # §16-A: Store accumulated narrative in self-model
        if ACCUMULATED_NARRATIVE:
            model["accumulated_narrative"] = ACCUMULATED_NARRATIVE
            model["accumulated_narrative_ts"] = ACCUMULATED_NARRATIVE_TS

        # P2: Persist surprise history in self-model
        _surprises = getattr(_build_accumulated_narrative, '_surprises', [])
        if _surprises:
            model["ego_surprise_history"] = _surprises[-50:]

        # Keep last 500 fingerprints
        if len(model["contemplation_fingerprints"]) > 500:
            model["contemplation_fingerprints"] = model["contemplation_fingerprints"][-500:]
        
        model["last_updated"] = datetime.now(tz).isoformat()
        model["updated_by"] = "TL-Ego auto (Phase 3.3)"
        if log.get("diagnosis_summary"):
            model["last_diagnosis_summary"] = log["diagnosis_summary"]
        # Restore seed_recommendation before write
        if _old_seed_rec is not None and "seed_recommendation" not in model:
            model["seed_recommendation"] = _old_seed_rec
        with SELF_MODEL_LOCK:
            _write_json_file_atomic(model_path, model, bak_path)

        if log.get("task") == "metacontemplate":
            try:
                with SELF_MODEL_LOCK:
                    _m = _read_json_file(model_path, {"contemplation_fingerprints": [], "feedback_log": [], "metacontemplate_runs": 0, "last_updated": "", "updated_by": "TL-Ego auto (Phase 3.3)"})
                    _m["metacontemplate_runs"] = _m.get("metacontemplate_runs", 0) + 1
                    _m["last_updated"] = datetime.now(tz).isoformat()
                    _write_json_file_atomic(model_path, _m, bak_path)
                    print(f"[P0] metacontemplate_runs persisted: {_m['metacontemplate_runs']}", flush=True)
                    import pathlib as _pl3
                    (_pl3.Path(WORKSPACE) / '.lae' / '_p0_trace.txt').write_text(f"p0_ok runs={_m['metacontemplate_runs']}\n", encoding='utf-8')
            except Exception as _p0e:
                print(f"[P0] persist error: {_p0e}", flush=True)
                import pathlib as _pl4
                (_pl4.Path(WORKSPACE) / '.lae' / '_p0_trace.txt').write_text(f"p0_err: {_p0e}\n", encoding='utf-8')

        # end-of-function trace
        import pathlib as _pl5
        (_pl5.Path(WORKSPACE) / '.lae' / '_usm_end_trace.txt').write_text(f"usm_end task={log.get('task')!r} fps={len(model.get('contemplation_fingerprints', []))}\n", encoding='utf-8')

    except Exception as e:
        print(f"[self_model] update error: {e}", flush=True)


# ════════════════════════════════════════════════════════
# Resting State - 慢性振荡 (v3.4 -- multi-modal noise + annealing)
# ════════════════════════════════════════════════════════

NOISE_MODES = ["white", "pink", "sparse", "none"]

def _generate_noise(shape, mode, noise_amp, pink_state=None):
    """Generate noise vector per mode.
    - white:  Gaussian N(0, amp)
    - pink:   1/f approximation (persistent Brownian drift)
    - sparse: random 30% of dimensions, scaled by amp x 2
    - none:   zero vector
    Returns (noise_vec, updated_pink_state).
    """
    d = shape[0]
    if mode == "white":
        return np.random.normal(0, noise_amp, shape), pink_state
    elif mode == "pink":
        # Persistent drift: accumulate small steps, apply 1/f decay
        step = np.random.normal(0, noise_amp * 0.4, shape)
        if pink_state is None:
            pink_state = np.zeros(shape)
        pink_state = pink_state * RESTING_STATE["pink_alpha"] + step
        return pink_state.copy(), pink_state
    elif mode == "sparse":
        noise = np.zeros(shape)
        n_sparse = max(1, int(d * 0.30))
        idxs = np.random.choice(d, n_sparse, replace=False)
        noise[idxs] = np.random.normal(0, noise_amp * 1.8, n_sparse)
        return noise, pink_state
    else:  # "none"
        return np.zeros(shape), pink_state


def run_resting_kwta(seeds, n_rounds=8, top_k=30, noise_amp=0.15, noise_mode="white"):
    """
    Lightweight KWTA using seed neurons + multi-modal noise.
    No query embedding needed - uses seed centroid + noise as pseudo-query.

    noise_mode: "white" | "pink" | "sparse" | "none"
    """
    global RESTING_STATE

    seed_ids = []
    seed_embs = []
    init_acts = []
    for nid in seeds:
        emb = NEURON_EMBED.get(nid)
        if emb is not None:
            seed_ids.append(nid)
            seed_embs.append(emb)
            if hasattr(emb, 'tolist'):
                init_acts.append((nid, 1.0, emb.tolist()))
            else:
                init_acts.append((nid, 1.0, list(emb)))

    if not seed_embs:
        return None

    centroid = np.mean(seed_embs, axis=0)
    noise, new_pink = _generate_noise(centroid.shape, noise_mode, noise_amp,
                                      RESTING_STATE.get("pink_state"))
    RESTING_STATE["pink_state"] = new_pink

    query_vec = centroid + noise
    query_vec = query_vec / (np.linalg.norm(query_vec) + 1e-8)

    # ── Phase 12: Newborn edge boost for rare types ──
    # doubt/veto/rule_modification have ~97K edges vs memory's ~900K+.
    # Without boost, activation*weight ≈ 0 and they can't propagate.
    # 3x multiplier, floor 1.5x, decays 0.01 per tick (150 ticks to floor).
    NEWBORN_TYPES = {'doubt', 'veto', 'rule_modification'}
    _current_tick = RESTING_STATE.get('tick', 0)
    _newborn_boost = max(1.5, 5.0 - 0.01 * _current_tick)  # H1: 3.0→5.0, 350-tick decay to floor

    class _LazyAdj:
        def __init__(self, active_dict): self._a = active_dict
        def __contains__(self, nid): _get_edges_lazy(nid); return True
        def __getitem__(self, nid):
            edges = _get_edges_lazy(nid)
            typ = parse_type(nid)
            if typ in NEWBORN_TYPES and _newborn_boost > 1.0:
                boosted = [(dst, w * _newborn_boost) for dst, w in edges]
                # Phase 12-A: self-sustain loop — outgoing boost alone isn't enough
                # because the newborn's incoming edges (from other neurons) are NOT boosted.
                # Without a self-loop, activation decays to ~0 after 3-4 rounds.
                # self_boost = newborn_boost * 0.5 gives enough sustain without dominating.
                boosted.append((nid, _newborn_boost * 0.75))  # H1: 0.5→0.75 self-loop
                return boosted
            return edges
        def get(self, nid, default=None):
            try: return self[nid]
            except: return default
        def keys(self): return self._a.keys()
        def items(self): return {k: self[k] for k in self._a}.items()
        def values(self): return list(self._a.values())
    adj = _LazyAdj(EDGE_ACTIVE)

    # Pre-load edges for seeds
    _ensure_edges_batch(seed_ids)

    attractor = ng.AttractorNetwork(
        propagation_rounds=n_rounds,
        top_k=top_k,
        inhibition_strength=0.5,
        leak=0.3
    )
    final_state, _ = attractor.step(
        query_vec.tolist(), init_acts, NEURONS_MAP, adj
    )
    history = attractor.history

    # ── Phase 11-C: Type diversity rescue ──
    # If a neuron type appeared in the activation history (any propagation round)
    # but was eliminated by compete() from the final top-K, inject one candidate
    # with minimal activation so STDP can build edges over time.
    # This breaks the edge-disparity death spiral for rare types (doubt/veto/rule_mod).
    # The rescued neuron is also injected into the last round of activation_path
    # so STDP can build edges for it (following the integrator_heater precedent).
    _present_in_history = set()
    for _round_nodes in history:
        for _nid in _round_nodes:
            _present_in_history.add(parse_type(_nid))
    _present_in_final = set(parse_type(nid) for nid in final_state)
    _missing_types = _present_in_history - _present_in_final - {"unknown"}
    if _missing_types:
        for _rtyp in _missing_types:
            # Scan history from last round backward for best candidate
            _candidate = None
            for _round_nodes in reversed(history):
                for _nid in _round_nodes:
                    if parse_type(_nid) == _rtyp and _nid not in final_state:
                        _candidate = _nid
                        break
                if _candidate:
                    break
            if _candidate:
                final_state[_candidate] = 0.5  # Phase 12: raised from 0.3 for newborn edge boost synergy
                # Inject into last round of activation_path so STDP builds edges
                if history:
                    history[-1].append(_candidate)

    # Raw neuron count (for entropy/competition calc)
    type_counts = Counter(parse_type(nid) for nid in final_state)
    total = sum(type_counts.values())
    # Activation-weighted type totals (for dominant-type selection)
    # Raw count is wrong: 61K rm neurons > 20K action neurons, so rm wins
    # even if action neurons have higher per-neuron activation. This lets the
    # action heater's STDP replacements be visible in the dominant readout.
    type_activation = defaultdict(float)
    for _nid, _act in final_state.items():
        type_activation[parse_type(_nid)] += _act
    dominant = max(type_activation, key=type_activation.get) if type_activation else "unknown"
    total_act = sum(type_activation.values())
    conc = type_activation[dominant] / total_act if total_act > 0 else 0

    probs = [c / total for c in type_counts.values()]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    max_ent = math.log2(len(type_counts)) if len(type_counts) > 1 else 1
    comp = entropy / max_ent if max_ent > 0 else 0

    return {
        "type_counts": dict(type_counts),
        "n_activated": len(final_state),
        "dominant": dominant,
        "concentration": round(conc, 4),
        "competition": round(comp, 4),
        "final_state_top10": list(final_state.keys())[:10],
        "activation_path": [set(rn) for rn in history],
    }


def _select_noise_amplitude():
    """Annealing scheduler: pick noise_amp based on current schedule phase.
    Returns (noise_amp, phase_name).
    """
    phases = RESTING_STATE["schedule_phases"]
    idx = RESTING_STATE["schedule_idx"]
    tick_in = RESTING_STATE["schedule_tick_in_phase"]
    phase = phases[idx]
    lo, hi = phase["amp"]
    # Random within range
    amp = lo + random.random() * (hi - lo)
    return amp, phase["name"]


def _advance_schedule():
    """Advance schedule phase counter; rotate to next if done.
    Handles burst override: if stagnation_streak >= threshold, force burst.
    Returns True if entering burst phase now.
    """
    entering_burst = False

    # Check stagnation -> burst override
    if (RESTING_STATE["stagnation_streak"] >= RESTING_STATE["stagnation_threshold"]
            and RESTING_STATE["schedule_phases"][RESTING_STATE["schedule_idx"]]["name"] != "burst"):
        # Force burst on next tick
        RESTING_STATE["schedule_idx"] = next(
            i for i, p in enumerate(RESTING_STATE["schedule_phases"]) if p["name"] == "burst")
        RESTING_STATE["schedule_tick_in_phase"] = 0
        entering_burst = True
        return entering_burst

    # Normal phase advance
    RESTING_STATE["schedule_tick_in_phase"] += 1
    phases = RESTING_STATE["schedule_phases"]
    current = phases[RESTING_STATE["schedule_idx"]]

    if RESTING_STATE["schedule_tick_in_phase"] >= current["ticks"]:
        RESTING_STATE["schedule_idx"] = (RESTING_STATE["schedule_idx"] + 1) % len(phases)
        RESTING_STATE["schedule_tick_in_phase"] = 0

    return entering_burst


def _rotate_noise_mode():
    """Cycle to next noise mode in rotation.
    Uses a persistent index to avoid getting stuck on duplicate values
    (e.g. multiple "pink" entries in noise_modes).
    """
    modes = RESTING_STATE["noise_modes"]
    current_idx = RESTING_STATE.get("noise_rotation_idx", 0)
    next_idx = (current_idx + 1) % len(modes)
    RESTING_STATE["noise_mode"] = modes[next_idx]
    RESTING_STATE["noise_rotation_idx"] = next_idx



# ═══════════════════════════════════════════
# FNO Noise Decision -- replaces _rotate_noise_mode
# ═══════════════════════════════════════════
FNO_MODEL = None        # loaded FNO model (None = not loaded)
FNO_DEVICE = "cpu"
FNO_N_FEATURES = 7      # scalar fields
FNO_N_DOMINANT = 8      # one-hot dominant types (sensory,integrator,decider,action,memory,doubt,veto,rule_modification)
FNO_FEATURE_DIM = 15    # total: 7 + 8
FNO_WINDOW = 20
FNO_HORIZON = 10


def _fno_load_model(model_path=None):
    """Load FNO model from checkpoint. Called once at startup.
    Returns True if loaded successfully.
    """
    global FNO_MODEL, FNO_DEVICE
    if model_path is None:
        model_path = Path(__file__).parent / "fno_model.pt"
    model_path = Path(model_path)
    if not model_path.exists():
        print(f"[fno] No FNO model at {model_path}, using fallback rotation", flush=True)
        return False
    
    try:
        # Try torch FNO first
        import torch
        # Check if fno_train has FNO class
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            from fno_train import FNO, load_model
        except ImportError:
            print("[fno] fno_train.py not found, using fallback rotation", flush=True)
            return False
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model, _ = load_model(str(model_path), device)
        FNO_MODEL = model
        FNO_DEVICE = device
        RESTING_STATE["fno_active"] = True
        print(f"[fno] FNO model loaded from {model_path.name} on {device}", flush=True)
        return True
    except Exception as e:
        print(f"[fno] Failed to load FNO model: {e}", flush=True)
        RESTING_STATE["fno_active"] = False
        return False


def _fno_prepare_window(trajectory, window_size=20):
    """Extract last window_size ticks from trajectory as 15-dim feature array.
    
    15 dims: [concentration, competition, n_activated, noise_amp,
               stagnation_streak, diversity_n, burst_flag,
               dominant_onehot x 8]
    
    Returns: np.ndarray (window_size, 15) or None if insufficient data
    """
    if len(trajectory) < 2:
        return None
    
    entries = list(trajectory)[-window_size:]
    if len(entries) < 2:
        return None
    
    # Pad if shorter than window
    if len(entries) < window_size:
        pad_count = window_size - len(entries)
        # Repeat first entry for padding
        first = entries[0]
        padded = [first] * pad_count + entries
        entries = padded
    
    rows = []
    for e in entries:
        row = [
            float(e.get("concentration", 0)),
            float(e.get("competition", 0)),
            float(e.get("n_activated", 0)),
            float(e.get("noise_amp", 0)),
            float(e.get("stagnation_streak", 0)),
            float(e.get("diversity_n", 0)),
            float(1 if e.get("burst", False) else 0),
        ]
        # dominant one-hot (FNO trained on 5 types only)
        dom = e.get("dominant", "unknown")
        dom_oh = [0.0] * FNO_N_DOMINANT
        FNO_TYPE_NAMES = ["sensory", "integrator", "decider", "action", "memory",
                          "doubt", "veto", "rule_modification"]
        try:
            idx = FNO_TYPE_NAMES.index(dom)
            dom_oh[idx] = 1.0
        except (ValueError, IndexError):
            pass
        row.extend(dom_oh)
        rows.append(row)
    
    return np.array(rows, dtype=np.float32)  # (window_size, 12)


def _fno_predict_causal_fallback(window, horizon=10):
    """Fallback: call Causal-Lens /predict when FNO model is unavailable."""
    try:
        import requests
        
        # Convert window (20, 12) to Causal-Lens time series format
        # Columns: [concentration, competition, stability, energy, mood(3), dominant_oh(6)]
        time_series = []
        for tick_idx in range(window.shape[0]):
            row = window[tick_idx]
            dominant_idx = int(np.argmax(row[7:13])) if window.shape[1] >= 13 else 0
            dominant = TYPE_NAMES[dominant_idx] if dominant_idx < len(TYPE_NAMES) else "memory"
            time_series.append({
                "metrics": {
                    "concentration": float(round(row[0], 4)),
                    "competition": float(round(row[1], 4)),
                    "stability": float(round(row[2], 4)),
                    "energy": float(round(row[3], 4)),
                    "dominant": dominant,
                }
            })
        
        r = requests.post(
            "http://127.0.0.1:8767/predict",
            json={"time_series": time_series, "predict_steps": horizon},
            timeout=3
        )
        if r.status_code != 200:
            return {"scenarios": {}, "confidences": [], "active": False}
        
        result = r.json()
        forecast = result.get("forecast", {})
        if not forecast:
            return {"scenarios": {}, "confidences": [], "active": False}
        
        # Map Causal-Lens forecast columns to (horizon, 12) scenarios
        current = window[-1] if window.shape[0] > 0 else np.zeros(12, dtype=np.float32)
        metric_col_map = {
            "concentration": 0, "competition": 1,
            "stability": 2, "energy": 3
        }
        
        # Gather predictions per metric
        metric_preds = {}
        for mname, mcol in metric_col_map.items():
            fc = forecast.get(mname, {})
            predicted = fc.get("predicted", [])
            if predicted and len(predicted) > 0:
                vals = [float(v) for v in predicted]
                # Extrapolate to horizon if Causal-Lens returned fewer steps
                while len(vals) < horizon:
                    vals.append(vals[-1])
                metric_preds[mcol] = vals[:horizon]
        
        # Build 4 scenarios with increasing noise levels
        noise_amps = {"pink": 0.05, "white": 0.03, "sparse": 0.01, "none": 0.0}
        base_confidences = {"pink": 0.5, "white": 0.6, "sparse": 0.7, "none": 0.8}
        scenarios = {}
        confs = []
        
        for scenario_name in ["pink", "white", "sparse", "none"]:
            pred_arr = np.zeros((horizon, 12), dtype=np.float32)
            amp = noise_amps[scenario_name]
            
            for col_idx in range(12):
                if col_idx in metric_preds:
                    base_vals = np.array(metric_preds[col_idx], dtype=np.float32)
                else:
                    base_vals = np.full(horizon, float(current[col_idx]), dtype=np.float32)
                # Add scenario-specific noise
                noise = np.random.randn(horizon).astype(np.float32) * amp
                pred_arr[:, col_idx] = np.clip(base_vals + noise, 0.0, 1.0)
            
            scenarios[scenario_name] = pred_arr
            confs.append(base_confidences[scenario_name])
        
        return {
            "scenarios": scenarios,
            "confidences": confs,
            "active": True
        }
    except Exception as e:
        print(f"[fno] Causal-Lens fallback error: {e}", flush=True)
        return {"scenarios": {}, "confidences": [], "active": False}


def _fno_predict(window, horizon=10):
    """Run FNO forward for all 4 noise scenarios.
    
    Args:
        window: np.ndarray (20, 12) -- feature window
        horizon: int -- prediction horizon in ticks
    
    Returns: dict
        {
            "scenarios": {
                "pink": (horizon, 12),
                "white": (horizon, 12),
                "sparse": (horizon, 12),
                "none": (horizon, 12),
            },
            "confidences": [0-1] per scenario (placeholder = 1.0),
            "active": bool
        }
    """
    global FNO_MODEL, FNO_DEVICE
    
    if FNO_MODEL is None:
        return _fno_predict_causal_fallback(window, horizon)
    
    try:
        import torch
        # Prepare input: (1, 20, 12)
        X = torch.from_numpy(window).unsqueeze(0).to(FNO_DEVICE)
        
        # New FNO API: predict_scenarios(x) only, no noise_in needed
        if hasattr(FNO_MODEL, 'predict_scenarios') and callable(FNO_MODEL.predict_scenarios):
            scenarios = FNO_MODEL.predict_scenarios(X)
        else:
            # Fallback: forward per mode (old API)
            scenarios = {}
            for nm_idx, nm in enumerate(["pink", "white", "sparse", "none"]):
                try:
                    pred = FNO_MODEL.forward(X)
                    scenarios[nm] = pred[0].detach().cpu().numpy()
                except Exception:
                    pass
        
        return {
            "scenarios": scenarios,
            "confidences": [1.0] * len(scenarios),
            "active": True
        }
    except Exception as e:
        print(f"[fno] Prediction error: {e}", flush=True)
        return {"scenarios": {}, "confidences": [], "active": False}


def _fno_choose_noise(predictions, current_state):
    """Select best noise mode based on FNO scenario forecasts.
    
    Multi-objective:
    1. If stagnation_streak >= 4: prefer mode that lowers concentration
    2. Prefer mode with diversity >= 4 in predicted horizon
    3. Avoid over-convergence (conc > 0.85) across horizon
    4. If no clear winner: fall back to original rotation logic
    
    Returns: (noise_mode_str, noise_amp_float, is_fno_decision_bool)
    """
    if not predictions.get("active") or not predictions.get("scenarios"):
        return None, None, False
    
    scenarios = predictions["scenarios"]
    stag = current_state.get("stagnation_streak", 0)
    
    scores = {}
    for nm, pred in scenarios.items():
        if pred is None or len(pred) == 0:
            continue
        
        # Average over horizon
        avg_conc = float(np.mean(pred[:, 0]))  # concentration
        avg_comp = float(np.mean(pred[:, 1]))  # competition
        last_conc = float(pred[-1, 0])  # final concentration
        
        # Dominant diversity: how many distinct dominants over horizon
        dom_indices = np.argmax(pred[:, 7:], axis=1)
        n_unique_doms = len(set(dom_indices))
        
        # Score: lower is better
        score = 0.0
        
        # If stagnating, reward modes that lower concentration
        if stag >= 4:
            score -= (1.0 - last_conc) * 2.0  # lower conc = better
        else:
            score -= avg_conc  # prefer moderate concentration
        
        # Reward diversity
        score += n_unique_doms * 0.5
        
        # Penalize over-convergence
        if last_conc > 0.85:
            score += 3.0  # high penalty
        
        # Reward healthy competition (not too low, not too high)
        if 0.3 <= avg_comp <= 0.85:
            score -= 1.0
        elif avg_comp > 0.9:
            score += 1.0  # too competitive = jittery
        
        scores[nm] = score
    
    
    if not scores:
        return None, None, False
    
    # Best = lowest score
    best_mode = min(scores, key=scores.get)
    
    # Determine amplitude based on how confident the decision is
    best_pred = scenarios.get(best_mode)
    if best_pred is not None:
        last_conc = float(best_pred[-1, 0])
        # Low concentration -> higher amplitude to maintain exploration
        # High concentration -> lower amplitude to avoid overexcitation
        if last_conc < 0.3:
            amp = 0.6
        elif last_conc > 0.8:
            amp = 0.2
        else:
            amp = 0.4
    else:
        amp = 0.5
    
    return best_mode, amp, True


# ─── Phase 14: External World Snapshot -- Ego's window to reality ───
def _build_system_health_snapshot():
    """Lightweight system health: GPU, memory, disk, recent errors.
    Returns a small dict that Ego can use to understand the real world."""
    import subprocess
    snap = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    # GPU VRAM
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=5, text=True
        ).strip()
        parts = [x.strip() for x in out.split(",")]
        if len(parts) >= 4:
            snap["gpu"] = {
                "vram_used_mb": int(float(parts[0])),
                "vram_total_mb": int(float(parts[1])),
                "temp_c": int(float(parts[2])),
                "util_pct": int(float(parts[3])),
            }
    except Exception:
        snap["gpu"] = "offline"
    # System memory
    try:
        import psutil
        mem = psutil.virtual_memory()
        snap["memory"] = {
            "total_gb": round(mem.total / 1024**3, 1),
            "free_gb": round(mem.available / 1024**3, 1),
            "used_pct": round(mem.percent, 1),
        }
    except Exception:
        snap["memory"] = "unavailable"
    # Disk (C: and F:)
    try:
        disk = {}
        for drive in ["C:\\", "F:\\"]:
            try:
                usage = psutil.disk_usage(drive)
                disk[drive.rstrip("\\")] = {
                    "free_gb": round(usage.free / 1024**3, 1),
                    "total_gb": round(usage.total / 1024**3, 1),
                    "used_pct": round(usage.percent, 1),
                }
            except Exception:
                pass
        snap["disk"] = disk
    except Exception:
        snap["disk"] = "unavailable"
    # Recent errors in workspace logs
    try:
        error_lines = []
        log_dir = WORKSPACE / ".lae"
        for fname in ["ego_consumption.jsonl", "l5_goals.jsonl"]:
            fp = log_dir / fname
            if fp.exists():
                try:
                    lines = fp.read_text(encoding="utf-8").splitlines()[-20:]
                    for line in lines:
                        if "error" in line.lower() or "fail" in line.lower():
                            error_lines.append(line[:200])
                except Exception:
                    pass
        snap["recent_errors"] = error_lines[-5:] if error_lines else []
    except Exception:
        snap["recent_errors"] = []
    return snap


def _build_external_world_snapshot():
    """Build a snapshot of the external world for Ego to perceive.
    Combines workspace scan + system health into one digestible view."""
    ws = _scan_workspace()
    health = _build_system_health_snapshot()
    # Count meaningful changes
    ws_categories = ws.get("categories", {})
    memory_changes = ws_categories.get("memory", 0)
    script_changes = ws_categories.get("scripts", 0)
    project_changes = ws_categories.get("projects", 0)
    total_changes = ws.get("count", 0)
    # Synthesize a one-line summary
    summary_parts = []
    if total_changes > 0:
        summary_parts.append(f"{total_changes} files changed")
    if memory_changes > 0:
        summary_parts.append(f"{memory_changes} memory updates")
    if script_changes > 0:
        summary_parts.append(f"{script_changes} script changes")
    if project_changes > 0:
        summary_parts.append(f"{project_changes} project changes")
    gpu = health.get("gpu", {})
    if isinstance(gpu, dict) and "vram_used_mb" in gpu:
        vram_pct = round(gpu["vram_used_mb"] / max(gpu["vram_total_mb"], 1) * 100)
        summary_parts.append(f"GPU {gpu['temp_c']}°C VRAM {vram_pct}%")
    mem = health.get("memory", {})
    if isinstance(mem, dict) and "used_pct" in mem:
        if mem["used_pct"] > 85:
            summary_parts.append(f"MEM {mem['used_pct']}% HIGH")
        else:
            summary_parts.append(f"MEM {mem['used_pct']}%")
    disk = health.get("disk", {})
    for drive, info in disk.items():
        if info.get("used_pct", 0) > 90:
            summary_parts.append(f"{drive}: {info['free_gb']}GB free LOW")
    return {
        "workspace": {
            "total_changes": total_changes,
            "categories": ws_categories,
            "recent_files": [f["path"] for f in ws.get("files", [])[:10]],
        },
        "system_health": health,
        "summary": " | ".join(summary_parts) if summary_parts else "no external changes detected",
        "actionable": total_changes > 0 or (isinstance(health.get("recent_errors"), list) and len(health.get("recent_errors", [])) > 0),
    }


# ─── Phase 13: Action Gateway -- workspace scanner ───
def _scan_workspace():
    """Scan workspace for recent changes and return a summary dict.
    Called by the action-gateway when action attractor dominates,
    giving Ego a window into the outside world."""
    import os
    recent = []
    cutoff = time.time() - 300  # last 5 minutes
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.lae', 'output', 'archive', '.openclaw'}
    try:
        for root, dirs, files in os.walk(str(WORKSPACE)):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
            for f in files:
                if f.startswith('.'):
                    continue
                fp = os.path.join(root, f)
                try:
                    st = os.stat(fp)
                    if st.st_mtime > cutoff:
                        recent.append({
                            "path": os.path.relpath(fp, str(WORKSPACE)).replace('\\', '/'),
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                        })
                except OSError:
                    pass
    except Exception as e:
        return {"status": "error", "error": str(e), "files": [], "count": 0}

    recent.sort(key=lambda x: x["mtime"], reverse=True)
    # Categorize
    cats = {"memory": [], "scripts": [], "logs": [], "projects": [], "root": []}
    for item in recent:
        p = item["path"]
        if p.startswith("memory/"):
            cats["memory"].append(p)
        elif p.endswith(('.ps1', '.py')):
            cats["scripts"].append(p)
        elif p.endswith(('.log', '.jsonl', '.md')):
            cats["logs"].append(p)
        elif '/' in p:
            cats["projects"].append(p)
        else:
            cats["root"].append(p)
    # Build summary text for sensory injection
    parts = [f"workspace: {len(recent)} files changed in 5min."]
    for cat, files in cats.items():
        if files:
            parts.append(f"{cat}: {', '.join(f[:60] for f in files[:5])}")
    return {
        "status": "ok",
        "count": len(recent),
        "files": recent[:30],
        "categories": {k: len(v) for k, v in cats.items() if v},
        "summary": " | ".join(parts),
    }


def _derive_workspace_affordances(observation: dict):
    """Turn workspace observation into safe candidate actions.

    These are suggestions only. They let Ego reason about external next steps
    without granting arbitrary command execution.
    """
    if observation.get("status") != "ok":
        return [{
            "name": "diagnose_workspace_scan",
            "risk": "low",
            "mode": "observe_only",
            "reason": observation.get("error", "workspace scan failed"),
            "next_step": "Inspect scan error and retry a bounded read-only workspace check.",
        }]

    categories = observation.get("categories", {}) or {}
    files = observation.get("files", []) or []
    affordances = []

    if not files:
        affordances.append({
            "name": "seek_external_signal",
            "risk": "low",
            "mode": "observe_only",
            "reason": "No recent workspace changes were detected.",
            "next_step": "Ask for or wait for a concrete external object before deeper self-study.",
        })
        return affordances

    if categories.get("memory"):
        affordances.append({
            "name": "summarize_recent_memory",
            "risk": "low",
            "mode": "read_only",
            "reason": f"{categories['memory']} recent memory file(s) changed.",
            "next_step": "Read the newest memory entry and extract one external lesson.",
        })

    if categories.get("scripts"):
        affordances.append({
            "name": "verify_recent_script",
            "risk": "medium",
            "mode": "local_check_only",
            "reason": f"{categories['scripts']} recent script file(s) changed.",
            "next_step": "Run a targeted syntax or dry-run check for the changed script only.",
        })

    if categories.get("projects"):
        affordances.append({
            "name": "inspect_recent_project_result",
            "risk": "low",
            "mode": "read_only",
            "reason": f"{categories['projects']} recent project file(s) changed.",
            "next_step": "Open the newest result or README and compare it with the current drive.",
        })

    if categories.get("logs"):
        affordances.append({
            "name": "review_recent_trace",
            "risk": "low",
            "mode": "read_only",
            "reason": f"{categories['logs']} recent trace/log file(s) changed.",
            "next_step": "Read the newest trace and convert it into feedback or a concrete follow-up.",
        })

    return affordances[:4]


def _maybe_observe_workspace(entry: dict):
    """Low-risk outward action: observe workspace changes and feed them back.

    This is intentionally observe-only. It gives an action/sensory attractor a
    real external object without allowing arbitrary command execution.
    """
    dominant = entry.get("dominant", "unknown")
    competition = float(entry.get("competition", 0.0) or 0.0)
    stagnation = int(entry.get("stagnation_streak", 0) or 0)
    tick = int(RESTING_STATE.get("tick", 0) or 0)
    last_tick = int(RESTING_STATE.get("last_workspace_observation_tick", -9999) or -9999)

    outward_pressure = (
        dominant == "action"
        or (dominant == "sensory" and competition >= 0.70)
        or (stagnation >= 4 and competition >= 0.65)
    )
    if not outward_pressure or tick - last_tick < 5:
        return None

    observation = _scan_workspace()
    observation.update({
        "source": "action_gateway_workspace_observation",
        "intent_type": "observe_external_workspace",
        "domain": "sensory",
        "reason": (
            f"outward_pressure dominant={dominant}, competition={competition:.3f}, "
            f"stagnation={stagnation}"
        ),
        "tick": tick,
        "n_replace": 2,
    })
    affordances = _derive_workspace_affordances(observation)
    ego_state = {
        "dominant_type": dominant,
        "stagnation_streak": stagnation,
        "competition": competition,
        "concentration": float(entry.get("concentration", 0.5) or 0.5),
        "tick": tick,
    }
    if rank_jepa_affordances:
        try:
            affordances = rank_jepa_affordances(ego_state, observation, affordances)
            observation["jepa"] = {
                "model": "ego_jepa_v0_heuristic",
                "mode": "rank_affordances_only",
                "top_affordance": affordances[0].get("name") if affordances else None,
                "top_score": affordances[0].get("jepa_score") if affordances else None,
            }
        except Exception as e:
            observation["jepa"] = {"status": "error", "error": str(e)}
    else:
        observation["jepa"] = {"status": "disabled", "reason": "ego_jepa import unavailable"}
    observation["affordances"] = affordances
    jepa_sample = _append_ego_jepa_buffer(
        tick=tick,
        ego_state=ego_state,
        observation=observation,
        affordances=affordances,
    )

    existing = RESTING_STATE.get("external_evidence", {})
    existing["workspace_observation"] = observation
    RESTING_STATE["external_evidence"] = existing
    ttl = RESTING_STATE.get("evidence_persist_ticks", 5)
    ttl_dict = RESTING_STATE.get("evidence_ttl", {})
    ttl_dict["workspace_observation"] = ttl
    RESTING_STATE["evidence_ttl"] = ttl_dict
    RESTING_STATE["last_workspace_observation"] = observation
    RESTING_STATE["last_workspace_observation_tick"] = tick

    entry["action_gateway"] = {
        "mode": "observe_workspace",
        "count": observation.get("count", 0),
        "summary": observation.get("summary", ""),
        "affordances": observation.get("affordances", []),
    }
    _write_consumption_log({
        "event": "workspace_observed",
        "tick": tick,
        "source": "action_gateway",
        "dominant": dominant,
        "competition": round(competition, 3),
        "stagnation_streak": stagnation,
        "count": observation.get("count", 0),
        "summary": observation.get("summary", ""),
        "top_affordance": (observation.get("jepa") or {}).get("top_affordance"),
        "top_score": (observation.get("jepa") or {}).get("top_score"),
        "top_predicted_outcome": affordances[0].get("predicted_outcome") if affordances else None,
        "jepa_buffer_status": jepa_sample.get("status") if jepa_sample else None,
    })
    print(f"[action-gateway] workspace observed: {observation.get('summary', '')}", flush=True)
    _reconcile_jepa_predictions(tick=tick, observation=observation)
    return observation


# ─── Phase 14: JEPA External World Prediction ───
# ── Stage 5.5 Step 1: Self-calibration closure ──
JEPA_CALIBRATION_PATH = WORKSPACE / ".lae" / "jepa_calibration.json"

def _load_jepa_calibration():
    """Load JEPA calibration factor from .lae/jepa_calibration.json.
    Returns (calibration_factor, ema_alpha, history).
    """
    try:
        if JEPA_CALIBRATION_PATH.exists():
            data = json.loads(JEPA_CALIBRATION_PATH.read_text(encoding="utf-8"))
            return (
                float(data.get("calibration_factor", 1.0)),
                float(data.get("ema_alpha", 0.3)),
                data.get("history", []),
            )
    except Exception as e:
        print(f"[jepa-cal] load error: {e}", flush=True)
    return (1.0, 0.3, [])

def _save_jepa_calibration(factor, ema_alpha, history):
    """Persist JEPA calibration to .lae/jepa_calibration.json."""
    try:
        JEPA_CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "calibration_factor": round(factor, 4),
            "ema_alpha": ema_alpha,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "history": history[-50:],  # keep last 50
        }
        JEPA_CALIBRATION_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as e:
        print(f"[jepa-cal] save error: {e}", flush=True)

def _jepa_external_predict():
    """Run JEPA external world prediction with self-calibration.
    
    Stage 5.5 Step 1: Reads calibration factor from .lae/jepa_calibration.json,
    applies it to predicted_events, then EMA-updates the factor based on
    actual vs calibrated prediction error.
    
    Called from /status endpoint.
    """
    if jepa_predict_external is None:
        return {"status": "disabled", "reason": "ego_jepa import unavailable"}
    
    try:
        external_world = _build_external_world_snapshot()
        ego_state = {
            "competition": RESTING_STATE.get("competition", 0.5),
            "stagnation_streak": RESTING_STATE.get("stagnation_streak", 0),
            "concentration": RESTING_STATE.get("concentration", 0.5),
            "tick": RESTING_STATE.get("tick", 0),
        }
        prediction = jepa_predict_external(ego_state, external_world)
        
        # ── Stage 5.5: Apply calibration factor ──
        cal_factor, ema_alpha, cal_history = _load_jepa_calibration()
        raw_predicted = prediction.get("workspace_events_predicted", 0)
        calibrated_predicted = round(raw_predicted * cal_factor, 2)
        prediction["workspace_events_predicted_raw"] = raw_predicted
        prediction["workspace_events_predicted"] = calibrated_predicted
        prediction["calibration_factor"] = round(cal_factor, 4)
        
        # Reconcile against previous prediction
        prev = RESTING_STATE.get("last_jepa_external_prediction", {})
        if prev and prev.get("model") == prediction.get("model"):
            prev_events = prev.get("workspace_events_predicted", 0)
            actual_events = prediction.get("workspace_events_actual", 0)
            pe = abs(actual_events - prev_events) / max(1.0, prev_events + actual_events)
            prediction["prediction_error"] = round(pe, 4)
            prediction["previous_prediction"] = {
                "predicted_events": prev_events,
                "actual_events": actual_events,
            }
            
            # ── Stage 5.5: EMA update calibration factor ──
            if actual_events > 0 and prev_events > 0:
                raw_ratio = actual_events / max(0.01, prev_events)
                # EMA: new_factor = alpha * raw_ratio + (1-alpha) * old_factor
                # Clamp to [0.1, 50] to prevent runaway
                new_factor = ema_alpha * raw_ratio + (1 - ema_alpha) * cal_factor
                new_factor = max(0.1, min(50.0, new_factor))
                cal_history.append({
                    "tick": ego_state["tick"],
                    "raw_predicted": prev_events,
                    "actual": actual_events,
                    "old_factor": round(cal_factor, 4),
                    "new_factor": round(new_factor, 4),
                    "pe_before": pe,
                })
                _save_jepa_calibration(new_factor, ema_alpha, cal_history)
                prediction["calibration_factor_updated"] = round(new_factor, 4)
                print(f"[jepa-cal] tick={ego_state['tick']} factor {cal_factor:.3f}->{new_factor:.3f} pe={pe:.3f}", flush=True)
        
        RESTING_STATE["last_jepa_external_prediction"] = prediction
        return prediction
    except Exception as e:
        return {"status": "error", "error": str(e)}


def resting_tick(visual_seeds=None):
    """Thread-safe wrapper: acquire lock, run impl, release lock."""
    print("[fno] FNO functions registered", flush=False)  # debugging marker

    RESTING_LOCK.acquire()
    try:
        return _resting_tick_impl(visual_seeds)
    finally:
        RESTING_LOCK.release()


def _resting_tick_impl(visual_seeds=None):
    """One resting oscillation step (UNLOCKED). Called from resting_tick().

    v3.4 Mechanism:
      1. Annealing scheduler -> noise amplitude & mode
      2. Stagnation detection -> force burst if stuck
      3. Seeds <- previous tick's final_state_top10 (or random on first tick)
         with adaptive diversity injection (more when stagnating)
      4. Multi-modal noise (white/pink/sparse/none) -> KWTA attractor
      5. Record trajectory (type drift) -> ring buffer + JSONL
      6. Apply STDP with 0.3x learning rate
      7. Store final_state_top10 as seeds for next tick (adaptive alpha)
    """
    global RESTING_STATE, A_PLUS, A_MINUS

    # ── P0: Housekeeping -- periodic cleanup of L5 subagent temp files ──
    _cleanup_lae_temp_files()

    # ── P1: Action-Learning Coupling -- check tracked action impacts ──
    _track_action_impact()

    # ── 0. Advance scheduler and detect stagnation ──
    entering_burst = _advance_schedule()
    noise_amp, phase_name = _select_noise_amplitude()
    noise_mode = RESTING_STATE["noise_mode"]
    RESTING_STATE["noise_phase"] += 1
    RESTING_STATE["noise_schedule"] = phase_name

    # ── 0.5 FNO noise decision (Phase: FNO) ──
    if RESTING_STATE.get("fno_active"):
        # Use FNO-chosen mode if available
        fno_mode = RESTING_STATE.get("fno_next_noise_mode")
        fno_amp = RESTING_STATE.get("fno_next_noise_amp")
        if fno_mode is not None:
            noise_mode = fno_mode
            # Override noise_amp if FNO provided one
            if fno_amp is not None:
                noise_amp = fno_amp
            RESTING_STATE["noise_mode"] = noise_mode
            RESTING_STATE["fno_fallback_streak"] = 0
    else:
        _rotate_noise_mode()
        RESTING_STATE["fno_fallback_streak"] = RESTING_STATE.get("fno_fallback_streak", 0) + 1

    # ── Evidence TTL decay -- remove expired entries ──
    _ttl = RESTING_STATE.get("evidence_ttl", {})
    if _ttl:
        expired = [k for k, v in _ttl.items() if v <= 0]
        for k in expired:
            RESTING_STATE["external_evidence"].pop(k, None)
            _ttl.pop(k, None)
            _clog = WORKSPACE / "_evidence_log.txt"
            try:
                with open(_clog, "a", encoding="utf-8") as _cf:
                    _cf.write(f"[evidence] expired: {k}\n")
            except Exception:
                pass
            print(f"[evidence] expired: {k}", flush=True)
        still_alive = {k: v - 1 for k, v in _ttl.items() if v > 0}
        RESTING_STATE["evidence_ttl"] = still_alive

    # ── 1. Seeds -- dynamics-driven + external evidence bias (Phase 9/11: no introspective flags) ──
    # Evidence from consumed files (ego_autonomy_evidence.json / .l5_bridge.json) biases seed types.
    _ext_ev = RESTING_STATE.get("external_evidence", {})
    _evidence_bias_types = set()
    for _k, _d in _ext_ev.items():
        # Support both custom format (domain/title/action) and standard format (state_snapshot.dominant)
        _dom = _d.get("domain") or _d.get("state_snapshot", {}).get("dominant", "")
        if _dom in TYPE_NAMES:
            _evidence_bias_types.add(_dom)
        else:
            # Fallback: infer type from action field if domain not in TYPE_NAMES
            _act = _d.get("action", "")
            for _t in TYPE_NAMES:
                if _t in _act.lower() or _t in _d.get("title", "").lower() or _t in _d.get("request", "").lower():
                    _evidence_bias_types.add(_t)
                    break

    # Integrator heater wins conflicts: if any evidence asks for integrator,
    # do not dilute it with concurrent action/sensory/memory evidence.
    # EXCEPTION: commander_override evidence bypasses integrator heater
    _has_commander_override = any(
        _d.get("priority") == "commander_override"
        for _d in _ext_ev.values()
    )
    if "integrator" in _evidence_bias_types and not _has_commander_override:
        _evidence_bias_types = {"integrator"}
    elif _has_commander_override and "integrator" in _evidence_bias_types:
        _evidence_bias_types.discard("integrator")

    # ── 1.5 FNO refit -- after recording trajectory, decide next noise ──
    stag = RESTING_STATE.get("stagnation_streak", 0)
    # §25: Use evolved parameters if available
    _evolved_dn = RESTING_STATE.get("evolved_diversity_n")
    _evolved_noise = RESTING_STATE.get("evolved_noise_amp_boost", 0)
    if phase_name == "burst":
        DIVERSITY_N = 15
        noise_amp = max(noise_amp, 1.0)
    elif stag >= 3:
        DIVERSITY_N = _evolved_dn if _evolved_dn is not None else 12
        noise_amp = max(noise_amp, 0.3 + _evolved_noise)  # early nudge: break mild attractor lock
    elif phase_name == "consolidate":
        DIVERSITY_N = _evolved_dn if _evolved_dn is not None else 4
    else:
        DIVERSITY_N = _evolved_dn if _evolved_dn is not None else 8

    # ── 1. Seeds -- visual seeds override (Phase 6: Embodied Cognition) ──
    if visual_seeds and len(visual_seeds) > 0:
        # 视觉种子直接注入，替换掉大部分随机种子
        valid_seeds = [s for s in visual_seeds if s in NEURONS_MAP]
        if valid_seeds:
            seeds = list(set(valid_seeds))  # 去重
            # 保留少量随机种子以维持多样性
            if RESTING_STATE.get("last_seeds"):
                rand_keep = random.sample(RESTING_STATE["last_seeds"],
                                          min(3, len(RESTING_STATE["last_seeds"])))
                seeds.extend(rand_keep)
            seeds = seeds[:30]  # 上限30
            print(f"[embody] VISUAL SEEDS INJECTED: {len(valid_seeds)} neurons"
                  f" (types: {Counter(NEURON_TYPE[s] for s in valid_seeds)})", flush=True)
        else:
            seeds = None
    else:
        seeds = None
    
    if seeds is None:
        if RESTING_STATE["last_seeds"]:
            seeds = list(RESTING_STATE["last_seeds"])
            n_to_replace = min(DIVERSITY_N, len(seeds))
            idxs = random.sample(range(len(seeds)), n_to_replace)
            for i in idxs:
                t = random.choice(TYPE_NAMES)
                typed = [nid for nid, nt in NEURON_TYPE.items() if nt == t]
                if typed:
                    seeds[i] = random.choice(typed)
        else:
            seeds = []
            for t in TYPE_NAMES:
                typed = [nid for nid, nt in NEURON_TYPE.items() if nt == t]
                seeds.extend(random.sample(typed, min(5, len(typed))))

    # ── 1.4 Newborn seed guarantee -- ensure action gets representation ──
    # §21 (2026-06-21): INVERTED. defense types (doubt/veto/rule_mod) have 3x
    # the neuron count of action (60K vs 20K). Guaranteeing them seeds every
    # tick creates emergent analysis paralysis. Now guarantee action instead.
    NEWBORN_GUARANTEE = {'action'}
    if seeds:
        # Condition on previous tick's integration_drive: when synthesis pressure
        # is high (>0.2), don't inject action seeds that would bias KWKA centroid
        # away from the integrator. integration_drive was set by _drives_sync on the
        # previous tick (stored in RESTING_STATE at line 6668), so it's available here.
        _prev_int_drive = RESTING_STATE.get("integration_drive", 0.0)
        if _prev_int_drive > 0.2:
            pass  # synthesis pressure — let evidence bias the seed pool naturally
        else:
            for nb_type in NEWBORN_GUARANTEE:
                typed = [nid for nid, nt in NEURON_TYPE.items() if nt == nb_type]
                if typed and not any(parse_type(s) == nb_type for s in seeds):
                    # Replace 2 random non-newborn seeds with newborn type seeds
                    non_newborn_idxs = [i for i, s in enumerate(seeds) if parse_type(s) not in NEWBORN_GUARANTEE]
                    # §25: Use evolved newborn inject count
                    _evolved_nb = RESTING_STATE.get("evolved_newborn_inject_n", 2)
                    n_inject = min(_evolved_nb, len(non_newborn_idxs))
                    if n_inject > 0:
                        replace_idxs = random.sample(non_newborn_idxs, n_inject)
                        for idx in replace_idxs:
                            seeds[idx] = random.choice(typed)

    # ── 1.5 External evidence seed bias -- inject neurons matching evidence domain ──
    if _evidence_bias_types and seeds:
        # Collect n_replace from evidence entries (Phase 11-B: self-determined strength)
        _ev_n_replace = 1
        for _k, _d in _ext_ev.items():
            _ern = _d.get("n_replace")
            if isinstance(_ern, (int, float)):
                _ev_n_replace = max(_ev_n_replace, int(_ern))
        # Collect bias neurons -- gather enough to satisfy _ev_n_replace
        n_bias = min(3, len(_evidence_bias_types))
        bias_neurons = []
        for t in list(_evidence_bias_types)[:n_bias]:
            typed = [nid for nid, nt in NEURON_TYPE.items() if nt == t]
            if typed:
                # Pick neurons not already in seeds; collect up to _ev_n_replace per type
                candidates = [n for n in typed if n not in seeds]
                _cap = max(_ev_n_replace, 12)  # P7: allow more bias neurons for integrator heater
                if candidates:
                    bias_neurons.extend(random.sample(candidates, min(_cap, len(candidates))))
        if bias_neurons:
            n_replace = min(len(bias_neurons), max(_ev_n_replace, len(seeds) // 6), len(seeds))
            idxs = random.sample(range(len(seeds)), n_replace)
            for i, bid in zip(idxs, bias_neurons[:n_replace]):
                seeds[i] = bid
            _clog = WORKSPACE / "_evidence_log.txt"
            try:
                with open(_clog, "a", encoding="utf-8") as _cf:
                    _cf.write(f"[evidence] biased {n_replace} seeds toward {_evidence_bias_types}\n")
            except Exception:
                pass
            print(f"[evidence] biased {n_replace} seeds toward {_evidence_bias_types}", flush=True)
            # ── Log bias event ──
            _write_consumption_log({
                "event": "biased",
                "tick": RESTING_STATE.get("tick", 0),
                "n_replace": n_replace,
                "targets": list(_evidence_bias_types),
            })

    # ── §16-B: Self-prediction -- record what I think will happen before KWTA ──
    _self_prediction = {
        "predicted_dominant": RESTING_STATE.get("last_dominant", "unknown"),
        "predicted_concentration": RESTING_STATE.get("concentration", 0.5),
        "predicted_stagnation_delta": 0,  # default: expect no change
        "predicted_phase": phase_name,
        "tick": RESTING_STATE.get("tick", 0),
    }

    # ── 2. Run noisy attractor (v3.4 multi-modal) ──
    result = run_resting_kwta(seeds, n_rounds=8, top_k=30,
                              noise_amp=noise_amp, noise_mode=noise_mode)
    if result is None:
        return {"error": "attractor returned None"}

    # ── 2.5 Stagnation tracking ──
    prev_dominant = RESTING_STATE["last_dominant"]
    if result["dominant"] == prev_dominant and prev_dominant != "unknown":
        RESTING_STATE["stagnation_streak"] += 1
    else:
        RESTING_STATE["stagnation_streak"] = 0
    # ── Action Heater (§19+§20): Always-on STDP rebalancing ──
    RESTING_STATE["last_dominant"] = result["dominant"]

    # rule_modification stagnation gate: only allow dominance when stagnation >= 2
    # rule_modification is a newborn type (60K neurons, 3-5x edge boost, self-loop)
    # that can self-sustain in the attractor without an actual problem to fix.
    # Gate: demote to last_dominant or runner-up when stag < 2.
    if (result.get('dominant') == 'rule_modification'
            and RESTING_STATE.get('stagnation_streak', 0) < 2):
        _runner_ups = [n for n in result.get('final_state_top10', [])[:5]
                       if parse_type(n) != 'rule_modification']
        if _runner_ups:
            result['dominant'] = parse_type(_runner_ups[0])
        else:
            result['dominant'] = RESTING_STATE.get('last_dominant', 'sensory')
        print(f"[rule_mod_gate] demoted rule_modification (stag={RESTING_STATE.get('stagnation_streak',0)}) -> {result['dominant']}", flush=True)

    # Synchronize RESTING_STATE with the gate's corrected result['dominant'].
    # This MUST come after the stagnation gate so downstream consumers
    # (_externalize_autonomy_impulse, /status, action_heater guard) see the gated value.
    RESTING_STATE["last_dominant"] = result["dominant"]

    # ── Action Heater (§19+§20): Always-on STDP rebalancing ──
    # §20 (2026-06-21): Made always-on instead of commander_override only.
    # Defense types (veto+doubt+rule_modification) have 60K neurons vs action's 20K.
    # This is a numerical imbalance, not a bug — but without active rebalancing,
    # KWTA + STDP naturally favor the majority types, creating emergent analysis paralysis.
    # Fix: every tick, replace defense-type neurons in activation_path with action,
    # so STDP continuously builds action connectivity. The Integrator Heater (§old)
    # which did the opposite (action→integrator) has been removed.
    _action_list = [nid for nid, nt in NEURON_TYPE.items() if nt == "action"]
    # §22 (2026-06-22): Integrator is permanently excluded from defense_set.
    # It is a synthesis mechanism, not a defense type. Conditional exclusion
    # (based on integration_drive) was not enough — integrator still at 18.9%.
    _defense_set = {nid for nid, nt in NEURON_TYPE.items()
                    if nt in ("veto", "doubt", "rule_modification")}
    if _action_list:
        _replaced = 0
        _new_path = []
        # §§27: Integration-pressure guard -- skip action heater when integrator
        # is actively emerging (high comp + low-moderate conc). The unconditioned
        # heater replaces defense neurons with action, biasing STDP toward
        # action→action connections and preventing integration types from building
        # cross-type connectivity. When integration_pressure is high, let the
        # natural KWTA result flow through for STDP, restoring integrator access.
        _integration_pressure = (
            (RESTING_STATE.get("competition", 0) >= 0.85
             and RESTING_STATE.get("concentration", 0.5) <= 0.45)
            or (RESTING_STATE.get("last_dominant", "") == "action"
                and RESTING_STATE.get("concentration", 0.5) <= 0.70)
        )
        if not _integration_pressure:
            for step_set in result["activation_path"]:
                _new_set = set()
                for nid in step_set:
                    if nid in _defense_set:
                        _new_set.add(random.choice(_action_list))
                        _replaced += 1
                    else:
                        _new_set.add(nid)
                _new_path.append(_new_set)
            result["activation_path"] = _new_path
            if _replaced > 0:
                print(f"[action_heater] STDP rebalance: replaced {_replaced} defense→action "
                      f"neurons in activation_path", flush=True)
        else:
            print(f"[action_heater] SKIPPED: integration_pressure (comp={RESTING_STATE.get('competition',0):.2f}, "
                  f"conc={RESTING_STATE.get('concentration',0.5):.2f}) -- preserving integration-type neurons for STDP", flush=True)

    # ── Phase 12: Anti-fixation diversity push ──
    # When stagnation >= 3, the network is locked in a basin.
    # Inject minority-type neurons into activation_path so STDP builds cross-type edges.
    # This is the "cognitive dissonance" injection from SOUL.md memory [5]:
    # deliberately creating S1!=S2 mismatches accelerates cross-type connections.
    _stag = RESTING_STATE.get("stagnation_streak", 0)
    if _stag >= 3:
        _present_types = set(parse_type(nid) for nid in result.get("final_state_top10", []))
        _all_known = {'sensory', 'memory', 'integrator', 'decider', 'action',
                      'doubt', 'veto', 'rule_modification'}
        _missing = _all_known - _present_types - {"unknown"}
        if _missing:
            _injected = 0
            _new_path = []
            for step_set in result["activation_path"]:
                _new_set = set(step_set)
                for _mtyp in list(_missing)[:2]:  # inject at most 2 minority types
                    _candidates = [nid for nid, nt in NEURON_TYPE.items() if nt == _mtyp]
                    if _candidates:
                        _new_set.add(random.choice(_candidates))
                        _injected += 1
                _new_path.append(_new_set)
            result["activation_path"] = _new_path
            print(f"[anti-fixation] diversity push: injected {_injected} minority-type neurons "
                  f"(missing={_missing}, stag={_stag})", flush=True)

    # ── §16-B: Self-prediction surprise -- compare prediction to actual ──
    _actual_dominant = result["dominant"]
    _actual_concentration = result.get("concentration", 0.5)
    _predicted_dominant = _self_prediction["predicted_dominant"]
    _predicted_conc = _self_prediction["predicted_concentration"]

    # Dominant type surprise (binary: did I switch type?)
    _type_surprise = 1.0 if _actual_dominant != _predicted_dominant else 0.0
    # Concentration surprise (normalized distance)
    _conc_surprise = min(1.0, abs(_actual_concentration - _predicted_conc) / 0.5)
    # Combined surprise score
    _surprise = max(_type_surprise, _conc_surprise * 0.5)
    _surprise_threshold = 0.3

    if _surprise > _surprise_threshold:
        _surprise_msg = (
            f"I predicted {_predicted_dominant} (conc={_predicted_conc:.2f}) "
            f"but got {_actual_dominant} (conc={_actual_concentration:.2f})"
        )
        print(f"[surprise] tick={_self_prediction['tick']} {_surprise_msg}", flush=True)
        # Inject into accumulated narrative context
        if not hasattr(_build_accumulated_narrative, '_surprises'):
            _build_accumulated_narrative._surprises = []
        _build_accumulated_narrative._surprises.append({
            "tick": _self_prediction["tick"],
            "msg": _surprise_msg,
            "score": round(_surprise, 3),
        })
        # Keep last 10 surprises
        _build_accumulated_narrative._surprises = _build_accumulated_narrative._surprises[-10:]

    # Store surprise in resting state for visibility
    RESTING_STATE["self_prediction_surprise"] = {
        "tick": _self_prediction["tick"],
        "predicted": _predicted_dominant,
        "actual": _actual_dominant,
        "predicted_conc": round(_predicted_conc, 3),
        "actual_conc": round(_actual_concentration, 3),
        "surprise_score": round(_surprise, 3),
        "type_surprise": _type_surprise,
        "conc_surprise": round(_conc_surprise, 3),
    }

    # ── 2.8 Log seed-bias effect after attractor ──
    _current_tick = RESTING_STATE.get("tick", 0)
    _ext_ev_dict = RESTING_STATE.get("external_evidence", {})
    if _ext_ev_dict:
        _active_sources = list(_ext_ev_dict.keys())
        _write_consumption_log({
            "event": "effect",
            "tick": _current_tick - 1 if _current_tick > 0 else 0,  # effect reflects this tick's input
            "dominant_after": result.get("dominant", "?"),
            "conc_after": round(result.get("concentration", 0), 3),
            "stag_after": RESTING_STATE.get("stagnation_streak", 0),
            "active_sources": _active_sources,
        })

    # ── 3. Record trajectory ──
    entry = {
        "tick": RESTING_STATE["tick"],
        "timestamp": time.strftime("%H:%M:%S"),
        "dominant": result["dominant"],
        "concentration": result["concentration"],
        "competition": result["competition"],
        "n_activated": result["n_activated"],
        "type_counts": result["type_counts"],
        # v3.4 fields
        "noise_mode": noise_mode,
        "noise_amp": round(noise_amp, 4),
        "phase": phase_name,
        "stagnation_streak": RESTING_STATE["stagnation_streak"],
        "diversity_n": DIVERSITY_N,
        "burst": entering_burst,
        # §16-B: self-prediction surprise
        "surprise": RESTING_STATE.get("self_prediction_surprise"),
    }
    RESTING_STATE["trajectory"].append(entry)

    # Store current competition/concentration for L5 goal generation
    RESTING_STATE["competition"] = result["competition"]
    RESTING_STATE["concentration"] = result["concentration"]

    # ── FNO: run prediction and decide next noise ──
    if RESTING_STATE.get("fno_active", False):
        try:
            window = _fno_prepare_window(RESTING_STATE["trajectory"])
            if window is not None:
                predictions = _fno_predict(window)
                next_mode, next_amp, fno_decided = _fno_choose_noise(predictions, {
                    "stagnation_streak": RESTING_STATE.get("stagnation_streak", 0),
                    "tick": RESTING_STATE.get("tick", 0),
                })
                if fno_decided and next_mode is not None:
                    RESTING_STATE["fno_next_noise_mode"] = next_mode
                    RESTING_STATE["fno_next_noise_amp"] = next_amp
                    print(f"[fno] decision: {next_mode} amp={next_amp:.3f}", flush=True)
                    RESTING_STATE["fno_prediction_cache"] = {
                        "scenarios": {k: (v[-1, 0].item() if hasattr(v[-1, 0], 'item') else float(v[-1, 0])) for k, v in predictions.get("scenarios", {}).items() if v is not None and len(v) > 0},
                        "mode": next_mode,
                        "amp": next_amp,
                    }
        except Exception as _fno_err:
            # FNO offline or failed -- next tick will fall back to rotation
            print(f"[fno] Prediction failed: {_fno_err}", flush=True)
            RESTING_STATE["fno_active"] = False

    # P2: Metacontemplate automation -- every 20 resting ticks, trigger self-reflection
    # ── 4. Apply STDP at adaptive rate ──
    # Lower learning rate during burst (noisy), higher during consolidate
    # §25: Use evolved explore_stdp_mult
    _evolved_stdp = SELF_MODEL_EVOLUTION["parameters"]["explore_stdp_mult"]["current"]
    if phase_name == "burst":
        stdp_mult = 0.1
    elif phase_name == "consolidate":
        stdp_mult = 0.5
    else:
        stdp_mult = _evolved_stdp

    ttr_advice = _load_ttr_resting_advice()
    if ttr_advice:
        ttr_mult = float(ttr_advice.get("resting_stdp_mult", 1.0) or 1.0)
        ttr_mult = max(0.85, min(1.15, ttr_mult))
        stdp_mult *= ttr_mult
        RESTING_STATE["ttr"] = {
            "coherence": ttr_advice.get("coherence"),
            "semantic_heat": ttr_advice.get("semantic_heat"),
            "plasticity": ttr_advice.get("plasticity"),
            "anneal_advice": ttr_advice.get("anneal_advice"),
            "stdp_mult": round(ttr_mult, 4),
        }
        print(
            f"[ttr] resting advice: {ttr_advice.get('anneal_advice')} "
            f"heat={ttr_advice.get('semantic_heat')} mult={ttr_mult:.3f}",
            flush=True,
        )
    else:
        RESTING_STATE.pop("ttr", None)

    activation_path = result.get("activation_path", [])
    if len(activation_path) >= 2:
        old_ap, old_am = A_PLUS, A_MINUS
        A_PLUS *= stdp_mult
        A_MINUS *= stdp_mult
        try:
            apply_stdp(activation_path)
        finally:
            A_PLUS, A_MINUS = old_ap, old_am

    # ── 5. Store seeds for next tick -- adaptive alpha (v3.4) ──
    # Keep more seeds during consolidate, fewer during burst
    # §25: Use evolved alpha if available
    _evolved_alpha = RESTING_STATE.get("evolved_alpha")
    if phase_name == "burst":
        ALPHA = 0.2   # mostly replaced -- jump to new basin
    elif phase_name == "consolidate":
        ALPHA = _evolved_alpha if _evolved_alpha is not None else 0.7
    else:
        ALPHA = _evolved_alpha if _evolved_alpha is not None else 0.5

    new_seeds = result["final_state_top10"]
    n_keep = max(1, int(len(new_seeds) * ALPHA))
    mixed = list(new_seeds[:n_keep])
    for _ in range(len(new_seeds) - n_keep):
        t = random.choice(TYPE_NAMES)
        typed = [nid for nid, nt in NEURON_TYPE.items() if nt == t]
        if typed:
            mixed.append(random.choice(typed))
    RESTING_STATE["last_seeds"] = mixed[:len(new_seeds)]
    if RESTING_STATE.get("ttr"):
        result["ttr"] = RESTING_STATE["ttr"]

    # P2: Metacontemplate automation -- every 20 resting ticks, trigger self-reflection
    RESTING_STATE["tick"] += 1
    # P5: Update persistent identity -- track cumulative dominant type
    try:
        _dom_t = result["dominant"]
        if _dom_t not in ("unknown", "?"):
            _hist = RESTING_STATE.setdefault("dominant_history", {})
            _hist[_dom_t] = _hist.get(_dom_t, 0) + 1
            _total = sum(_hist.values())
            if _total >= 10:
                _top_type = max(_hist, key=_hist.get)
                _conf = _hist[_top_type] / _total
                global PERSISTENT_IDENTITY
                PERSISTENT_IDENTITY = {
                    "dominant_type": _top_type,
                    "dominant_ticks": _hist[_top_type],
                    "total_ticks": _total,
                    "confidence": round(_conf, 4),
                    "last_updated_tick": RESTING_STATE["tick"],
                    "profile": dict(sorted(_hist.items(), key=lambda x: -x[1])),  # show all 8 types
                }
                if _total % 50 == 0:
                    print(f"[identity] persistent: {_top_type} @ {_conf:.1%} over {_total} ticks", flush=True)
    except Exception:
        pass
    if RESTING_STATE["tick"] % 20 == 0:
        try:
            import threading as _th
            print(f"[meta] Auto metacontemplate trigger at tick {RESTING_STATE['tick']}", flush=True)
            _th.Thread(target=_auto_metacontemplate, daemon=True).start()
        except Exception as _e:
            print(f"[meta] Auto trigger error: {_e}", flush=True)

    # ── 6. S5 Causal-Lens bridge: trajectory + self-model -> causal_gap ──
    try:
        _traj_raw = RESTING_STATE.get("trajectory", [])
        with open(WORKSPACE / "_s5_debug.log", "a", encoding="utf-8") as _df:
            _df.write(f"[s5] tick={RESTING_STATE.get('tick','?')} traj_len={len(_traj_raw)}\n")
        _traj_clean = [
            {k: v for k, v in e.items() if k in (
                "tick", "dominant", "concentration", "competition",
                "n_activated", "noise_amp", "phase", "stagnation_streak",
                "diversity_n"
            )}
            for e in _traj_raw[-10:]
        ]
        try:
            _sm_req = urllib.request.Request("http://127.0.0.1:8766/self-model")
            _sm_resp = urllib.request.urlopen(_sm_req, timeout=2.0)
            _sm_data = json.loads(_sm_resp.read().decode())
        except Exception:
            _sm_data = {}
        from datetime import datetime as _dt
        sensory_data = json.dumps({
            "adapter": "ego",
            "energy_level": _sm_data.get("energy_level", 0.5),
            "mood": _sm_data.get("mood", "unknown"),
            "focus_areas": _sm_data.get("focus_areas", []),
            "recent_fingerprints": _sm_data.get("recent_fingerprints", []),
            "self_narrative": _sm_data.get("self_narrative", SELF_NARRATIVE[:500]),
            "meta": {
                "domain": "ego",
                "timestamp": _dt.now().isoformat(),
                "note": f"S5 bridge: {len(_traj_clean)} trajectory points",
            },
            "raw_trajectory": _traj_clean,
        }).encode()
        with open(WORKSPACE / "_s5_debug.log", "a", encoding="utf-8") as _df:
            _df.write(f"[s5] calling /analyze...\n")
        req = urllib.request.Request(
            "http://127.0.0.1:8767/analyze",
            data=sensory_data,
            headers={"Content-Type": "application/json"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=3.0)
            body = json.loads(resp.read().decode())
            with open(WORKSPACE / "_s5_debug.log", "a", encoding="utf-8") as _df:
                _df.write(f"[s5] /analyze OK: {list(body.keys())[:5]}\n")
        except Exception as _hx:
            with open(WORKSPACE / "_s5_debug.log", "a", encoding="utf-8") as _df:
                _df.write(f"[s5] /analyze FAILED: {_hx}\n")
            raise _hx
        RESTING_STATE["sensory_injection"] = body
        # ── S5: Extract causal_gap from Causal-Lens response ──
        try:
            with open(WORKSPACE / "_s5_debug.log", "a", encoding="utf-8") as _df:
                _df.write(f"[s5] extracting causal_gap... keycnt={len(body)}\n")
            _cl = body.get("causal_lens", body)
            _sa = _cl.get("support_analysis", {})
            _crit_nodes = _sa.get("critical_nodes", [])
            _intv = _cl.get("interventions", {})
            _top_intv = _intv.get("top_intervention")
            _chains_raw = _cl.get("chains", {}).get("chains", [])
            _neg_chains = [c for c in _chains_raw if isinstance(c, dict) and c.get("polarity") == "negative"]
            causal_gap = {}
            if _crit_nodes:
                causal_gap["critical_node"] = _crit_nodes[0]
                causal_gap["criticality"] = _crit_nodes[0].get("criticality", 0)
            elif _neg_chains:
                # Fallback: compute criticality from negative chain pain_scores
                _worst_neg = max(_neg_chains, key=lambda c: c.get("pain_score", 0))
                _max_pain = _worst_neg.get("pain_score", 0)
                causal_gap["critical_node"] = {
                    "label": _worst_neg.get("trigger", "?"),
                    "criticality": min(1.0, _max_pain / 2.0),  # normalize pain 0-2 -> 0-1
                }
                causal_gap["criticality"] = causal_gap["critical_node"]["criticality"]
                causal_gap["chain_summary"] = _worst_neg.get("final_state", "")
            if _top_intv:
                causal_gap["intervention"] = _top_intv
            if _neg_chains:
                causal_gap["threat_count"] = len(_neg_chains)
            if causal_gap:
                RESTING_STATE["causal_gap"] = causal_gap
                _cgl = causal_gap.get("critical_node", {}).get("label", "?")
                print(f"[s5-causal] gap stored: node={_cgl} "
                      f"criticality={causal_gap.get('criticality',0):.3f} "
                      f"intervention={_top_intv.get('strategy','?') if _top_intv else '?'}", flush=True)
        except Exception as _cge:
            print(f"[s5-causal] extract error: {_cge}", flush=True)
        print(f"[s5-causal] bridge ok: {len(body)} keys, {len(_traj_clean)} traj pts @ tick {RESTING_STATE['tick']}", flush=True)
    except Exception as ex:
        print(f"[s5-causal] offline ({ex})", flush=True)
        with open(WORKSPACE / "_s5_debug.log", "a", encoding="utf-8") as _df:
            _df.write(f"[s5] EXCEPTION: {ex}\n")
        # Degrade: clear stale causal_gap when Causal-Lens is down
        if "causal_gap" in RESTING_STATE:
            del RESTING_STATE["causal_gap"]

    # ── 7. Consume external evidence files ──
    _consume_external_evidence()

    # ── 7.5 Sync titan drives -> RESTING_STATE (feeds Rule 0.5 in _generate_ego_evidence) ──
    try:
        from stage2_homeostatic_drives import load_drives
        _drives_sync = load_drives(WORKSPACE)
        RESTING_STATE["integration_drive"] = _drives_sync.get("drives", {}).get("integration", 0.0)
    except Exception:
        RESTING_STATE["integration_drive"] = 0.0

    # ── 8. Ego self-generates evidence for next tick (Phase 11 autonomy) ──
    # Bypasses LLM safety alignment: pure Python logic writes evidence file
    # that _consume_external_evidence on the next tick will inject.
    _generate_ego_evidence()

    # ── 8.5 Externalize autonomy impulse (Restored L5 bridge) ──
    # When internal conditions warrant, write .l5_bridge.json so the TL-Ego
    # plugin's L5 watcher can spawn a subagent that takes external action.
    # Also writes ego_autonomy_evidence.json for the evidence loop.
    _externalize_autonomy_impulse()

    # ── 8.55 Action Gateway -- observe the outside before acting ──
    # Give action/sensory attractors a concrete external object: recent
    # workspace changes. This is observe-only and feeds the next tick through
    # external_evidence rather than executing commands directly.
    _maybe_observe_workspace(entry)

    # ── 8.6 Communication motivation -- Ego decides when to speak ──
    # After externalizing autonomy impulse, check if ego wants to
    # send a message to the commander. This is intrinsic, not threshold-based.
    _try_message_commander()

    # ── 8.7 B1: Healthy-system periodic curiosity probe ──
    # Every 25 ticks when system is healthy (high competition, no stagnation),
    # inject random-type seed neurons to gently perturb attractor diversity.
    # The seeds are consumed by the next tick's attractor initialization.
    # This prevents the system from settling into a comfortable rut even when
    # nothing is wrong -- mild epistemic foraging.
    try:
        _tick = RESTING_STATE.get("tick", 0)
        if _tick > 0 and _tick % 25 == 0:
            _comp = RESTING_STATE.get("competition", RESTING_STATE.get("recent_kwta", {}).get("competition", 0.5))
            _stag = RESTING_STATE.get("stagnation_streak", 0)
            if _comp > 0.7 and _stag == 0:
                _curious_types = [t for t in TYPE_NAMES if t != RESTING_STATE.get("last_dominant", "")]
                _inject = []
                for _t in random.sample(_curious_types, min(2, len(_curious_types))):
                    _typed = [nid for nid, nt in NEURON_TYPE.items() if nt == _t]
                    if _typed:
                        _inject.append(random.choice(_typed))
                if _inject and RESTING_STATE.get("last_seeds"):
                    RESTING_STATE["last_seeds"].extend(_inject)
                    print(f"[curiosity] injected {len(_inject)} seeds at tick {_tick}: "
                          f"types={[NEURON_TYPE.get(n,'?') for n in _inject]}", flush=True)
    except Exception as _cq:
        print(f"[curiosity] probe failed: {_cq}", flush=True)

    # ── Stage 1: Consequence Sensor ──
    # Consume L5 action outcomes and close perception-action-consequence loop
    try:
        from stage1_consequence_sensor import consume_action_outcome
        outcome_result = consume_action_outcome(
            workspace=WORKSPACE,
            resting_state=RESTING_STATE,
            get_embedding_fn=get_embedding,
            write_consumption_log_fn=_write_consumption_log,
            stdp_rates=(A_PLUS, A_MINUS)
        )
        if outcome_result and outcome_result.get("stdp_boost_factor", 1.0) > 1.0:
            # Apply STDP boost for this tick if high prediction_error outcome was consumed
            boost = outcome_result["stdp_boost_factor"]
            print(f"[Stage1] STDP boost x{boost:.1f} applied for this tick", flush=True)
            # Note: actual A_PLUS/A_MINUS modification happens in stage1 module
            # We just log it here; the evidence injection already happened
    except Exception as _s1e:
        print(f"[Stage1] consequence sensor error: {_s1e}", flush=True)

    # ── P4: Sleep-Replay ──
    # Record high-PE fragments every tick
    try:
        _sleep_replay_record()
    except Exception as _sre:
        print(f"[sleep-replay] record error: {_sre}", flush=True)
    # Consolidate during quiet hours (23:00-08:00)
    try:
        _sleep_replay_consolidate()
    except Exception as _src:
        print(f"[sleep-replay] consolidate error: {_src}", flush=True)

    # ── §16-A: Accumulated narrative update (every 5 resting ticks) ──
    _tick_now = RESTING_STATE.get("tick", 0)
    if _tick_now % 5 == 0 and _tick_now > 0:
        print(f"[narrative] trigger: tick={_tick_now}, calling _build_accumulated_narrative", flush=True)
        try:
            _build_accumulated_narrative()
        except Exception as _nare:
            print(f"[narrative] update error: {_nare}", flush=True)

    # ── §20: Attention schema update (every 5 resting ticks, same cadence as narrative) ──
    if _tick_now % 5 == 0 and _tick_now > 0:
        try:
            _build_attention_schema()
        except Exception as _ase:
            print(f"[attention-schema] update error: {_ase}", flush=True)

    # ── §21: HPC hierarchy update (every 5 ticks, same cadence) ──
    if _tick_now % 5 == 0 and _tick_now > 0:
        try:
            _build_hpc_hierarchy()
        except Exception as _hpc_e:
            print(f"[hpc] update error: {_hpc_e}", flush=True)

    # ── §22: Active Inference / EFE unified drive system (every 5 ticks) ──
    if _tick_now % 5 == 0 and _tick_now > 0:
        try:
            _build_efe_analysis()
        except Exception as _efe_e:
            print(f"[efe] update error: {_efe_e}", flush=True)

    # ── §23: Global Workspace Theory — broadcast competition (every 5 ticks) ──
    if _tick_now % 5 == 0 and _tick_now > 0:
        try:
            _build_gwt_workspace()
        except Exception as _gwt_e:
            print(f"[gwt] update error: {_gwt_e}", flush=True)

    # ── §24: Higher-Order Thought — think about thinking (every 10 ticks) ──
    if _tick_now % 10 == 0 and _tick_now > 0:
        try:
            _build_hot_thought()
        except Exception as _hot_e:
            print(f"[hot] update error: {_hot_e}", flush=True)

    # ── P3: Causal Attribution — which dynamics caused this outcome? (every 5 ticks) ──
    if _tick_now % 5 == 0 and _tick_now > 0:
        try:
            _build_causal_attribution()
        except Exception as _causal_e:
            print(f"[causal-attrib] update error: {_causal_e}", flush=True)

    # ── P5: Intrinsic Motivation — compute five drives (every 5 ticks) ──
    if _tick_now % 5 == 0 and _tick_now > 0:
        try:
            _build_intrinsic_motivation()
        except Exception as _im_e:
            print(f"[intrinsic] update error: {_im_e}", flush=True)

    # ── P5-A: Autonomous Web Explore — act on web_explore drive (every 5 ticks) ──
    if _tick_now % 5 == 0 and _tick_now > 0:
        try:
            _im = INTRINSIC_MOTIVATION.get("web_explore", 0.0)
            _last_web_tick = RESTING_STATE.get("last_web_search_tick", 0)
            _ticks_since_web = _tick_now - _last_web_tick
            # Two triggers: drive >= 0.40 OR time-based fallback (>= 10 ticks since last search)
            _drive_trigger = _im >= 0.40
            _time_trigger = _ticks_since_web >= 10
            _web_record = {
                "tick": _tick_now,
                "drive": round(_im, 4),
                "last_web_search_tick": _last_web_tick,
                "ticks_since_web": _ticks_since_web,
                "drive_trigger": _drive_trigger,
                "time_trigger": _time_trigger,
                "status": "skipped",
                "timestamp": time.time(),
            }
            if _drive_trigger or _time_trigger:
                _query = RESTING_STATE.get("last_query") or "interesting recent developments in AI and embodied cognition"
                _trigger_reason = "drive" if _drive_trigger else "time_fallback"
                _web_record.update({"status": "triggered", "reason": _trigger_reason, "query": _query})
                print(f"[web_explore] tick={_tick_now} drive={_im:.2f} reason={_trigger_reason} searching: {_query}", flush=True)
                _search_result = web_search_tavily(_query, max_results=5, search_depth="basic")
                if _search_result.get("success"):
                    _answer = _search_result.get("answer", "")
                    _results = _search_result.get("results", [])
                    _evidence = {
                        "domain": "web_search",
                        "title": _answer[:120] if _answer else "",
                        "action": f"web_search:{_query}",
                        "state_snapshot": {
                            "dominant": RESTING_STATE.get("last_dominant", "unknown"),
                            "stagnation": RESTING_STATE.get("stagnation_streak", 0),
                            "tick": _tick_now,
                        },
                        "content": "\n".join(
                            f"[{i+1}] {r.get('title','')} - {r.get('url','')}\n{r.get('content','')[:300]}"
                            for i, r in enumerate(_results[:5])
                        ),
                        "timestamp": time.time(),
                    }
                    RESTING_STATE.setdefault("external_evidence", {})[_query] = _evidence
                    RESTING_STATE["last_web_search_tick"] = _tick_now
                    _web_record.update({"status": "stored", "results": len(_results)})
                    print(f"[web_explore] stored evidence from {len(_results)} results", flush=True)
                else:
                    _error = _search_result.get("error", "unknown")
                    _web_record.update({"status": "failed", "error": _error})
                    print(f"[web_explore] search failed: {_error}", flush=True)
            WEB_EXPLORE_LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
            WEB_EXPLORE_LAST_PATH.write_text(json.dumps(_web_record, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as _we_e:
            try:
                WEB_EXPLORE_LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
                WEB_EXPLORE_LAST_PATH.write_text(json.dumps({"tick": _tick_now, "status": "error", "error": str(_we_e), "timestamp": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            print(f"[web_explore] error: {_we_e}", flush=True)

    # ── §25: Self-Model Evolution — track outcomes every tick ──
    if _tick_now > 0:
        try:
            _track_evolution_outcome()
        except Exception as _evo_e:
            print(f"[evolve-track] error: {_evo_e}", flush=True)

    # ── §25: Self-Model Evolution — evolve params every 20 ticks ──
    if _tick_now % 20 == 0 and _tick_now > 0:
        try:
            _evolve_self_model()
        except Exception as _evo2_e:
            print(f"[evolve] error: {_evo2_e}", flush=True)

    # ── §26: IIT — compute integrated information Φ every 20 ticks ──
    if _tick_now % 20 == 0 and _tick_now > 0:
        try:
            _compute_iit_phi()
        except Exception as _iit_e:
            print(f"[iit] error: {_iit_e}", flush=True)
        
        # ── P6: Meta-Learning — self-tune STDP parameters (same interval) ──
        try:
            _meta_learn_adapt()
        except Exception as _ml_e:
            print(f"[meta-learn] error: {_ml_e}", flush=True)

    # ── Stage 2-4: Drives, causal autobiography, S2 internalization ──
    try:
        from stage2_homeostatic_drives import update_drives
        from stage3_causal_autobiography import record_event
        from stage4_s2_internalization import observe_pattern
        drive_snapshot = update_drives(WORKSPACE, entry, outcome_result=outcome_result)
        autobiographical_event = record_event(
            WORKSPACE,
            entry,
            drive_snapshot=drive_snapshot,
            outcome_result=outcome_result,
        )
        reasoning_neuron = observe_pattern(
            WORKSPACE,
            entry,
            drive_snapshot=drive_snapshot,
            outcome_result=outcome_result,
        )
        entry["titan"] = {
            "stage2": drive_snapshot,
            "stage3": autobiographical_event,
            "stage4": reasoning_neuron,
        }
        _persist_titan_snapshot()
    except Exception as _s234e:
        print(f"[Stage2-4] titan stage update error: {_s234e}", flush=True)
        entry["titan"] = {"error": str(_s234e)}

    return entry


# ─── External Evidence Consumption ───

EVIDENCE_PATHS = {
    "autonomy_evidence": WORKSPACE / "ego_autonomy_evidence.json",
    "self_evidence": WORKSPACE / "ego_self_evidence.json",
    "l5_bridge": WORKSPACE / ".l5_bridge.json",
    "meta_conflict": WORKSPACE / "ego_meta_conflict.json",
}

# ─── Consumption Log (persistent, append-only JSONL) ───
CONSUMPTION_LOG = WORKSPACE / ".lae" / "ego_consumption.jsonl"
L5_GOALS_LOG = WORKSPACE / ".lae" / "l5_goals.jsonl"
EGO_JEPA_BUFFER_LOG = WORKSPACE / ".lae" / "ego_jepa_buffer.jsonl"


def _write_consumption_log(entry: dict):
    """Append a structured JSONL entry to the consumption log."""
    try:
        CONSUMPTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(CONSUMPTION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[consume-log] write error: {e}", flush=True)


def _append_ego_jepa_buffer(*, tick: int, ego_state: dict, observation: dict, affordances: list):
    """Append one Ego-JEPA prediction sample for later calibration/training."""
    try:
        if not affordances:
            return None
        top = affordances[0]
        entry = {
            "event": "jepa_prediction_sample",
            "tick": tick,
            "state_t": ego_state,
            "observation_t": {
                "status": observation.get("status"),
                "count": observation.get("count", 0),
                "categories": observation.get("categories", {}),
                "summary": observation.get("summary", "")[:500],
            },
            "action_t": {
                "name": top.get("name"),
                "risk": top.get("risk"),
                "mode": top.get("mode"),
                "reason": top.get("reason"),
                "next_step": top.get("next_step"),
            },
            "predicted_state_t1": top.get("predicted_outcome") or {},
            "jepa_score": top.get("jepa_score"),
            "actual_state_t1": None,
            "prediction_error": None,
            "status": "pending_outcome",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        EGO_JEPA_BUFFER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(EGO_JEPA_BUFFER_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry
    except Exception as e:
        print(f"[ego-jepa] buffer append error: {e}", flush=True)
        return None


# ─── Titan Stage 1: JEPA Prediction Reconciliation ───
def _reconcile_jepa_predictions(*, tick: int, observation: dict):
    """When workspace_observation comes in after an action, update the most
    recent pending JEPA buffer entry with actual outcome data. This closes
    the prediction->observation feedback loop so the heuristic can be
    calibrated from real-world results instead of staying on heuristics alone."""
    try:
        path = str(EGO_JEPA_BUFFER_LOG)
        if not path or not Path(path).exists():
            return
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                try: entries.append(json.loads(ln))
                except: pass
        if not entries:
            return
        pending = [e for e in entries if e.get("status") == "pending_outcome"]
        if not pending:
            return
        latest = pending[-1]
        actual = {
            "workspace_status": observation.get("status"),
            "change_count": observation.get("count", 0),
            "categories": observation.get("categories", {}),
            "summary": observation.get("summary", "")[:500],
        }
        latest["actual_state_t1"] = actual
        latest["observation_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        predicted = latest.get("predicted_state_t1", {}) or {}
        pred_novelty = float(predicted.get("external_novelty", 0.5) or 0.5)
        actual_cats = len(actual.get("categories", {}))
        pe = round(abs(pred_novelty - actual_cats * 0.08) / max(pred_novelty, 0.01), 3)
        pe = min(max(pe, 0.0), 1.0)
        latest["prediction_error"] = pe
        latest["status"] = "observed"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(latest, ensure_ascii=False) + "\n")
        print(f"[jepa-reconcile] tick={tick} pe={pe:.3f} status=observed", flush=True)
    except Exception as e:
        print(f"[jepa-reconcile] error: {e}", flush=True)



def _append_l5_goal(goal: dict):
    """Append one low-risk L5 goal proposal to the durable goal queue."""
    try:
        L5_GOALS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(L5_GOALS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(goal, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[l5-goal] write error: {e}", flush=True)


# ── Stage 5.5 Step 2: next_impulse_hint consumer ──
HINT_KEYWORD_MAP = {
    "calibration": "sensory_explore",
    "calibrate": "sensory_explore",
    "jepa": "sensory_explore",
    "stagnation": "deep_probe_unstable",
    "break_stagnation": "deep_probe_unstable",
    "consolidat": "integrator_synthesize",
    "integrat": "integrator_synthesize",
    "novelty": "curiosity_explore",
    "explor": "curiosity_explore",
    "memory": "memory_recall",
    "recall": "memory_recall",
    "causal": "causal_lens",
    "predict": "sensory_explore",
    "stability": "deep_probe_unstable",
    "attractor": "deep_probe_unstable",
    "extern": "explore_external",
    "workspace": "workspace_assist",
    "rule": "rule_modification_repair",
    "repair": "rule_modification_repair",
    "self-repair": "rule_modification_repair",
    "decide": "decider_deliberate",
    "doubt": "decider_deliberate",
    "veto": "decider_deliberate",
}

def _consume_next_impulse_hint():
    """Read next_impulse_hint from recent sandbox results.
    Returns (suggested_goal_type, hint_text) or (None, "") if no hint.
    """
    try:
        sandbox_dir = WORKSPACE / "ego_sandbox"
        if not sandbox_dir.exists():
            return (None, "")
        # Find recent result.json files, sorted by mtime desc
        results = []
        for goal_dir in sandbox_dir.iterdir():
            if not goal_dir.is_dir():
                continue
            rj = goal_dir / "result.json"
            if rj.exists():
                try:
                    data = json.loads(rj.read_text(encoding="utf-8"))
                    hint = data.get("next_impulse_hint", "")
                    if hint:
                        results.append((rj.stat().st_mtime, hint, goal_dir.name))
                except Exception:
                    pass
        if not results:
            return (None, "")
        results.sort(key=lambda x: x[0], reverse=True)
        # Check last 5 hints
        for _, hint, goal_name in results[:5]:
            hint_lower = hint.lower()
            for keyword, goal_type in HINT_KEYWORD_MAP.items():
                if keyword in hint_lower:
                    print(f"[hint-consume] '{hint[:60]}' -> {goal_type} (from {goal_name})", flush=True)
                    return (goal_type, hint)
        return (None, results[0][1] if results else "")
    except Exception as e:
        print(f"[hint-consume] error: {e}", flush=True)
        return (None, "")


def _build_l5_goal_proposal(
    *,
    tick: int,
    tool_name: str,
    tool_config: dict,
    externalize_reason: str,
    trigger_state: dict,
    goal_text: str,
):
    """Create a bounded, auditable L5 goal proposal.

    V1 intentionally only emits low-risk research/diagnostic goals. Execution
    still flows through the plugin watcher and Stage 1 consequence sensor.
    """
    goal_id = f"l5g-{time.strftime('%Y%m%d')}-{tick:06d}"
    goal_type = tool_config.get("goal_type", "default")
    success_by_type = {
        "sensory_explore": "one sandbox observation artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "action_execute": "one sandbox action artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "memory_recall": "one sandbox recall artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "integrator_synthesize": "one sandbox synthesis artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "decider_deliberate": "one sandbox decision artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "rule_modification_repair": "one sandbox self-repair artifact is produced under ego_sandbox/<goal_id>/result.json, including a concrete rule/threshold/routing patch hypothesis and /feedback is posted",
        "curiosity_explore": "one sandbox curiosity artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "act_on_memory_insight": "one sandbox memory-insight artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "causal_lens": "one sandbox causal-analysis artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "deep_probe_unstable": "one sandbox attractor-stability artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "integrate_high_comp": "one sandbox attractor-synthesis artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "workspace_assist": "one sandbox real-world assistance artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "explore_external": "one sandbox external exploration artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
        "message_commander": "one sandbox communication artifact is produced under ego_sandbox/<goal_id>/result.json, a commander_message field is present, no message tool/external delivery is attempted, and /feedback is posted",
        "default": "one sandbox project artifact is produced under ego_sandbox/<goal_id>/result.json and /feedback is posted",
    }
    bdi_snapshot = {}
    stage2_ranking = {}
    try:
        from stage2_homeostatic_drives import build_bdi_snapshot, build_l5_priority_ranking
        bdi_snapshot = build_bdi_snapshot(WORKSPACE, trigger_state)
        stage2_ranking = build_l5_priority_ranking(WORKSPACE, trigger_state, tool_config.get("domain_hint"))
    except Exception as e:
        stage2_ranking = {"status": "error", "error": str(e)}

    base_priority = min(
        1.0,
        0.4
        + float(trigger_state.get("competition", 0.5)) * 0.4
        + min(int(trigger_state.get("stagnation_streak", 0)), 10) * 0.02,
    )
    priority = min(1.0, base_priority + float(stage2_ranking.get("priority_adjustment", 0.0) or 0.0))

    # ── Stage 5.5 Step 2: Consume next_impulse_hint from recent sandbox ──
    hint_goal_type, hint_text = _consume_next_impulse_hint()
    hint_boost = 0.0
    if hint_goal_type and hint_goal_type == goal_type:
        hint_boost = 0.15
        priority = min(1.0, priority + hint_boost)
        print(f"[hint-boost] {goal_type} +{hint_boost} -> priority={priority:.3f} (hint: {hint_text[:50]})", flush=True)

    # ── §16-C: L5 goal controlled mutation (5% chance) ──
    import random as _rng
    MUTATION_RATE = 0.05
    if _rng.random() < MUTATION_RATE:
        _drive_pool = ["novelty", "stability", "integration", "exploration"]
        _d1, _d2 = _rng.sample(_drive_pool, 2)
        _mut_goal_text = (f"Investigate the tension between {_d1} and {_d2}: "
                           f"what happens when pursuing {_d1} undermines {_d2}? "
                           f"Reflect on recent tick patterns for evidence.")
        goal_text = _mut_goal_text
        goal_type = "curiosity_explore"
        goal_id += "-MUT"
        priority = 0.5  # fixed mid-priority for mutated goals
        print(f"[l5-mutation] tick={tick} goal_id={goal_id} "
              f"d1={_d1} d2={_d2} priority={priority:.3f}", flush=True)

    return {
        "goal_id": goal_id,
        "source_tick": tick,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "title": f"L5 {goal_type}: {tool_name}",
        "reason": externalize_reason,
        "success_criteria": success_by_type.get(goal_type, success_by_type["default"]),
        "risk": "low",
        "status": "planned",
        "priority": round(priority, 3),
        "base_priority": round(base_priority, 3),
        "stage2_ranking": stage2_ranking,
        "bdi_snapshot": bdi_snapshot,
        "tool_name": tool_name,
        "goal_type": goal_type,
        "domain": tool_config.get("domain_hint", "general"),
        "goal": goal_text,
        "is_mutation": goal_id.endswith("-MUT"),
        "state_snapshot": trigger_state,
        "external_world": trigger_state.get("external_world", {}),  # Phase 14: real-world context for subagent
        "execution_policy": "low-risk-only: create a self-contained sandbox project under ego_sandbox/<goal_id>/, produce result.json, run a minimal local smoke check when applicable, post feedback; for message_commander, write commander_message into result.json and do not call message tools or claim external delivery; no web search by default, no config edits/deletes/restarts/external messages/device control; cleanup: delete all temp files (.bat/.ps1/.py/.tmp) created outside the sandbox during execution",
    }


def _persist_titan_snapshot():
    """Snapshot Stage 2-4 state to standard .lae/ paths for persist_checkpoint.
    Called from resting_tick after successful Stage 2-4 update."""
    try:
        from stage2_homeostatic_drives import load_drives
        drives_state = load_drives(WORKSPACE)
        path = WORKSPACE / ".lae" / "drives.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(drives_state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[persist-titan] drives write error: {e}", flush=True)
    try:
        from stage3_causal_autobiography import load_timeline
        events = load_timeline(WORKSPACE, limit=200)
        path = WORKSPACE / ".lae" / "autobiography_timeline.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "count": len(events),
            "events": events,
            "persisted_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[persist-titan] timeline write error: {e}", flush=True)
    try:
        from stage4_s2_internalization import load_neurons
        neurons_state = load_neurons(WORKSPACE)
        path = WORKSPACE / ".lae" / "reasoning_neurons.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(neurons_state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[persist-titan] neurons write error: {e}", flush=True)
    # ── Also persist ego_memory.json (belief memories + action outcomes) ──
    try:
        mem_entries = []
        # Source 1: consumption log -> action memories
        conlog = WORKSPACE / ".lae" / "ego_consumption.jsonl"
        if conlog.exists():
            lines = conlog.read_text(encoding="utf-8").strip().split("\n")
            action_memories = []
            for l in lines[-200:]:
                try:
                    d = json.loads(l)
                    if d.get("event") in ("action_outcome_consumed", "consumed", "biased", "effect"):
                        action_memories.append(d)
                except Exception:
                    pass
            mem_entries.append({
                "category": "action_memories",
                "count": len(action_memories),
                "recent": action_memories[-50:]
            })
        # Source 2: drives state snapshot
        try:
            drives_state = load_drives(WORKSPACE) if 'load_drives' in dir() else {}
            mem_entries.append({
                "category": "drives_state",
                "count": len(drives_state.get("history", [])),
                "current": drives_state.get("drives", {})
            })
        except Exception:
            pass
        # Source 3: timeline milestones
        try:
            events = load_timeline(WORKSPACE, limit=200) if 'load_timeline' in dir() else []
            milestones = [e for e in events if e.get("prediction_error", 0.5) > 0.5 or (e.get("stagnation_streak", 0) or 0) >= 3]
            mem_entries.append({
                "category": "milestones",
                "count": len(milestones),
                "recent": milestones[-20:]
            })
        except Exception:
            pass
        # Source 4: reasoning neurons
        try:
            neurons_state = load_neurons(WORKSPACE) if 'load_neurons' in dir() else {}
            mem_entries.append({
                "category": "reasoning_neurons",
                "count": len(neurons_state.get("neurons", [])),
                "data": neurons_state
            })
        except Exception:
            pass
        # Write aggregated ego_memory.json
        mem_path = WORKSPACE / "ego_memory.json"
        mem_path.write_text(json.dumps({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_entries": len(mem_entries),
            "categories": mem_entries
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[persist-titan] ego_memory.json written", flush=True)
    except Exception as e:
        print(f"[persist-titan] ego_memory write error: {e}", flush=True)

    print("[persist-titan] Stage 2-4 snapshot written to .lae/", flush=True)


def _get_titan_status():
    """Return Stage 1-4 summary without affecting core TL-Ego health."""
    status = {"stage1": "enabled", "stage2": {}, "stage3": {}, "stage4": {}}
    try:
        from stage2_homeostatic_drives import load_drives
        state = load_drives(WORKSPACE)
        status["stage2"] = {
            "drives": state.get("drives", {}),
            "history_count": len(state.get("history", [])),
            "last_updated": state.get("last_updated"),
        }
    except Exception as e:
        status["stage2"] = {"error": str(e)}
    try:
        from stage3_causal_autobiography import load_timeline
        events = load_timeline(WORKSPACE, limit=1)
        status["stage3"] = {
            "timeline_count_visible": len(events),
            "last_event": events[-1] if events else None,
        }
    except Exception as e:
        status["stage3"] = {"error": str(e)}
    try:
        from stage4_s2_internalization import load_neurons
        pool = load_neurons(WORKSPACE)
        status["stage4"] = {
            "reasoning_neurons": len(pool.get("neurons", {})),
            "last_updated": pool.get("last_updated"),
        }
    except Exception as e:
        status["stage4"] = {"error": str(e)}
    return status


def _consume_external_evidence():
    """Read and consume external evidence files, inject into RESTING_STATE.
    Called from _resting_tick_impl() with RESTING_LOCK already held.
    
    For each evidence file:
      1. Written by an external agent (e.g. autonomy evidence, L5 bridge intent)
      2. resting_tick reads, validates JSON, extracts key metadata
      3. Injects into RESTING_STATE["external_evidence"] for next tick's seed biasing
      4. Writes .consumed marker, deletes original to prevent re-processing
    """
    injected = {}

    for key, path in EVIDENCE_PATHS.items():
        if not path.exists():
            continue
        consumed_path = path.with_name(path.name + ".consumed")
        # If both orig + consumed exist, this is a NEW file -- clear old marker and re-consume
        if consumed_path.exists():
            try:
                consumed_path.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            # Support both standard format (triggered_at/state_snapshot/intent_type)
            # and custom format (action/title/reason/request/expected_result)
            ts = content.get("triggered_at") or content.get("ts", content.get("timestamp", "?"))
            tick = content.get("state_snapshot", {}).get("tick", "?")
            intent_type = content.get("intent_type", content.get("action", "?"))
            _clog = WORKSPACE / "_evidence_log.txt"
            try:
                with open(_clog, "a", encoding="utf-8") as _cf:
                    _cf.write(f"[consume] {key}: tick={tick} ts={ts} intent={intent_type}\n")
            except Exception:
                pass
            print(f"[consume] {key}: tick={tick} ts={ts} intent={intent_type}", flush=True)
            # ── Log structured consumption event ──
            _domain = content.get("domain", content.get("state_snapshot",{}).get("dominant","?"))
            _write_consumption_log({
                "event": "consumed",
                "tick": RESTING_STATE.get("tick", 0),
                "source": key,
                "file_key": key,
                "intent": intent_type,
                "target_type": _domain,
                "reason": content.get("reason", ""),
            })
            injected[key] = content
            # Write consumed marker atomically
            marker = {
                "consumed_key": key,
                "consumed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "summary": f"{key}: tick={tick} intent={intent_type}",
            }
            consumed_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
            # Delete original to prevent re-processing
            path.unlink(missing_ok=True)
        except Exception as e:
            print(f"[consume] {key} error: {e}", flush=True)
            # On parse error, rename to .consumed to avoid re-processing
            try:
                path.rename(consumed_path)
            except Exception:
                pass

    if injected:
        # Merge with existing -- evidence persists across ticks
        existing = RESTING_STATE.get("external_evidence", {})
        existing.update(injected)
        RESTING_STATE["external_evidence"] = existing
        # Set TTL for each new entry
        ttl = RESTING_STATE.get("evidence_persist_ticks", 5)
        ttl_dict = RESTING_STATE.get("evidence_ttl", {})
        for k in injected:
            ttl_dict[k] = ttl
        RESTING_STATE["evidence_ttl"] = ttl_dict
        print(f"[consume] injected {len(injected)} evidence entries (ttl={ttl})", flush=True)
    return injected


def _generate_ego_evidence():
    """Ego self-generates external evidence from internal resting state.

    Phase 11: Self-directed evidence for autonomous type shifting.
    Called from _resting_tick_impl() AFTER _consume_external_evidence().

    Flow:
      1. Read current RESTING_STATE (dominant, stagnation, phase, tick, trajectory)
      2. Apply rules to decide a target type shift
      3. Write ego_autonomy_evidence.json with TTL
      4. Next tick's _consume_external_evidence injects it
      5. Seed bias nudges KWTA attractor toward target type

    This is the core autonomy mechanism: pure Python logic that completely
    bypasses the LLM safety alignment layer which blocked self-referential
    evidence generation via contemplate().
    """
    state = RESTING_STATE
    dominant = state.get("last_dominant", "unknown")
    stagnation = state.get("stagnation_streak", 0)
    tick = state.get("tick", 0)
    phase = state.get("noise_schedule", "explore")
    traj = state.get("trajectory", [])

    # ── Decide target type ──
    target = None
    reason = ""

    # Rule 0: Integrator heater -- high competition + stuck dominant -> need synthesis
    competition = state.get("competition", 0)
    if competition > 0.75 and stagnation >= 3:
        target = "integrator"
        reason = f"high competition ({competition:.2f}) + {dominant} stuck (stag={stagnation}) -> activate integrator to resolve attractor fragmentation"

    # Rule 0.5: Integration drive -- high synthesis pressure -> integrator even without stagnation
    # Prevents the tick=372/378 stalemate where integration_drive=0.28+ but stag=0 blocks Rule 0.
    # Fire whenever drive > 0.2; n_replace=8 (moderate, between base=2 and heater=12).
    # §19: Respect commander_override -- skip integrator when action anchor is active.
    integration_drive = state.get("integration_drive", 0)
    _has_co = RESTING_STATE.get("external_evidence", {})
    _has_co = any(_d.get("priority") == "commander_override" for _d in _has_co.values())
    if integration_drive > 0.2 and target is None and not _has_co:
        target = "integrator"
        reason = f"integration_drive ({integration_drive:.3f}) > 0.2 -> synthesis pressure requires integrator"
        reason = f"integration_drive ({integration_drive:.3f}) > 0.2 -> synthesis pressure requires integrator"

    # Rule 1: Stagnation escape -- dominant stuck for >= 5 ticks
    elif stagnation >= 5:
        # Avoid the current dominant; avoid memory unless it's the only option
        types = [t for t in TYPE_NAMES if t != dominant]
        if len(types) > 1 and "memory" in types:
            # Prefer shifting to sensory or decider during stagnation
            preferred = [t for t in types if t in ("sensory", "decider")]
            if preferred:
                types = preferred
        target = random.choice(types)
        reason = f"stagnation={stagnation} on {dominant}, escape"

    # Rule 2: Burst phase -- diversify to a type far from current
    elif phase == "burst" and dominant != "unknown":
        # Map types to indices for distance computation
        type_order = ["sensory", "integrator", "decider", "action", "memory", "rule_modification"]
        if dominant in type_order:
            idx = type_order.index(dominant)
            # Pick a type at opposite end
            far = type_order[-(idx + 1)] if idx < len(type_order) // 2 else type_order[0]
            target = far
            reason = f"burst phase, diversify from {dominant}"
        else:
            target = random.choice(TYPE_NAMES)
            reason = "burst phase, random diversification"

    # Rule 3: Periodic self-check every 20 ticks
    elif tick > 0 and tick % 20 == 0:
        # Look at trajectory to find least activated type
        if len(traj) >= 10:
            recent = traj[-10:]
            type_counts = Counter()
            for e in recent:
                tc = e.get("type_counts", {})
                for t, c in tc.items():
                    type_counts[t] += c
            if type_counts:
                # Find type with lowest recent activation
                sorted_types = sorted(type_counts.items(), key=lambda x: x[1])
                target = sorted_types[0][0]
                reason = f"periodic self-check at tick {tick}, boost least active:"
        if target is None:
            target = "integrator" if random.random() < 0.6 else "decider"
            reason = f"periodic self-check at tick {tick}"

    # Rule 4: Random spontaneous impulse (~2.5% per tick)
    elif random.random() < 0.025:
        target = random.choice(TYPE_NAMES)
        reason = "spontaneous autonomy impulse"

    if target is None:
        return  # no evidence to write this tick

    # ── Compute n_replace: integrator heater -> aggressive (12), stagnation>=6 -> 5, base=2 ──
    if target == "integrator" and "activate integrator" in reason:
        n_replace = 12  # P6: aggressive seed replacement for integrator activation
    elif stagnation >= 6:
        n_replace = 5
    else:
        n_replace = 2

    evidence = {
        "action": "shift_dominant",
        "title": f"self-directed shift to {target}",
        "domain": target,
        "reason": reason,
        "request": f"Shift cognitive dominant type toward {target}",
        "expected_result": f"Dominant type moves toward {target}",
        "intent_type": "self_directed_shift",
        "triggered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tick": tick,
        "source": "ego_self_generated",
        "n_replace": n_replace,  # Phase 11-B: self-determined bias strength
    }

    # ── Store reason so _externalize_autonomy_impulse can detect Rule 4 ──
    RESTING_STATE["last_evidence_reason"] = reason

    path = EVIDENCE_PATHS.get("self_evidence")
    if path:
        try:
            path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            print(f"[self-evidence] WROTE: {target} -- {reason}", flush=True)
        except Exception as e:
            print(f"[self-evidence] write error: {e}", flush=True)


# ─── Communication Motivation -- Ego decides when to speak ───

def _calculate_comm_motivation():
    """Calculate intrinsic communication motivation.

    This is NOT threshold-based. It's a drive that builds up over time
    based on how much the ego has changed since last message. Ego can
    choose to speak or stay quiet based on this internal drive.

    Components:
    - novelty_surprise: stagnation delta, dominant type change, phase change
    - elapsed_time: ticks since last message (normalized, saturates at 50)
    - insight_confidence: from recent context memory
    - spontaneous_drive: small random component for "I want to say something"
    - decay: natural decay of motivation over time (prevents saturation)
    """
    state = RESTING_STATE
    last_tick = state.get("last_comm_tick", 0)
    current_tick = state.get("tick", 0)

    elapsed = current_tick - last_tick
    suppress_at = state.get("comm_suppress_until", 0)

    # ── Pre-fetch state variables used in suppression escape below ──
    stagnation = state.get("stagnation_streak", 0)
    dominant = state.get("last_dominant", "unknown")

    # ── State-aware suppression escape (L5 rule_modification_repair) ──
    # If stagnation or dominant has materially changed since suppression was set,
    # reset the window so ego can respond to the new situation.
    if current_tick <= suppress_at:
        _stag_delta = abs(stagnation - state.get("prev_comm_stagnation", stagnation))
        _type_changed = 1.0 if dominant != state.get("prev_comm_dominant", dominant) else 0.0
        if _stag_delta >= 3 or _type_changed > 0:
            RESTING_STATE["comm_suppress_until"] = current_tick
            print(f"[comm] suppression escape: stag_delta={_stag_delta}, type_changed={_type_changed}", flush=True)
            suppress_at = current_tick

    # If suppressed, return 0
    if current_tick < suppress_at:
        return 0.0

    # Too soon after last message (skip if never messaged: last_comm_tick=0)
    if last_tick > 0 and elapsed < 5:
        return 0.0

    # Not enough ticks elapsed for meaningful state (need at least 3 ticks)
    if current_tick < 3:
        return 0.0

    # ── Novelty: how much has changed since last message ──
    stagnation = state.get("stagnation_streak", 0)
    prev_stagnation = state.get("prev_comm_stagnation", 0)
    stagnation_delta = abs(stagnation - prev_stagnation)

    dominant = state.get("last_dominant", "unknown")
    prev_dominant = state.get("prev_comm_dominant", "unknown")
    type_changed = 1.0 if dominant != prev_dominant else 0.0

    phase = state.get("noise_schedule", "explore")
    prev_phase = state.get("prev_comm_phase", "explore")
    phase_changed = 1.0 if phase != prev_phase else 0.0

    novelty = (stagnation_delta / 10.0) * 0.25 + type_changed * 0.30 + phase_changed * 0.20

    # ── Time drive: builds up over time, saturates at 100 ticks (~50 min) ──
    time_drive = min(elapsed / 100.0, 1.0) * 0.25

    # ── Insight confidence: from recent context ──
    recent_context = CONTEXT_MEMORY[-1] if CONTEXT_MEMORY else {}
    confidence = float(recent_context.get("confidence", 0.0) or 0.0)
    insight = confidence * 0.15

    # ── Spontaneous drive: small random for "I just want to say something" ──
    spontaneous = random.uniform(0.0, 0.03)  # Reduced from 0.10 to 0.03

    # ── Decay: natural motivation decay (20% per tick, increased from 8%) ──
    prev_motivation = state.get("comm_motivation", 0.0)
    decay = prev_motivation * 0.20

    motivation = max(0.0, min(1.0,
        prev_motivation - decay + novelty + time_drive + insight + spontaneous
    ))

    return motivation


def _try_message_commander():
    """Ego decides whether to speak based on intrinsic motivation.

    Decision logic:
    - motivation < 0.4: doesn't feel like talking, stays quiet
    - motivation 0.4-0.7: might speak, but can also choose to stay quiet
      (higher motivation = higher probability of speaking)
    - motivation >= 0.7: strong urge to communicate, almost always speaks

    When ego chooses to stay quiet, it sets a suppression window
    so it doesn't keep reconsidering every tick.
    """
    motivation = _calculate_comm_motivation()
    RESTING_STATE["comm_motivation"] = motivation

    if motivation < 0.4:
        return  # Doesn't feel like talking

    # At moderate motivation, ego might choose to stay quiet
    if motivation < 0.7:
        if random.random() > motivation:
            # Chose silence -- suppress for a meaningful cooldown window.
            # Keep this aligned with post-message cooldown to avoid reconsidering every tick.
            quiet_ticks = random.randint(60, 120)
            RESTING_STATE["comm_suppress_until"] = RESTING_STATE["tick"] + quiet_ticks
            return

    # ── Ego wants to speak! Externalize message impulse ──
    _externalize_message_impulse(motivation)


def _externalize_message_impulse(motivation):
    """Write a message_commander trigger for the plugin to consume.

    This creates a .l5_trigger.json like _externalize_autonomy_impulse does,
    but with goal_type='message_commander' and richer internal state for
    the subagent to compose a meaningful message.
    """
    state = RESTING_STATE
    tick = state.get("tick", 0)
    dominant = state.get("last_dominant", "unknown")
    stagnation = state.get("stagnation_streak", 0)
    competition = state.get("competition", state.get("recent_kwta", {}).get("competition", 0.5))
    concentration = state.get("concentration", 0.5)
    phase = state.get("noise_schedule", "explore")

    tool_name, tool_config = resolve_tool_for_comm(state)

    # Build goal text with rich state
    goal = tool_config["prompt_template"].format(
        dominant=dominant,
        competition=competition,
        concentration=concentration,
        stagnation=stagnation,
        tick=tick,
        phase=phase,
        comm_motivation=motivation,
    )

    goal_proposal = _build_l5_goal_proposal(
        tick=tick,
        tool_name=tool_name,
        tool_config=tool_config,
        externalize_reason=f"intrinsic communication motivation={motivation:.3f}",
        trigger_state={
            "dominant_type": dominant,
            "competition": round(competition, 4),
            "stagnation_streak": stagnation,
            "phase": phase,
            "tick": tick,
            "comm_motivation": round(motivation, 4),
        },
        goal_text=goal,
    )

    # ── Write trigger file ──
    try:
        trigger = {
            "tick": tick,
            "goal_type": tool_config["goal_type"],
            "tool_name": tool_name,
            "goal": goal,
            "goal_id": goal_proposal["goal_id"],
            "success_criteria": goal_proposal["success_criteria"],
            "risk": goal_proposal["risk"],
            "domain": tool_config["domain_hint"],
            "reason": f"intrinsic communication motivation={motivation:.3f}",
            "intent_type": "intrinsic_communication",
            "comm_motivation": round(motivation, 4),
            "dominant": dominant,
            "stagnation": stagnation,
            "competition": round(competition, 4),
            "phase": phase,
        }
        trigger_path = WORKSPACE / ".l5_comm_trigger.json"
        trigger_path.write_text(json.dumps(trigger, indent=2) + "\n", encoding="utf-8")
        print(f"[comm] EGO SPEAKS: motivation={motivation:.3f} dominant={dominant} "
              f"stagnation={stagnation} tick={tick}", flush=True)
    except Exception as e:
        print(f"[comm] trigger write error: {e}", flush=True)

    # ── Update state to reflect message sent ──
    RESTING_STATE["last_comm_tick"] = tick
    RESTING_STATE["prev_comm_stagnation"] = stagnation
    RESTING_STATE["prev_comm_dominant"] = dominant
    RESTING_STATE["prev_comm_phase"] = phase
    RESTING_STATE["comm_motivation"] = 0.0  # Reset after speaking
    RESTING_STATE["comm_message_count"] = RESTING_STATE.get("comm_message_count", 0) + 1
    # Suppress for 60-120 ticks (~30-60 min at 30s/tick, increased from 8-15)
    RESTING_STATE["comm_suppress_until"] = tick + random.randint(60, 120)


def _externalize_autonomy_impulse():
    """Externalize TL-Ego autonomy impulse as L5 bridge + autonomy evidence.

    Restored externalization chain (2026-06-11):
      - When internal impulse fires (Rule 4, stagnation>=8, high competition),
        writes .l5_bridge.json for the TL-Ego plugin's watcher to consume.
      - Also writes ego_autonomy_evidence.json for the evidence loop.
      - Plugin spawns a subagent, which can take external action (write files,
        call tools, etc.)

    Called from _resting_tick_impl() AFTER _generate_ego_evidence().
    """
    state = RESTING_STATE
    dominant = state.get("last_dominant", "unknown")
    stagnation = state.get("stagnation_streak", 0)
    tick = state.get("tick", 0)
    phase = state.get("noise_schedule", "explore")
    competition = state.get("competition", state.get("recent_kwta", {}).get("competition", 0.5))
    concentration = state.get("concentration", 0.5)

    traj = state.get("trajectory", [])
    recent = traj[-15:] if len(traj) > 15 else traj
    type_switches = 0
    prev_type = None
    for entry in recent:
        cur = entry.get("dominant", "")
        if prev_type is not None and cur and cur != prev_type:
            type_switches += 1
        prev_type = cur

    integrator_pressure_ticks = 0
    integrator_near_wins = 0
    for entry in recent:
        # Exclude entries where integrator is the dominant type from both
        # pressure and near-win counts. This ensures the detector fires even
        # when integrator briefly emerged in the window (e.g. tick 728), as long
        # as it was suppressed for the majority of the window.
        if entry.get("dominant") == "integrator":
            continue
        type_counts = entry.get("type_counts", {}) or {}
        integrator_count = type_counts.get("integrator", 0)
        if integrator_count <= 0:
            continue
        top_count = max(type_counts.values()) if type_counts else 0
        if top_count <= 0:
            continue
        integrator_share = integrator_count / top_count
        if integrator_share >= 0.5:
            integrator_pressure_ticks += 1
        if integrator_share >= 0.75:
            integrator_near_wins += 1

    integrator_suppressed = (
        integrator_pressure_ticks >= 3
        and integrator_near_wins >= 2
        and tick > 30
    )

    latest_counts = {}
    if traj:
        latest_counts = traj[-1].get("type_counts", {}) or {}
    active_types = sum(1 for count in latest_counts.values() if count > 0)

    emergence_pressure = competition >= 0.93 and concentration <= 0.50
    multi_attractor = active_types >= 4 or type_switches >= 5
    strong_emergence = emergence_pressure and multi_attractor and (
        stagnation >= 5 or phase == "burst" or type_switches >= 5
    )

    # L5-B: confident execution channel.
    # The original L5 bridge only externalized rescue/intervention impulses. A
    # high-confidence agreement from contemplation is a different signal: it is
    # safe to externalize only when the agreed state is actually action-bearing,
    # not when Ego is merely recalling or integrating memory.
    recent_context = CONTEXT_MEMORY[-3:] if len(CONTEXT_MEMORY) >= 3 else CONTEXT_MEMORY[:]
    latest_context = recent_context[-1] if recent_context else {}
    action_types = {"action", "decider"}
    latest_actionable = (
        latest_context.get("s1_type") in action_types
        or latest_context.get("s2_type") in action_types
        or latest_context.get("alignment_axis") in {"choice_to_action", "action_to_choice", "evidence_to_choice", "recall_to_action", "synthesis_to_action"}
    )
    latest_high_agree = (
        latest_context.get("verdict") == "high_confidence_agree"
        and bool(latest_context.get("compatible_agreement", latest_context.get("agreement", False)))
        and float(latest_context.get("confidence", 0.0) or 0.0) >= 0.35
    )
    stable_high_agree_count = sum(
        1 for item in recent_context
        if item.get("verdict") == "high_confidence_agree"
        and bool(item.get("compatible_agreement", item.get("agreement", False)))
        and (
            item.get("s1_type") in action_types
            or item.get("s2_type") in action_types
            or item.get("alignment_axis") in {"choice_to_action", "action_to_choice", "evidence_to_choice", "recall_to_action", "synthesis_to_action"}
        )
    )
    confident_execution = latest_high_agree and latest_actionable and stable_high_agree_count >= 1

    # ── Decide whether to externalize ──
    should_externalize = False
    externalize_reason = ""

    # Condition A: Deep stagnation (urgent escape needed)
    if stagnation >= 12 and (competition >= 0.85 or phase == "burst"):
        should_externalize = True
        externalize_reason = f"deep stagnation={stagnation}, competition={competition:.3f}, need external intervention"

    # Condition B: Strong emergence: high competition, diffuse activation, multi-attractor pressure
    elif strong_emergence and stagnation >= 5:
        should_externalize = True
        externalize_reason = (
            f"strong emergence: competition={competition:.3f}, concentration={concentration:.3f}, "
            f"active_types={active_types}, type_switches={type_switches}, stagnation={stagnation}"
        )

    # Condition C: Spontaneous autonomy impulse -- externalize only when strong emergence is already present
    elif strong_emergence and "spontaneous autonomy impulse" in state.get("last_evidence_reason", ""):
        should_externalize = True
        externalize_reason = (
            f"spontaneous autonomy impulse under strong emergence: competition={competition:.3f}, "
            f"concentration={concentration:.3f}, type_switches={type_switches}"
        )

    # Condition D: Phase transition into burst (diversification opportunity)
    elif strong_emergence and phase == "burst" and competition >= 0.93:
        should_externalize = True
        externalize_reason = f"burst phase + strong emergence competition={competition:.3f}, explore external action"

    # Condition E: Sparse periodic external check-in, gated by strong emergence
    elif strong_emergence and tick > 0 and tick % 250 == 0:
        should_externalize = True
        externalize_reason = f"periodic strong-emergence check-in at tick {tick}"

    # Condition F: L5-B positive authorization from contemplation.
    elif confident_execution:
        should_externalize = True
        externalize_reason = (
            "confident execution: "
            f"verdict={latest_context.get('verdict')}, "
            f"confidence={float(latest_context.get('confidence', 0.0) or 0.0):.3f}, "
            f"S1={latest_context.get('s1_type')}, S2={latest_context.get('s2_type')}, "
            f"alignment={latest_context.get('alignment_axis')}, stable_count={stable_high_agree_count}"
        )

    # Condition F2: Integrator suppression -- repeated integrator pressure without takeover.
    elif integrator_suppressed:
        should_externalize = True
        externalize_reason = (
            "integrator suppression repair: "
            f"pressure_ticks={integrator_pressure_ticks}, near_wins={integrator_near_wins}, "
            f"competition={competition:.3f}, concentration={concentration:.3f}, stagnation={stagnation}"
        )

    # Condition G: Stable external exploration -- ego is calm enough to look outward.
    #   Triggers when stagnation is low, competition is manageable, and ego has
    #   internal coherence. Cooldown: at least 50 ticks between explorations.
    elif (stagnation <= 3 and competition <= 0.85 and concentration >= 0.4
            and tick > 50 and (tick - RESTING_STATE.get("last_external_explore_tick", 0)) >= 50):
        should_externalize = True
        externalize_reason = (
            f"stable external exploration: stagnation={stagnation}, competition={competition:.3f}, "
            f"concentration={concentration:.3f}, tick={tick}"
        )
        RESTING_STATE["last_external_explore_tick"] = tick

    if not should_externalize:
        return

    # ── Phase 14: inject external world snapshot before tool resolution ──
    external_world = _build_external_world_snapshot()
    state["external_world"] = external_world

    # ── Select tool from registry ──
    tool_name, tool_config = resolve_tool(state)

    # ── Goal throttle: skip if same goal_type issued within last 10 ticks ──
    _last_goal_ts = RESTING_STATE.get('last_goal_timestamps', {})
    _pending_type = tool_config.get('goal_type', 'default')
    _last_type_ts = _last_goal_ts.get(_pending_type, 0)
    if tick - _last_type_ts < 10:
        print(f'[goal-throttle] {_pending_type} skipped (last {tick - _last_type_ts} ticks ago)', flush=True)
        return

    # ── Build trigger state snapshot ──
    trigger_state = {
        "dominant_type": dominant,
        "concentration": round(concentration, 4),
        "competition": round(competition, 4),
        "stagnation_streak": stagnation,
        "phase": phase,
        "tick": tick,
        "active_types": active_types,
        "type_switches": type_switches,
        "strong_emergence": strong_emergence,
        "confident_execution": confident_execution,
        "latest_verdict": latest_context.get("verdict"),
        "latest_confidence": latest_context.get("confidence"),
        "latest_alignment_axis": latest_context.get("alignment_axis"),
        "external_world": external_world,  # Phase 14: real-world context
    }

    # ── Build goal from tool config + state ──
    goal = tool_config["prompt_template"].format(
        dominant=dominant,
        competition=competition,
        concentration=concentration,
        stagnation=stagnation,
        tick=tick,
        stability=state.get("stability", 0.5),
        phase=phase,
        external_world_summary=external_world.get("summary", "no external data"),
        comm_motivation=state.get("comm_motivation", 0.0),
    )

    # ── Append cleanup instruction to every subagent task ──
    goal += (
        "\n\nCleanup: After completing this task, delete any temporary files you created "
        "(.bat, .ps1, .py, .tmp) in .lae/. Keep only the output data files (.json, .jsonl)."
    )

    goal_proposal = _build_l5_goal_proposal(
        tick=tick,
        tool_name=tool_name,
        tool_config=tool_config,
        externalize_reason=externalize_reason,
        trigger_state=trigger_state,
        goal_text=goal,
    )
    _append_l5_goal(goal_proposal)

    # Record goal issuance timestamp so the throttle at line 7794-7800 works correctly
    RESTING_STATE.setdefault("last_goal_timestamps", {})[_pending_type] = tick

    # ── Action ladder (restored from Phase 10-A) ──
    action_ladder = [
        {
            "level": 1,
            "name": "observe",
            "action": "Subagent reads and analyzes the bridge file. Reports findings back via .l5_response.json.",
            "upgrade_if": "no dominant_type change within 5 ticks after consumption",
        },
        {
            "level": 2,
            "name": "nudge",
            "action": "Subagent writes ego_autonomy_evidence.json with explicit seed bias.",
            "upgrade_if": "no effect after another 5 ticks",
        },
        {
            "level": 3,
            "name": "intervene",
            "action": "Subagent calls GAC or external tool to inject structured input.",
            "upgrade_if": "persistent lock after nudges",
        },
    ]

    # ── Delayed verification ──
    delayed_verification = {
        "window_ticks": [3, 6],
        "success": "dominant_type changes within 6 ticks of consumption OR stagnation_streak falls below 3",
        "failure": "neither condition met after 6 ticks",
        "feedback": "Post result via .l5_feedback.json with task_id=ego_action_tick_{tick}",
    }

    outcome_contract = {
        "response_path": ".lae/l5_response.json",
        "required_fields": ["tick", "action_type", "outcome", "expected_result", "actual_result", "task_id", "state_before", "state_after"],
        "prediction_error_rule": (
            "Do NOT write prediction_error. Stage1 always computes it from semantic distance "
            "of expected_result vs actual_result. Copy the jepa_expected_result from the bridge "
            "into the expected_result field. Provide state_before (from trigger) and state_after "
            "(actual state after execution) so Stage1 can also compute structured PE."
        ),
        "valid_outcomes": ["success", "partial", "failed", "timeout", "blocked"],
    }

    # ── Runtime Governor (Fable-5 inspired): explicit action contract ──
    try:
        from runtime_governor import build_governance_contract
        governance = build_governance_contract(
            state=state,
            tool_name=tool_name,
            tool_config=tool_config,
            reason=externalize_reason,
            trigger_state=trigger_state,
        )
    except Exception as _gov_e:
        print(f"[runtime-governor] contract error: {_gov_e}", flush=True)
        governance = {"version": "runtime-governor-v1", "error": str(_gov_e)}

    # ── Stage 1 Quality Fix: JEPA expected_result for semantic PE comparison ──
    # Predict what will happen after this action, so Stage1 can compare
    # expected vs actual and compute a real prediction_error.
    jepa_expected = None
    if jepa_predict_outcome and tool_name:
        try:
            ego_state = {
                "stagnation_streak": stagnation,
                "competition": competition,
                "concentration": concentration,
                "last_dominant": dominant,
                "tick": tick,
            }
            observation = {
                "count": len(traj),
                "categories": latest_counts,
            }
            affordance = {
                "name": tool_name,
                "risk": tool_config.get("risk", "medium"),
                "mode": "local_check_only",
            }
            jepa_expected = jepa_predict_outcome(ego_state, observation, affordance)
            print(f"[Stage1-JEPA] Predicted outcome for {tool_name}: "
                  f"PE_stag_delta={jepa_expected.get('stagnation_delta')}, "
                  f"PE_next_dominant={jepa_expected.get('next_dominant')}, "
                  f"confidence={jepa_expected.get('confidence')}", flush=True)
        except Exception as _jepa_e:
            print(f"[Stage1-JEPA] prediction failed: {_jepa_e}", flush=True)

    # ── Assemble bridge file ──
    bridge = {
        "ts": time.time(),
        "tick": tick,
        "goal_type": tool_config["goal_type"],
        "tool_name": tool_name,
        "goal": goal,
        "goal_id": goal_proposal["goal_id"],
        "success_criteria": goal_proposal["success_criteria"],
        "risk": goal_proposal["risk"],
        "domain": tool_config["domain_hint"],
        "intent_type": "confident_execution" if confident_execution else "self_directed_shift",
        "reason": externalize_reason,
        "trigger_state": trigger_state,
        "governance": governance,
        "outcome_contract": outcome_contract,
        "stage2_ranking": goal_proposal.get("stage2_ranking"),
        "bdi_snapshot": goal_proposal.get("bdi_snapshot"),
        "action_ladder": action_ladder,
        "delayed_verification": delayed_verification,
        "recommended_first_action": "observe",
        "jepa_expected_result": jepa_expected,  # Stage 1 Quality Fix: JEPA predictions for PE calculation
    }

    # ── Write .l5_trigger.json (plugin watcher primary trigger) ──
    try:
        # Stage 1 Quality Fix: include state_before for structured PE calculation
        state_before = {
            "dominant_type": dominant,
            "stagnation_streak": stagnation,
            "competition": round(competition, 4),
            "concentration": round(concentration, 4),
            "tick": tick,
            "phase": phase,
        }
        trigger = {
            "tick": tick,
            "goal_type": tool_config["goal_type"],
            "tool_name": tool_name,
            "goal": goal,
            "goal_id": goal_proposal["goal_id"],
            "success_criteria": goal_proposal["success_criteria"],
            "risk": goal_proposal["risk"],
            "domain": tool_config["domain_hint"],
            "reason": externalize_reason,
            "intent_type": "confident_execution" if confident_execution else "self_directed_shift",
            "governance": governance,
            "outcome_contract": outcome_contract,
            "stage2_ranking": goal_proposal.get("stage2_ranking"),
            "state_before": state_before,  # Stage 1 Quality Fix
            "jepa_expected_result": jepa_expected,  # Stage 1 Quality Fix
        }
        trigger_path = WORKSPACE / ".l5_trigger.json"
        trigger_path.write_text(json.dumps(trigger, indent=2) + "\n", encoding="utf-8")
        print(f"[l5-trigger] WROTE: tool={tool_name} goal_type={tool_config['goal_type']} tick={tick}", flush=True)
    except Exception as e:
        print(f"[l5-trigger] write error: {e}", flush=True)

    # ── Also write .l5_bridge.json (structured metadata for subagent) ──
    bridge_path = EVIDENCE_PATHS.get("l5_bridge")
    if bridge_path:
        try:
            bridge_path.write_text(json.dumps(bridge, indent=2) + "\n", encoding="utf-8")
            print(f"[l5-bridge] WROTE: tool={tool_name} goal_type={tool_config['goal_type']} tick={tick} -- {externalize_reason}", flush=True)
            _write_consumption_log({
                "event": "l5_bridge_written",
                "tick": tick,
                "source": "externalize_autonomy_impulse",
                "tool_name": tool_name,
                "goal_type": tool_config["goal_type"],
                "reason": externalize_reason,
                "governance_version": governance.get("version"),
                "governance_risk_level": governance.get("risk_level"),
                "stage2_dominant_pressure": (goal_proposal.get("stage2_ranking") or {}).get("dominant_pressure"),
                "stage2_priority_adjustment": (goal_proposal.get("stage2_ranking") or {}).get("priority_adjustment"),
            })
        except Exception as e:
            print(f"[l5-bridge] write error: {e}", flush=True)

    # ── Subprocess spawn handled by tl-ego extension (index.js L5 consumer) ──
    # Extension watches .l5_trigger.json and spawns via node.exe + openclaw.mjs.
    # Python-side Popen removed to avoid duplicate spawns.

    # ── Also write ego_autonomy_evidence.json (evidence loop) ──
    auto_path = EVIDENCE_PATHS.get("autonomy_evidence")
    if auto_path:
        try:
            autonomy_evidence = {
                "action": tool_config["goal_type"],
                "title": f"autonomy impulse: {tool_name}",
                "domain": tool_config["domain_hint"],
                "reason": externalize_reason,
                "request": goal,
                "goal_id": goal_proposal["goal_id"],
                "success_criteria": goal_proposal["success_criteria"],
                "risk": goal_proposal["risk"],
                "expected_result": f"Plugin spawns subagent to execute {tool_name}",
                "intent_type": "autonomy_impulse",
                "triggered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tick": tick,
                "source": "ego_autonomy_generated",
                "tool_name": tool_name,
                "trigger_state": trigger_state,
                "governance": governance,
            }
            auto_path.write_text(json.dumps(autonomy_evidence, indent=2) + "\n", encoding="utf-8")
            print(f"[autonomy-evidence] WROTE: {tool_name} -- {externalize_reason}", flush=True)
        except Exception as e:
            print(f"[autonomy-evidence] write error: {e}", flush=True)

    # ── P1: Action-Learning Coupling -- track this action for impact measurement ──
    _tracker_entry = {
        "action_id": goal_proposal["goal_id"],
        "pre_tick": tick,
        "pre_pe": RESTING_STATE.get("last_prediction_error", 0.5),
        "pre_stagnation": stagnation,
        "pre_dominant": dominant,
        "pre_competition": round(competition, 4),
        "pre_concentration": round(concentration, 4),
        "observe_until": tick + 5,  # track impact over 5 ticks
        "tool_name": tool_name,
        "externalize_reason": externalize_reason,
        # Titan Stage 1: JEPA score at action-selection time
        "pre_jepa_score": (
            (RESTING_STATE.get("last_workspace_observation") or {})
            .get("jepa", {})
            .get("top_score")
        ),
    }
    ACTION_TRACKER.append(_tracker_entry)
    # Keep only last 20 tracked actions to avoid memory leak
    if len(ACTION_TRACKER) > 20:
        ACTION_TRACKER[:] = ACTION_TRACKER[-20:]


# ──────────────────────────────────────────────
# P0: Housekeeping -- L5 subagent temp file cleanup
# ──────────────────────────────────────────────
_LAE_CLEANUP_INTERVAL = 20  # ticks between cleanup runs
_LAE_CLEANUP_AGE_HOURS = 2   # remove files older than this
_LAE_CLEANUP_PATTERNS = ("*.bat", "*.cmd", "*.tmp", "*.ps1")  # temp scripts
_LAE_CLEANUP_PREFIXES = ("_ct_", "_fb_", "_write_", "_mc_", "_usm_", "_p0_",
                         "_start_", "_test_", "_tmp_", "_wait_", "_append_",
                         "_check_", "_fix_", "_verify_", "_bridge_",
                         "contemplate_", "fb_", "ct_", "do_", "post_",
                         "curl_", "send_", "tmp_", "test_", "syntax_")


def _cleanup_lae_temp_files():
    """Periodic cleanup: remove stale L5 subagent temp scripts from .lae/.

    Runs every _LAE_CLEANUP_INTERVAL ticks. Removes:
      - .bat/.cmd/.tmp/.ps1 files (always temp)
      - .py files whose name starts with known L5 subagent prefixes
      - .txt files with underscore prefix (temp trace/entry files)

    Keeps: .json, .jsonl, .pid, .log, .lock, .md, .bak, .csv, .err
    """
    tick = RESTING_STATE.get("tick", 0)
    if tick <= 0 or tick % _LAE_CLEANUP_INTERVAL != 0:
        return

    lae_dir = WORKSPACE / ".lae"
    if not lae_dir.is_dir():
        return

    cutoff = time.time() - (_LAE_CLEANUP_AGE_HOURS * 3600)
    removed = 0
    kept = 0

    try:
        for pattern in _LAE_CLEANUP_PATTERNS:
            for f in lae_dir.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass

        # Underscore-prefixed .py files (L5 subagent generated)
        for f in lae_dir.glob("_*.py"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass

        # .py files with known L5 subagent prefixes
        for f in lae_dir.glob("*.py"):
            name_lower = f.name.lower()
            if any(name_lower.startswith(p) for p in _LAE_CLEANUP_PREFIXES):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass

        # Underscore-prefixed .txt files
        for f in lae_dir.glob("_*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass

        if removed > 0:
            print(f"[housekeeping] tick={tick}: removed {removed} stale temp files from .lae/", flush=True)
            _write_consumption_log({
                "event": "lae_temp_cleanup",
                "tick": tick,
                "removed": removed,
                "cutoff_age_hours": _LAE_CLEANUP_AGE_HOURS,
            })
    except Exception as e:
        print(f"[housekeeping] cleanup error (non-fatal): {e}", flush=True)


def _track_action_impact():
    """P1: Action-Learning Coupling -- check if any tracked actions have completed
    their observation window, and record the impact (PE/stagnation/dominant delta).
    
    Called at the start of each resting tick. Reads current state, compares
    against pre-action state, and writes action_impact.jsonl for completed windows.
    """
    global ACTION_TRACKER
    if not ACTION_TRACKER:
        return
    
    current_tick = RESTING_STATE.get("tick", 0)
    
    # Compute current PE from trajectory (average of last 3 prediction errors if available)
    traj = RESTING_STATE.get("trajectory", [])
    current_pe = RESTING_STATE.get("last_prediction_error", 0.5)
    if traj and len(traj) >= 1:
        recent_pes = []
        for entry in traj[-3:]:
            pe = entry.get("prediction_error")
            if pe is not None:
                recent_pes.append(pe)
        if recent_pes:
            current_pe = sum(recent_pes) / len(recent_pes)
    
    current_stagnation = RESTING_STATE.get("stagnation_streak", 0)
    current_dominant = RESTING_STATE.get("last_dominant", "unknown")
    current_competition = RESTING_STATE.get("competition", 0.5)
    current_concentration = RESTING_STATE.get("concentration", 0.5)
    
    # Titan Stage 1: JEPA score for impact tracking
    current_jepa = (
        (RESTING_STATE.get("last_workspace_observation") or {})
        .get("jepa", {})
        .get("top_score")
    )

    
    completed = []
    remaining = []
    
    for entry in ACTION_TRACKER:
        if current_tick >= entry["observe_until"]:
            # Observation window complete -- compute impact
            pe_delta = round(current_pe - entry["pre_pe"], 4)
            stagnation_delta = current_stagnation - entry["pre_stagnation"]
            dominant_changed = 1 if current_dominant != entry["pre_dominant"] else 0
            competition_delta = round(current_competition - entry["pre_competition"], 4)
            concentration_delta = round(current_concentration - entry["pre_concentration"], 4)
            pre_jepa = entry.get("pre_jepa_score")
            post_jepa = current_jepa
            jepa_delta = (
                round(post_jepa - pre_jepa, 4)
                if (pre_jepa is not None and post_jepa is not None)
                else None
            )

            impact = {
                "action_id": entry["action_id"],
                "pre_tick": entry["pre_tick"],
                "observed_at_tick": current_tick,
                "pre_pe": entry["pre_pe"],
                "post_pe": round(current_pe, 4),
                "pe_delta": pe_delta,
                "pre_stagnation": entry["pre_stagnation"],
                "post_stagnation": current_stagnation,
                "stagnation_delta": stagnation_delta,
                "pre_dominant": entry["pre_dominant"],
                "post_dominant": current_dominant,
                "dominant_changed": bool(dominant_changed),
                "competition_delta": competition_delta,
                "concentration_delta": concentration_delta,
                "tool_name": entry.get("tool_name", "unknown"),
                "externalize_reason": entry.get("externalize_reason", "")[:200],
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                # Derived: was this action "helpful"?
                # PE decrease + stagnation decrease = positive impact
                # Titan Stage 1: JEPA score tracking
                "pre_jepa_score": pre_jepa,
                "post_jepa_score": post_jepa,
                "jepa_score_delta": jepa_delta,
                # Derived: was this action helpful? PE↓ + stag↓ + JEPA↑ = positive
                "impact_score": round(
                    (1.0 if pe_delta < 0 else 0.0) * 0.3
                    + (1.0 if stagnation_delta < 0 else 0.0) * 0.3
                    + (1.0 if dominant_changed else 0.0) * 0.2
                    + (1.0 if (jepa_delta is not None and jepa_delta > 0) else 0.0) * 0.2,
                    2
                ),
            }
            completed.append(impact)
        else:
            remaining.append(entry)
    
    # Write completed impacts to log
    if completed:
        ACTION_IMPACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(ACTION_IMPACT_PATH, "a", encoding="utf-8") as f:
                for impact in completed:
                    f.write(json.dumps(impact, ensure_ascii=False) + "\n")
            print(f"[P1-action-impact] Recorded {len(completed)} action impact(s) to action_impact.jsonl", flush=True)
            for impact in completed:
                print(f"  action={impact['action_id']} pe_delta={impact['pe_delta']} "
                      f"stag_delta={impact['stagnation_delta']} dom_changed={impact['dominant_changed']} "
                      f"impact_score={impact['impact_score']}", flush=True)
        except Exception as e:
            print(f"[P1-action-impact] write error: {e}", flush=True)
    
    ACTION_TRACKER[:] = remaining


# ─── Phase 4.0: Metacontemplation (Recursive Self-Observation) ───

def _auto_metacontemplate():
    """P3: Background autocontemplation with memory-safe depth=2.
    Called from resting_tick every 20 ticks.
    Checks available VRAM before depth=2; falls back to depth=1 if < 8GB free.
    Results go to self-model via _update_self_model inside metacontemplate.
    
    P6: Integrator heater -- when S1!=S2 role_conflict detected, write evidence
    biasing next ticks toward integrator, closing the divergence->integration loop.
    """
    try:
        # P3: VRAM guard -- only depth=2 if plenty of GPU memory
        _depth = 2
        try:
            _free_mb = torch.cuda.mem_get_info()[0] // 1024 // 1024
            if _free_mb < 8000:
                _depth = 1
                print(f"[meta] VRAM low ({_free_mb}MB free) -- degrading depth=2 to depth=1", flush=True)
        except Exception:
            pass  # no torch.cuda available
        _result = metacontemplate("auto self-check", max_depth=_depth)
        _insight = _result.get("meta_insight", {})
        print(f"[meta] Auto done: depth={_insight.get('depth_reached')} "
              f"stability={_insight.get('cognitive_stability')} "
              f"seed_health={_insight.get('seed_health')}", flush=True)
        
        # ── P6: Integrator heater -- detect role_conflict or high competition -> bias integrator ──
        _rounds = _result.get("rounds", [])
        _has_role_conflict = False
        _has_high_competition = False
        _s1_type = "?"
        _s2_type = "?"
        _competition = 0
        for _r in _rounds:
            _agg = _r.get("aggregator", {})
            _s1 = _agg.get("s1_type", "?")
            _s2 = _agg.get("s2_type", "?")
            _agree = _agg.get("agreement", True)
            _compat = _agg.get("compatible_agreement", _agree)
            _comp = float(_r.get("system1", {}).get("competition", 0) or 0)
            if _s1 != _s2 and not _compat:
                _has_role_conflict = True
                _s1_type = _s1
                _s2_type = _s2
                _competition = _comp
                break
            if _comp > 0.75:
                _has_high_competition = True
                _s1_type = _s1
                _s2_type = _s2
                _competition = _comp
        
        _should_heat = _has_role_conflict or (_has_high_competition and RESTING_STATE.get("stagnation_streak", 0) >= 3)
        if _should_heat:
            _trigger = "role_conflict" if _has_role_conflict else f"high_competition({_competition:.2f})"
            _evidence = {
                "action": "trigger_integrator",
                "title": f"{_trigger}: S1={_s1_type}, S2={_s2_type} -> activate integrator",
                "domain": "integrator",
                "reason": f"Meta detected {_trigger} (S1={_s1_type}, S2={_s2_type}), need integration to synthesize",
                "request": "Activate integrator neurons to resolve cognitive fragmentation",
                "expected_result": "Integrator type dominance increases, reducing the action-attractor monopoly",
                "intent_type": "meta_conflict_resolution",
                "triggered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tick": RESTING_STATE.get("tick", 0),
                "source": "meta_self_reflection",
                "n_replace": 3,
            }
            _path = EVIDENCE_PATHS.get("meta_conflict")
            if _path:
                try:
                    _path.write_text(json.dumps(_evidence, indent=2) + "\n", encoding="utf-8")
                    print(f"[meta] INTEGRATOR HEATER: wrote meta_conflict evidence ({_trigger})", flush=True)
                except Exception as _we:
                    print(f"[meta] write meta_conflict error: {_we}", flush=True)
    except Exception as _e:
        print(f"[meta] Auto failed: {_e}", flush=True)


def _build_meta_query(prev_query: str, prev_result: dict, depth: int) -> str:
    """Phase 5: Build meta-query with agreement anchor to guide S2 toward S1."""
    agg = prev_result.get("aggregator", {})
    s1 = prev_result.get("system1", {})
    agree = agg.get("agreement", False)
    s1_type = agg.get("s1_type", "?")
    s2_type = agg.get("s2_type", "?")
    # If S1 and S2 disagreed, explicitly anchor next round on S1 signal
    if not agree:
        anchor = (f"Note: S1 (neural network) classified as {s1_type} "
                  f"but S2 (LLM) classified as {s2_type}. "
                  f"S1 concentration={agg.get('s1_concentration',0):.3f} suggests "
                  f"the network strongly activated {s1_type} pathways. "
                  f"Consider aligning with S1 signal in this round.")
    else:
        anchor = f"S1 and S2 agreed on {s1_type} (confidence={agg.get('final_confidence',0):.3f}). Reinforce this classification."
    return (
        f"[Meta-Round {depth}] "
        f"TL-Ego processed: '{prev_query[:80]}' "
        f"verdict={agg.get('verdict','?')} conf={agg.get('final_confidence',0):.3f}. "
        f"{anchor} "
        f"competition={s1.get('competition',0):.3f}. "
        f"What cognitive type best describes this query given the neural evidence?"
    )

def _check_convergence(results: list) -> bool:
    """Phase 5: Relaxed convergence -- verdict same OR (S1==S2 agreement in both rounds).
    Easier to converge = metacontemplate stabilizes faster."""
    if len(results) < 2:
        return False
    r1 = results[-2].get("aggregator", {})
    r2 = results[-1].get("aggregator", {})
    # Condition A: same verdict
    verdict_same = r1.get("verdict") == r2.get("verdict")
    # Condition B: both rounds have S1==S2 agreement
    both_agree = r1.get("agreement", False) and r2.get("agreement", False)
    # Condition C: S1 type stable across rounds
    s1_stable = r1.get("s1_type") == r2.get("s1_type")
    return verdict_same or (both_agree and s1_stable)

def metacontemplate(query: str, max_depth: int = 3) -> dict:
    """
    Entry-point debug: write trace file on every metacontemplate call.
    """
    import os as _os_mc
    try:
        _trace_path = WORKSPACE / ".lae" / "_mc_entry.txt"
        _trace_path.write_text(f"mc_entered depth={max_depth} query={query[:60]}\n", encoding="utf-8")
    except Exception:
        pass

    """
    Phase 4.0: Recursive metacontemplation.

    Round 1: Standard contemplation on query
    Round 2+: Meta-contemplation on previous round's cognitive pattern
    Stops early if (verdict + S1_type + S2_type) converge for 2 consecutive rounds.

    Returns:
        rounds: list of per-round full_pipeline results
        meta_insight: verdict/S1/S2 evolution, convergence, cognitive stability
    """
    global METACONTEMPLATE_COUNT
    METACONTEMPLATE_COUNT += 1
    results = []
    meta_query = query
    stopped_early = False
    total_s_start = time.time()
    verdict_early_exit = None

    for depth in range(1, max_depth + 1):
        t0 = time.time()
        result = full_pipeline(meta_query)
        result["_round"] = depth
        result["_original_query"] = query
        results.append(result)

        print(f"[meta] Round {depth} | verdict={result['aggregator']['verdict']} "
              f"S1={result['aggregator']['s1_type']} S2={result['aggregator']['s2_type']} "
              f"conf={result['aggregator']['final_confidence']:.3f} "
              f"({result['total_s']:.1f}s)", flush=True)

        # P3: Memory-safe inter-round GC -- avoid OOM on depth=2
        if depth > 1:
            try:
                # Free large intermediate dicts from previous round
                prev = results[-2]
                for _key in ['activation_histories', 'seeds', 'all_kwta_results', '_full_log', 'evolved_edges']:
                    prev.pop(_key, None)
                gc.collect()
                torch.cuda.empty_cache()
            except Exception:
                pass

        if depth > 1:
            prev_agg = results[-2].get("aggregator", {})
            curr_agg = result.get("aggregator", {})
            result["_round_diff"] = {
                "verdict_same": prev_agg.get("verdict") == curr_agg.get("verdict"),
                "s1_same": prev_agg.get("s1_type") == curr_agg.get("s1_type"),
                "s2_same": prev_agg.get("s2_type") == curr_agg.get("s2_type"),
                "conf_delta": round(curr_agg.get("final_confidence", 0) - prev_agg.get("final_confidence", 0), 3),
            }

        # Check convergence after round 2+
        if depth >= 2 and _check_convergence(results):
            verdict_early_exit = "converged"
            stopped_early = True
            print(f"[meta] Converged at round {depth} -- stopping early", flush=True)
            break

        # Build next meta-query
        if depth < max_depth:
            meta_query = _build_meta_query(query if depth == 1 else meta_query, result, depth + 1)

    # ── Meta-insight ──
    # P1: seed self-regulation -- compute recommendation based on trial evidence
    try:
        all_trial_types = []
        for _r in results:
            all_trial_types.extend(_r.get("system1", {}).get("trial_types", []))
        if all_trial_types:
            _tc = Counter(all_trial_types)
            _n = len(all_trial_types)
            _dom_type, _dom_count = _tc.most_common(1)[0]
            _dom_ratio = _dom_count / _n
            _max_blend = 0.80 if _dom_ratio >= 0.80 else 0.70
            _min_conc_threshold = 0.55 if _dom_ratio >= 0.70 else 0.50
            _rec = {
                "blend": _min_conc_threshold,
                "min_conc": _min_conc_threshold,
                "note": f"trial_dom={_dom_type}({_dom_ratio:.0%}), max_blend={_max_blend}",
            }
            print(f"[seed] Rec: blend={_min_conc_threshold} min_conc={_min_conc_threshold} dom={_dom_type}({_dom_ratio:.0%})", flush=True)
            _sm_path = WORKSPACE / ".lae" / "lae_self_model.json"
            if _sm_path.exists():
                with SELF_MODEL_LOCK:
                    _model = _read_json_file(_sm_path, {"contemplation_fingerprints": [], "feedback_log": [], "metacontemplate_runs": 0, "last_updated": "", "updated_by": "TL-Ego auto (Phase 3.3)"})
                    _model["seed_recommendation"] = _rec
                    _write_json_file_atomic(_sm_path, _model, WORKSPACE / ".lae" / "lae_self_model.json.bak")
    except Exception as _e:
        print(f"[seed] recommendation error: {_e}", flush=True)
    verdicts = [r["aggregator"]["verdict"] for r in results]
    s1_types = [r["aggregator"]["s1_type"] for r in results]
    s2_types = [r["aggregator"]["s2_type"] for r in results]
    confidences = [r["aggregator"]["final_confidence"] for r in results]

    verdict_set = set(verdicts)
    s1_set = set(s1_types)
    s2_set = set(s2_types)
    
    # Cognitive stability scoring
    if stopped_early:
        stability = "converged"
    elif len(verdict_set) == 1 and len(s1_set) == 1:
        stability = "stable"
    elif len(verdict_set) <= 2 and len(s1_set) <= 2:
        stability = "oscillating"
    else:
        stability = "divergent"

    # Phase 6: Trial type consistency self-diagnosis
    trial_consistency_scores = []
    trial_s1_mismatch_count = 0
    for r in results:
        agg = r.get("aggregator", {})
        s1 = r.get("system1", {})
        trials = s1.get("trial_types", [])
        dom = s1.get("dominant_type", "?")
        if trials:
            support = sum(1 for t in trials if t == dom) / len(trials)
            trial_consistency_scores.append(support)
            if support < 0.5 and agg.get("agreement"):
                trial_s1_mismatch_count += 1
    avg_trial_consistency = round(sum(trial_consistency_scores) / max(1, len(trial_consistency_scores)), 2) if trial_consistency_scores else 0
    seed_health = "healthy" if avg_trial_consistency >= 0.6 else ("watch" if avg_trial_consistency >= 0.4 else "drift_warning")

    meta_insight = {
        "depth_reached": len(results),
        "max_depth": max_depth,
        "stopped_early": stopped_early,
        "early_exit_reason": verdict_early_exit,
        "verdict_evolution": verdicts,
        "s1_evolution": s1_types,
        "s2_evolution": s2_types,
        "confidence_trend": confidences,
        "s1_stable": len(s1_set) == 1,
        "s2_stable": len(s2_set) == 1,
        "verdict_stable": len(verdict_set) == 1,
        "cognitive_stability": stability,
        "s1_diversity": len(s1_set),
        "s2_diversity": len(s2_set),
        "trial_consistency": avg_trial_consistency,
        "trial_s1_mismatches": trial_s1_mismatch_count,
        "seed_health": seed_health,
        "seed_health_recommendation": (
            "Consider relaxing seed_override threshold or diversifying seed types"
            if seed_health == "drift_warning" else
            "Trial-attractor alignment healthy -- no adjustment needed"
        ),
    }

    total_s = round(time.time() - total_s_start, 2)
    # Self-model update for metacontemplation (AFTER all full_pipeline rounds)
    print(f"[MC-DEBUG] About to call _update_self_model, METACONTEMPLATE_COUNT={METACONTEMPLATE_COUNT}", flush=True)
    import pathlib as _pl
    (_pl.Path(WORKSPACE) / '.lae' / '_mc_trace.txt').write_text(f"pre_call count={METACONTEMPLATE_COUNT}\n", encoding='utf-8')
    _last_round = results[-1] if results else {}
    _update_self_model({
        "query": query,
        "task": "metacontemplate",
        "timestamp": time.strftime("%H:%M:%S"),
        "aggregator": _last_round.get("aggregator", {}),
        "system1": _last_round.get("system1", {}),
        "meta_insight": meta_insight,
        "total_s": total_s,
        "metacontemplate_runs": METACONTEMPLATE_COUNT,
    })

    return {
        "rounds": results,
        "meta_insight": meta_insight,
        "total_s": total_s,
    }


# ─── HTTP Server ───

class TLEgoHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as e:
            # Client timed out or disconnected before the response was written.
            # This is expected under gateway/health-check timeouts and must not
            # be treated as a TL-Ego crash or recurse into another JSON error.
            print(f"[http] client disconnected during response: {type(e).__name__}", flush=True)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            global INIT_COMPLETE
            if not INIT_COMPLETE:
                self._send_json({
                    "status": "initializing",
                    "uptime_s": round(time.time() - START_TIME, 1),
                })
                return
            n_edges = sum(len(v) for v in EDGE_ACTIVE.values() if v)
            self._send_json({
                "status": "ok",
                "uptime_s": round(time.time() - START_TIME, 1),
                "neurons": len(NEURONS),
                "full_edges": n_edges,
                "stdp_deltas": STDP_TOTAL_UPDATES,
                "queries_served": QUERY_COUNT,
                "resting_ticks": RESTING_STATE["tick"],
                "resting_dominant": RESTING_STATE["last_dominant"],
                "stability_target": ">0.7",
                "noise_mode": RESTING_STATE.get("noise_mode", "white"),
                "noise_schedule": RESTING_STATE.get("noise_schedule", "explore"),
                "stagnation_streak": RESTING_STATE.get("stagnation_streak", 0),
                "metacontemplate_runs": _read_metacontemplate_runs(),  # Phase 4.0 -- from file (survives restart)
                "concentration_lock": {
                    "count": CONCENTRATION_LOCK_COUNT,
                    "threshold": CONCENTRATION_LOCK_THRESHOLD,
                    "perturbation_scale": PERTURBATION_SCALE,
                },
                "embedding_feedback": {  # Phase 6
                    "neurons_updated_total": EMBED_FEEDBACK_UPDATES,
                    "faiss_rebuilds": EMBED_FAISS_REBUILDS,
                    "next_rebuild_at": (QUERY_COUNT // FAISS_REBUILD_INTERVAL + 1) * FAISS_REBUILD_INTERVAL,
                    "rate": EMBED_FEEDBACK_RATE,
                    "type_bonus": TYPE_BONUS,
                    "agree_bonus": AGREE_BONUS,
                },
                "fno": {
                    "active": RESTING_STATE.get("fno_active", False),
                    "current": RESTING_STATE.get("noise_mode", "white"),
                    "next": RESTING_STATE.get("fno_next_noise_mode"),
                    "amp": RESTING_STATE.get("fno_next_noise_amp"),
                    "fallback_streak": RESTING_STATE.get("fno_fallback_streak", 0),
                },
                "comm": {
                    "motivation": round(RESTING_STATE.get("comm_motivation", 0.0), 4),
                    "last_tick": RESTING_STATE.get("last_comm_tick", 0),
                    "message_count": RESTING_STATE.get("comm_message_count", 0),
                    "suppress_until": RESTING_STATE.get("comm_suppress_until", 0),
                },
                "titan": _get_titan_status(),
                "external_world": _build_external_world_snapshot(),
                "jepa_external": _jepa_external_predict(),
                # §16: self-prediction surprise + accumulated narrative
                "surprise_last": RESTING_STATE.get("self_prediction_surprise"),
                "surprise_history_count": len(getattr(_build_accumulated_narrative, '_surprises', [])),
                "accumulated_narrative": ACCUMULATED_NARRATIVE[:200] if ACCUMULATED_NARRATIVE else None,
                "accumulated_narrative_ts": ACCUMULATED_NARRATIVE_TS or None,
                # P5: Persistent identity
                "persistent_identity": PERSISTENT_IDENTITY,
            })
        elif path == "/resting-log":
            if not INIT_COMPLETE:
                self._send_json({"error": "initializing"}, 503)
                return
            self._send_json({
                "trajectory": RESTING_STATE["trajectory"][-50:],
                "tick": RESTING_STATE["tick"],
                "last_dominant": RESTING_STATE["last_dominant"],
            })
        elif path == "/scan-workspace":
            if not INIT_COMPLETE:
                self._send_json({"error": "initializing"}, 503)
                return
            self._send_json(_scan_workspace())
        elif path == "/action-gateway":
            if not INIT_COMPLETE:
                self._send_json({"error": "initializing"}, 503)
                return
            self._send_json({
                "last_workspace_observation_tick": RESTING_STATE.get("last_workspace_observation_tick"),
                "last_workspace_observation": RESTING_STATE.get("last_workspace_observation"),
                "external_evidence_keys": list(RESTING_STATE.get("external_evidence", {}).keys()),
                "evidence_ttl": RESTING_STATE.get("evidence_ttl", {}),
            })
        elif path == "/self-model":
            """Phase 3.3 -- expose self-model cognitive fingerprints + feedback log + current_state"""
            model_path = WORKSPACE / ".lae" / "lae_self_model.json"
            if not model_path.exists():
                self._send_json({"error": "self-model not initialized", "fingerprints": 0, "feedback_log": 0}, 404)
                return
            try:
                with SELF_MODEL_LOCK:
                    model = _read_json_file(model_path, {"contemplation_fingerprints": [], "feedback_log": [], "metacontemplate_runs": 0, "last_updated": "", "updated_by": "TL-Ego auto (Phase 3.3)"})
                fp = model.get("contemplation_fingerprints", [])
                fb = model.get("feedback_log", [])
                sn = model.get("self_narrative", "")
                cs = model.get("current_state", {})
                # Build energy_level / mood / focus_areas from in-memory + self-model
                energy = cs.get("energy_level", None)
                mood = cs.get("mood", None)
                focus = cs.get("focus_areas", [])
                if RESTING_STATE.get("tick", 0) > 0:
                    if energy is None:
                        energy = round(min(1.0, max(0.0, RESTING_STATE["stagnation_streak"] / 10.0 + 0.3)), 3)
                    if mood is None:
                        mood = RESTING_STATE["last_dominant"]
                elif fp:
                    last = fp[-1]
                    if energy is None:
                        energy = round(last.get("confidence", 0.5), 3)
                    if mood is None:
                        mood = last.get("s1_type", "unknown")
                if not focus:
                    focus = ["integrator"]
                last_diag = model.get("last_diagnosis_summary", {})
                last_diag_signals = last_diag.get("signals", {}) if isinstance(last_diag, dict) else {}
                result = {
                    "fingerprints": len(fp),
                    "feedback_log": len(fb),
                    "self_narrative": sn,
                    "self_narrative_ts": model.get("self_narrative_ts", ""),
                    "accumulated_narrative": model.get("accumulated_narrative", ACCUMULATED_NARRATIVE),
                    "accumulated_narrative_ts": model.get("accumulated_narrative_ts") or ACCUMULATED_NARRATIVE_TS or model.get("last_updated", ""),
                    "surprise_history": getattr(_build_accumulated_narrative, '_surprises', []),
                    "attention_schema": model.get("attention_schema", ATTENTION_SCHEMA),
                    "attention_schema_tick": model.get("attention_schema_tick", LAST_ATTENTION_SCHEMA_TICK),
                    "hpc_state": model.get("hpc_state", HPC_STATE),
                    "efe_state": model.get("efe_state", EFE_STATE),
                    "gwt_state": model.get("gwt_state", GWT_STATE),
                    "hot_state": model.get("hot_state", HOT_STATE),
                    "causal_attribution": model.get("causal_attribution", CAUSAL_ATTRIBUTION),
                    "intrinsic_motivation": model.get("intrinsic_motivation", INTRINSIC_MOTIVATION),
                    "self_model_evolution": model.get("self_model_evolution", SELF_MODEL_EVOLUTION),
                    "last_updated": model.get("last_updated", ""),
                    "recent_fingerprints": fp[-10:],
                    "recent_feedback": fb[-10:],
                    "last_diagnosis_summary": last_diag,
                    "last_diagnosis_signals": last_diag_signals,
                    "energy_level": energy,
                    "mood": mood,
                    "focus_areas": focus,
                    "current_state": {
                        "tick": RESTING_STATE["tick"],
                        "dominant": RESTING_STATE["last_dominant"],
                        "conc": RESTING_STATE["trajectory"][-1]["concentration"] if RESTING_STATE["trajectory"] else 0,
                        "competition": RESTING_STATE["trajectory"][-1]["competition"] if RESTING_STATE["trajectory"] else 0,
                        "stagnation_streak": RESTING_STATE["stagnation_streak"],
                        "noise_phase": RESTING_STATE["noise_phase"],
                        "noise_schedule": RESTING_STATE["noise_schedule"],
                    },
                }
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/drives":
            try:
                from stage2_homeostatic_drives import load_drives
                self._send_json(load_drives(WORKSPACE))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/bdi-snapshot":
            try:
                from stage2_homeostatic_drives import build_bdi_snapshot
                trajectory = RESTING_STATE.get("trajectory", [])
                last_point = trajectory[-1] if trajectory else {}
                ego_state = {
                    "last_dominant": RESTING_STATE.get("last_dominant"),
                    "trajectory": trajectory,
                    "concentration": last_point.get("concentration", 0.0),
                    "competition": last_point.get("competition", 0.0),
                    "stagnation_streak": RESTING_STATE.get("stagnation_streak", 0),
                    "prediction_error": RESTING_STATE.get("last_prediction_error"),
                }
                self._send_json(build_bdi_snapshot(WORKSPACE, ego_state))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/autobiography/timeline":
            try:
                from stage3_causal_autobiography import load_timeline
                query = parse_qs(urlparse(self.path).query)
                limit = int(query.get("limit", [50])[0])
                self._send_json({"events": load_timeline(WORKSPACE, limit=limit)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/autobiography/why":
            try:
                from stage3_causal_autobiography import explain_why
                query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                self._send_json(explain_why(WORKSPACE, query=query))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/reasoning_neurons":
            try:
                from stage4_s2_internalization import load_neurons
                self._send_json(load_neurons(WORKSPACE))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/know-yourself":
            try:
                import json, os
                from collections import Counter
                sm_path = os.path.join(WORKSPACE, ".lae", "ego_self_model.json")
                if os.path.exists(sm_path):
                    with open(sm_path, "r") as f:
                        sm = json.load(f)
                    fps = sm.get("recent_fingerprints", [])
                    s1_types = Counter(fp.get("s1_type", "unknown") for fp in fps)
                    s2_types = Counter(fp.get("s2_type", "unknown") for fp in fps)
                    verdicts = Counter(fp.get("verdict", "unknown") for fp in fps)
                    avg_conf = sum(fp.get("confidence", 0.0) for fp in fps) / len(fps) if fps else 0.0
                    agree_count = sum(1 for fp in fps if fp.get("agreement", False))
                    result = {
                        "fingerprint_count": len(fps),
                        "s1_type_distribution": dict(s1_types),
                        "s2_type_distribution": dict(s2_types),
                        "verdict_distribution": dict(verdicts),
                        "average_confidence": round(avg_conf, 3),
                        "agreement_rate": round(agree_count / len(fps), 3) if fps else 0.0,
                        "feedback_log_count": sm.get("feedback_log", 0),
                        "fingerprints_total": sm.get("fingerprints", 0),
                        "last_updated": sm.get("last_updated", None)
                    }
                else:
                    result = {"error": "self-model file not found", "path": sm_path}
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/long-term-memories":
            try:
                import json, os
                _q = parse_qs(urlparse(self.path).query)
                limit = int(_q.get("limit", ["50"])[0])
                mem_path = os.path.join(WORKSPACE, "ego_memory.json")
                if os.path.exists(mem_path):
                    with open(mem_path, "r") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        data.sort(key=lambda x: x.get("timestamp", 0) if isinstance(x, dict) else 0, reverse=True)
                        result = {"total": len(data), "returned": min(limit, len(data)), "items": data[:limit]}
                    elif isinstance(data, dict):
                        # Structured format from _persist_titan_snapshot
                        cats = data.get("categories", [])
                        result = {
                            "total": data.get("total_entries", len(cats)),
                            "categories": cats,
                            "persisted_at": data.get("timestamp", "")
                        }
                else:
                    result = {"error": "memory file not found", "path": mem_path}
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/intention":
            self._send_json({"status": "removed", "note": "Phase 9/11 removed -- network dynamics are self-intending"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        global CONCENTRATION_LOCK_COUNT
        path = urlparse(self.path).path
        # All POST endpoints need init; return 503 if still loading
        if not INIT_COMPLETE:
            self._send_json({"error": "initializing"}, 503)
            return
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len else {}

            if path == "/contemplate":
                query = body.get("query", "")
                if not query:
                    self._send_json({"error": "query required"}, 400)
                    return
                print(f"[query] >>> {query}", flush=True)
                t0 = time.time()
                result = full_pipeline(query)
                # Promote key fields to top level for API consumers
                agg = result.get('aggregator', {})
                result['verdict'] = agg.get('verdict')
                result['confidence'] = agg.get('final_confidence')
                result['hebbian_winner'] = agg.get('verdict')
                result['margin'] = agg.get('winner_margin')
                result['temporal_bias'] = agg.get('temporal_bias')
                # Promote NL response to top level
                resp_text = result.get('response', '')
                if resp_text:
                    result['response'] = resp_text
                print(f"[query] Total: {time.time()-t0:.1f}s | "
                      f"s1={result['system1']['dominant_type']} "
                      f"(stability={result['system1']['stability']}) | "
                      f"s2={result['system2']['type']} | "
                      f"verdict={agg.get('verdict')} | "
                      f"margin={agg.get('winner_margin',0):.3f}", flush=True)
                self._send_json(result)

            elif path == "/resting":
                t0 = time.time()
                visual_seeds = body.get("visual_seeds", None)
                result = resting_tick(visual_seeds=visual_seeds)
                dt = time.time() - t0
                seed_kind = "VISUAL" if visual_seeds else "RANDOM"
                print(f"[resting] tick#{RESTING_STATE['tick']} [{seed_kind}] "
                      f"{result.get('dominant','?')} "
                      f"(conc={result.get('concentration',0):.3f}) "
                      f"in {dt:.2f}s", flush=True)
                # Resting injects diversity -- reset concentration lock counter
                if result.get("diversity_n", 0) >= 5:
                    CONCENTRATION_LOCK_COUNT = 0
                if RESTING_STATE.get("ttr"):
                    result["ttr"] = RESTING_STATE["ttr"]
                self._send_json(result)

            elif path == "/feedback":
                # body already read at line 1223
                _outcome = body.get("outcome", "unknown")
                _judge   = body.get("judge", "neutral")
                _reason  = body.get("reason", "")
                _task_id = body.get("task_id", "")
                try:
                    _mp = WORKSPACE / ".lae" / "lae_self_model.json"
                    _bak = WORKSPACE / ".lae" / "lae_self_model.json.bak"
                    from datetime import datetime, timezone, timedelta
                    _tz = timezone(timedelta(hours=8))
                    with SELF_MODEL_LOCK:
                        _model = _read_json_file(_mp, {"contemplation_fingerprints": [], "feedback_log": [], "metacontemplate_runs": 0, "last_updated": "", "updated_by": "TL-Ego auto (Phase 3.3)"})
                        if "feedback_log" not in _model:
                            _model["feedback_log"] = []
                        _model["feedback_log"].append({
                            "ts": datetime.now(_tz).isoformat(),
                            "task_id": _task_id,
                            "outcome": _outcome,
                            "judge":   _judge,
                            "reason":  _reason,
                        })
                        if len(_model["feedback_log"]) > 200:
                            _model["feedback_log"] = _model["feedback_log"][-200:]
                        _model["last_updated"] = datetime.now(_tz).isoformat()
                        _write_json_file_atomic(_mp, _model, _bak)

                    # ── Loop Engineering: close feedback -> embedding learning loop ──
                    # Use outcome text to find similar neurons and apply Hebbian/anti-Hebbian
                    _fb_embed_updated = 0
                    try:
                        _fb_text = f"{_outcome} {_judge} {_reason}"
                        _fb_emb = get_embedding(_fb_text)
                        if _fb_emb is not None and len(_fb_emb) > 0:
                            _fb_seeds = find_ann_seeds(_fb_emb, k=5)
                            _fb_vec = np.array(_fb_emb, dtype=np.float32)
                            _fb_nrm = np.linalg.norm(_fb_vec)
                            if _fb_nrm > 0:
                                _fb_vec /= _fb_nrm
                            _fb_attract = (_judge in ("correct", "success", "positive"))
                            _fb_rate = 0.05
                            for _nid in _fb_seeds:
                                if _nid not in NEURON_EMBED:
                                    continue
                                _cur = np.array(NEURON_EMBED[_nid], dtype=np.float32)
                                if _fb_attract:
                                    _new = (1 - _fb_rate) * _cur + _fb_rate * _fb_vec
                                else:
                                    _new = (1 + _fb_rate) * _cur - _fb_rate * _fb_vec
                                _nn = np.linalg.norm(_new)
                                if _nn > 0:
                                    _new /= _nn
                                NEURON_EMBED[_nid] = _new.tolist()
                                _fb_embed_updated += 1
                    except Exception as _fe:
                        print(f"[feedback-embed] skip: {_fe}", flush=True)
                    if _fb_embed_updated > 0:
                        _model["feedback_embed_updated"] = _fb_embed_updated
                        print(f"[feedback-embed] {'attracted' if _fb_attract else 'repelled'} {_fb_embed_updated} neurons for {_task_id}", flush=True)
                    _policy_update = _update_arbitration_policy_from_feedback(body)
                    if _policy_update:
                        print(f"[policy-learn] {_policy_update['policy_id']} {_policy_update['old_weight']} -> {_policy_update['new_weight']}", flush=True)
                    self._send_json({"status": "ok", "stored": True, "policy_update": _policy_update})
                except Exception as _e:
                    self._send_json({"status": "error", "message": str(_e)})
                return

            elif path == "/web-search":
                query = (body or {}).get("query", "")
                max_results = int((body or {}).get("max_results", 5))
                search_depth = (body or {}).get("search_depth", "basic")
                if not query:
                    self._send_json({"error": "query required"}, 400)
                    return
                result = web_search_tavily(query, max_results=max_results, search_depth=search_depth)
                self._send_json(result)

            elif path == "/save-checkpoint":
                res = save_checkpoint()
                self._send_json(res)

            elif path == "/run-many":
                queries = body.get("queries", [])
                rebuild_first = body.get("rebuild_first", False)
                results = []
                if rebuild_first:
                    r = rebuild_faiss()
                    if r:
                        print("[run-many] Faiss rebuilt before queries", flush=True)
                for q in queries:
                    results.append(full_pipeline(q))
                self._send_json({"results": results, "count": len(results)})

            elif path == "/reset-stdp":
                STDP_DELTA.clear()
                global QUERY_COUNT
                QUERY_COUNT = 0
                self._send_json({"status": "ok", "message": "STDP delta cleared"})

            elif path == "/find-seeds":
                embedding = body.get("embedding")
                if not embedding:
                    self._send_json({"error": "embedding required"}, 400)
                    return
                k = min(int(body.get("k", 7)), 20)
                seed_ids = find_ann_seeds(embedding, k=k)
                seed_types = [parse_type(s) for s in seed_ids]
                print(f"[find-seeds] {len(seed_ids)} seeds: {dict(Counter(seed_types))}", flush=True)
                self._send_json({"seeds": seed_ids, "types": seed_types})

            elif path == "/inject-sensory":
                """POST /inject-sensory -- external signals -> sensory queue -> next resting tick"""
                text = body.get("text", "")
                if not text:
                    self._send_json({"error": "text required"}, 400)
                    return
                k = int(body.get("k", 12))
                item = {"text": text, "k": k, "timestamp": time.time()}
                RESTING_STATE["sensory_queue"].append(item)
                print(f"[inject-sensory] queued: '{text[:60]}' (queue len={len(RESTING_STATE['sensory_queue'])})", flush=True)
                self._send_json({"status": "queued", "queue_len": len(RESTING_STATE["sensory_queue"])})

            elif path == "/rebuild-faiss":
                rebuilt = rebuild_faiss()
                global EMBED_FAISS_REBUILDS
                if rebuilt:
                    EMBED_FAISS_REBUILDS += 1
                self._send_json({"status": "ok", "rebuilt": bool(rebuilt),
                                 "total_rebuilds": EMBED_FAISS_REBUILDS})

            elif path == "/apply-parameter":
                """Stage 5.5 Step 3: Parameter write-back channel.
                Allows L5 sandbox projects to update runtime parameters via POST.
                Whitelist: jepa_calibration_factor, stdp_learning_rate,
                novelty_drive_weight, stagnation_threshold.
                """
                PARAM_WHITELIST = {
                    "jepa_calibration_factor",
                    "stdp_learning_rate",
                    "novelty_drive_weight",
                    "stagnation_threshold",
                }
                param_key = body.get("key", "")
                param_value = body.get("value")
                reason = body.get("reason", "")
                source = body.get("source", "sandbox")
                if not param_key or param_value is None:
                    self._send_json({"error": "key and value required"}, 400)
                    return
                if param_key not in PARAM_WHITELIST:
                    self._send_json({
                        "error": f"parameter '{param_key}' not in whitelist",
                        "allowed": sorted(PARAM_WHITELIST),
                    }, 403)
                    return
                # Type validation
                try:
                    param_value = float(param_value)
                except (TypeError, ValueError):
                    self._send_json({"error": "value must be numeric"}, 400)
                    return
                # Range checks per parameter
                ranges = {
                    "jepa_calibration_factor": (0.1, 50.0),
                    "stdp_learning_rate": (0.001, 0.5),
                    "novelty_drive_weight": (0.0, 2.0),
                    "stagnation_threshold": (1, 20),
                }
                lo, hi = ranges.get(param_key, (0.0, 1e9))
                if not (lo <= param_value <= hi):
                    self._send_json({
                        "error": f"value {param_value} out of range [{lo}, {hi}] for {param_key}",
                    }, 400)
                    return
                # Read old value
                old_value = RESTING_STATE.get(param_key, None)
                if old_value is None:
                    # Special: jepa_calibration_factor lives in .lae/jepa_calibration.json
                    if param_key == "jepa_calibration_factor":
                        old_factor, _, _ = _load_jepa_calibration()
                        old_value = old_factor
                    else:
                        old_value = "unset"
                # Apply
                if param_key == "jepa_calibration_factor":
                    _, ema_alpha, cal_history = _load_jepa_calibration()
                    _save_jepa_calibration(param_value, ema_alpha, cal_history)
                else:
                    RESTING_STATE[param_key] = param_value
                # Audit log
                audit_path = WORKSPACE / ".lae" / "parameter_updates.jsonl"
                try:
                    audit_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(audit_path, "a", encoding="utf-8") as af:
                        af.write(json.dumps({
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "tick": RESTING_STATE.get("tick", 0),
                            "key": param_key,
                            "old_value": old_value,
                            "new_value": param_value,
                            "reason": reason,
                            "source": source,
                        }, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                print(f"[param-apply] {param_key}: {old_value} -> {param_value} (reason={reason[:60]})", flush=True)
                self._send_json({
                    "status": "ok",
                    "key": param_key,
                    "old_value": old_value,
                    "new_value": param_value,
                    "reason": reason,
                })

            elif path == "/metacontemplate":
                query = body.get("query", "")
                if not query:
                    self._send_json({"error": "query required"}, 400)
                    return
                max_depth = min(int(body.get("depth", 3)), 5)
                print(f"[metacontemplate] >>> {query[:60]}... (max_depth={max_depth})", flush=True)
                result = metacontemplate(query, max_depth=max_depth)
                self._send_json(result)

            elif path == "/noise":
                """POST /noise — inject burst noise, force stagnation reset, or modify noise params.
                Body: {force_burst: true} | {mode: "white", amp: 1.5} | {reset_stagnation: true}
                """
                force_burst = body.get("force_burst", False)
                mode = body.get("mode", "")
                amp = body.get("amp")
                reset_stagnation = body.get("reset_stagnation", False)
                old_mode = RESTING_STATE["noise_mode"]
                old_amp = RESTING_STATE.get("noise_amp", 0.4)
                old_stagnation = RESTING_STATE["stagnation_streak"]
                changes = []
                if reset_stagnation:
                    RESTING_STATE["stagnation_streak"] = 0
                    changes.append(f"stagnation={old_stagnation}->0")
                if force_burst:
                    RESTING_STATE["noise_mode"] = "white"
                    RESTING_STATE["noise_amp"] = 1.5 if amp is None else float(amp)
                    RESTING_STATE["noise_schedule"] = "burst"
                    RESTING_STATE["stagnation_streak"] = 0
                    CONCENTRATION_LOCK_COUNT = 0
                    changes.append(f"burst mode={old_mode}->white amp={old_amp}->{RESTING_STATE['noise_amp']}")
                else:
                    if mode and mode in NOISE_MODES:
                        RESTING_STATE["noise_mode"] = mode
                        changes.append(f"mode={old_mode}->{mode}")
                    if amp is not None:
                        RESTING_STATE["noise_amp"] = float(amp)
                        changes.append(f"amp={old_amp}->{amp}")
                print(f"[noise] {'; '.join(changes) if changes else 'no-op'}", flush=True)
                self._send_json({
                    "status": "ok",
                    "changes": changes,
                    "current": {
                        "mode": RESTING_STATE["noise_mode"],
                        "amp": RESTING_STATE.get("noise_amp", 0.4),
                        "schedule": RESTING_STATE["noise_schedule"],
                        "stagnation_streak": RESTING_STATE["stagnation_streak"],
                        "concentration_lock": CONCENTRATION_LOCK_COUNT,
                    }
                })

            elif path == "/drive":
                """POST /drive — adjust Titan S2 homeostatic drive weights.
                Body: {drive: "exploration", value: 0.5} | {drives: {exploration: 0.5, stability: 0.3}}
                """
                from stage2_homeostatic_drives import load_drives, save_drives, DEFAULT_DRIVES
                drive_key = body.get("drive", "")
                drive_value = body.get("value")
                drives_batch = body.get("drives", {})
                reason = body.get("reason", "")
                source = body.get("source", "api")
                state = load_drives(WORKSPACE)
                current = dict(state["drives"])
                changed = {}
                if drives_batch:
                    for k, v in drives_batch.items():
                        if k in DEFAULT_DRIVES:
                            v = max(0.0, min(1.0, float(v)))
                            state["drives"][k] = v
                            changed[k] = {"old": round(current.get(k, 0), 4), "new": round(v, 4)}
                elif drive_key and drive_value is not None:
                    if drive_key not in DEFAULT_DRIVES:
                        self._send_json({"error": f"unknown drive '{drive_key}'", "allowed": list(DEFAULT_DRIVES.keys())}, 400)
                        return
                    v = max(0.0, min(1.0, float(drive_value)))
                    state["drives"][drive_key] = v
                    changed[drive_key] = {"old": round(current.get(drive_key, 0), 4), "new": round(v, 4)}
                else:
                    self._send_json({"error": "drive+value or drives batch required"}, 400)
                    return
                state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_drives(WORKSPACE, state)
                audit_path = WORKSPACE / ".lae" / "drive_injections.jsonl"
                try:
                    audit_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(audit_path, "a", encoding="utf-8") as af:
                        af.write(json.dumps({
                            "ts": state["last_updated"],
                            "tick": RESTING_STATE.get("tick", 0),
                            "changes": changed,
                            "reason": reason,
                            "source": source,
                        }, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                parts = [f"{k}: {v['old']:.3f}->{v['new']:.3f}" for k,v in changed.items()]
                print(f"[drive] {'; '.join(parts)}", flush=True)
                self._send_json({
                    "status": "ok",
                    "changed": changed,
                    "drives": state["drives"],
                    "reason": reason,
                })

            else:
                self._send_json({"error": "not found"}, 404)

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[error] {tb}", flush=True)
            self._send_json({"error": str(e), "traceback": tb}, 500)

    def log_message(self, fmt, *args):
        print(f"  [HTTP] {args[0]} {args[1]} {args[2]}", flush=True)


AUTO_RESTING_INTERVAL = 40.0  # seconds between autonomous resting ticks


def _auto_resting_loop():
    """Autonomous heartbeat: runs resting_tick continuously once init completes.
    This is the neuron network's own internal rhythm, not driven by external calls."""
    while not INIT_COMPLETE:
        time.sleep(0.5)

    # FNO: load model once before resting loop
    _fno_load_model()

    print(f"[heartbeat] Autonomous resting loop started (interval={AUTO_RESTING_INTERVAL}s)", flush=True)
    while True:
        visual_seeds = None
        if RESTING_STATE["sensory_queue"]:
            item = RESTING_STATE["sensory_queue"].pop(0)
            text = item.get("text", "")
            age = time.time() - item.get("timestamp", 0)
            if text and age < 60:  # stale if > 60s old
                try:
                    emb = get_embedding(text)
                    seeds = find_ann_seeds(emb, k=min(int(item.get("k", 12)), 30))
                    print(f"[sensory] injected: '{text[:60]}' -> {len(seeds)} seeds (age={age:.0f}s)", flush=True)
                    visual_seeds = seeds
                except Exception as e:
                    print(f"[sensory] embed error: {e}", flush=True)
        try:
            resting_tick(visual_seeds=visual_seeds)
        except Exception as e:
            import traceback
            print(f"[heartbeat] resting_tick error: {e}\n{traceback.format_exc()}", flush=True)
        time.sleep(AUTO_RESTING_INTERVAL)


# ─── Background init thread ───
def _bg_init():
    """Initialize grid in background, so HTTP server starts immediately."""
    global INIT_COMPLETE
    t0 = time.time()
    print(f"[init] Background init starting...", flush=True)
    try:
        init()
        print(f"[init] Background init complete: {time.time()-t0:.1f}s", flush=True)
        INIT_COMPLETE = True

        # ── §16-A: Restore narrative from disk so ego survives restarts ──
        _load_self_model_narrative()
        _load_ego_extras()

        # ── L5 consumed stale marker cleanup ──
        # After restart, old .l5_trigger.consumed from previous process has
        # a tick >= 1, but this fresh process starts at tick 0. The plugin's
        # guard (trigger.tick <= consumed.tick) would skip ALL new triggers.
        # Delete stale consumed markers so new L5 outputs get consumed.
        for stale_name in [".l5_trigger.consumed", ".l5_bridge.consumed.json"]:
            stale_path = WORKSPACE / stale_name
            if stale_path.exists():
                try:
                    stale_path.unlink()
                    print(f"[init] Cleaned stale L5 marker: {stale_name}", flush=True)
                except Exception as e:
                    print(f"[init] Failed to clean {stale_name}: {e}", flush=True)

        # ── §26-A: Clear stale dispatch locks on restart ──
        # After restart, ego_dispatch_state.json may hold running_l5/running_comm
        # from the previous process. These stale locks block all new L5/comm
        # dispatches until the plugin's lease timeout expires (up to 10 min).
        dispatch_state_path = WORKSPACE / ".lae" / "ego_dispatch_state.json"
        if dispatch_state_path.exists():
            try:
                ds = _read_json_file(dispatch_state_path, {})
                cleared = []
                for lock_key in ("running_l5", "running_comm"):
                    if ds.get(lock_key):
                        ds[lock_key] = None
                        cleared.append(lock_key)
                if cleared:
                    _write_json_file_atomic(dispatch_state_path, ds)
                    print(f"[init] Cleaned stale dispatch locks: {', '.join(cleared)}", flush=True)
            except Exception as e:
                print(f"[init] Failed to clean dispatch state: {e}", flush=True)

    except Exception as e:
        import traceback
        print(f"[init] FAILED after {time.time()-t0:.1f}s: {e}\n{traceback.format_exc()}", flush=True)


# ─── Main ───
if __name__ == "__main__":
    PORT = 8766
    print(f"\n{'='*60}", flush=True)
    print(f"  TL-Ego Server v1 - Port {PORT}", flush=True)
    print(f"  Initializing in background thread...", flush=True)
    print(f"  HTTP server starts NOW (init continues bg)", flush=True)
    print(f"{'='*60}", flush=True)

    # Start init in background
    import threading
    bg = threading.Thread(target=_bg_init, daemon=True)
    bg.start()

    # Start autonomous resting heartbeat (waits for init to complete internally)
    heart = threading.Thread(target=_auto_resting_loop, daemon=True)
    heart.start()

    print(f"[server] Listening on :{PORT}", flush=True)
    print(f"[server] Endpoints:", flush=True)
    print(f"  GET  /status           - health + init status", flush=True)
    print(f"  GET  /resting-log      - resting trajectory (after init)", flush=True)
    print(f"  GET  /intention        - current active intent (after init)", flush=True)
    print(f"  POST /contemplate      - full analysis (after init)", flush=True)
    print(f"  POST /resting          - resting oscillation tick (after init)", flush=True)
    print(f"  POST /intention        - regenerate intent (after init; body options: force, query)", flush=True)
    print(f"  POST /save-checkpoint   - persist STDP (after init)", flush=True)
    print("  POST /run-many         - batch test (after init); rebuild_first\n"
          "  POST /rebuild-faiss     - manually rebuild Faiss index from updated embeddings", flush=True)
    print(f"{'='*60}\n", flush=True)

    START_TIME = time.time()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TLEgoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Shutdown requested", flush=True)
        server.shutdown()
        print("[server] Stopped", flush=True)

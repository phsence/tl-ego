#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 1: Consequence Sensor — Action-Outcome Feedback Loop
泰坦醒觉方案 Stage 1 实现

Purpose:
  闭合 感知-行动-后果 环路，让 Ego 第一次真正感知"我做了什么，世界如何回应"。

Flow:
  1. Subagent 执行 L5 自主冲动后写 .l5_response.json
  2. _consume_action_outcome() 在 resting_tick 末尾被调用
  3. 计算 prediction_error = semantic_distance(expected, actual)
  4. 将 outcome 注入为 external_evidence，strength = f(prediction_error)
  5. 高 prediction_error 时，临时提升 STDP 学习率 ×3
  6. 写入 ego_consumption.jsonl 审计日志

Key Innovation:
  - prediction_error 驱动学习强度（surprise = learning signal）
  - 不依赖外部标签，用语义相似度自动计算误差
  - 结果直接影响下一个 resting tick 的种子选择
"""

import json
import re
import shutil
import time
import traceback
from pathlib import Path
import numpy as np

from runtime_identity import generation_id, load_runtime_identity


# ── Landing gate (2026-09-11) ──
# A "success" that only produced a draft/proposal/undelivered artifact must not
# count as a landed outcome — otherwise the learning loop teaches that writing
# artifacts is the same as changing the world (observed: repair proposals with
# "no runtime changes", messages "drafted, not delivered", graded success).
_NOT_LANDED_MARKERS = (
    "not delivered", "no delivery", "delivery not attempted", "not attempted",
    "artifact_only", "artifact only", "not applied", "no runtime changes",
    "read-only audit", "drafted_only", "draft only",
)


def _not_landed_text(*texts) -> bool:
    """True when the outcome/evidence text admits nothing landed in the world."""
    blob = " ".join(str(t or "") for t in texts).lower()
    return any(m in blob for m in _NOT_LANDED_MARKERS)


def _is_pending_outcome(resp):
    """True when l5_response is only a placeholder, not a finished outcome."""
    if not isinstance(resp, dict):
        return False
    outcome = str(resp.get("outcome") or resp.get("status") or "").strip().lower()
    if outcome == "pending":
        return True
    actual = resp.get("actual_result")
    content = resp.get("content")
    if outcome in {"", "unknown"} and not actual and not content:
        # Empty shell written before subagent finishes.
        return True
    return False


def _looks_like_goal_id(value):
    text = str(value or "").strip()
    return bool(text) and text.startswith("l5g-")


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _resolve_goal_id(resp, source_path=None, workspace=None):
    """Resolve a stable L5 goal_id; never invent l5-unknown-* when better evidence exists.

    2026-09-14 event-envelope rule: goal_id must come from the record itself
    (goal_id/goalId/id/task_id) or from the IMMEDIATE sandbox dir the record
    physically lives in. The old `src.parent.parent` fallback resolved ANY
    response file against whatever ego_sandbox/<id> happened to sit two levels
    up — measured 32/90 wrong bindings to a 4-day-old goal this epoch. Only
    paths actually inside ego_sandbox/<goal_id>/ may use the directory name;
    everything else falls through to explicit fields or an explicit UNKNOWN
    marker (never an old goal's id).
    """
    if not isinstance(resp, dict):
        resp = {}

    for key in ("goal_id", "goalId", "id"):
        if _looks_like_goal_id(resp.get(key)):
            return str(resp.get(key)).strip()

    task_id = str(resp.get("task_id") or "").strip()
    if _looks_like_goal_id(task_id):
        return task_id

    # Only when source_path is INSIDE ego_sandbox/<goal_id>/ may the directory
    # name count as identity. Sandbox response files live at
    # ego_sandbox/<goal_id>/l5_response.json — parent.name, not parent.parent.
    src = Path(source_path) if source_path else None
    if src is not None:
        try:
            parts = [p for p in src.parts if p]
            if "ego_sandbox" in parts:
                i = parts.index("ego_sandbox")
                if i + 1 < len(parts) and _looks_like_goal_id(parts[i + 1]):
                    return parts[i + 1]
        except Exception:
            pass

    # Latest open goal as weak fallback when response omitted goal_id.
    if workspace is not None:
        goals_path = Path(workspace) / ".lae" / "l5_goals.jsonl"
        try:
            if goals_path.is_file():
                lines = [ln.strip() for ln in goals_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                for ln in reversed(lines[-20:]):
                    try:
                        obj = json.loads(ln)
                    except Exception:
                        continue
                    gid = obj.get("goal_id")
                    if _looks_like_goal_id(gid):
                        return str(gid).strip()
        except Exception:
            pass

    action_type = str(resp.get("action_type") or "unknown").strip() or "unknown"
    tick = resp.get("tick", 0)
    if task_id:
        return task_id
    return f"l5-{action_type}-tick{tick}"


def _mut_evidence_verifiable(evidence_text: str, workspace) -> bool:
    """MUT (self-modification) goals must cite a verifiable artifact: an existing
    file path, a checksum/diff/backup marker, or a test-run reference. Pure
    self-narrative does not count (audit 2026-08-28: l5g-20260828-000009-MUT
    recorded success on narrative alone, bypassing the 2026-08-12 empty-evidence gate)."""
    if not evidence_text or not str(evidence_text).strip():
        return False
    _low = str(evidence_text).lower()
    _markers = ("sha256", "checksum", "md5", "diff", "backup", "patch",
                "pytest", "smoke", "test pass", "tests pass", "self_test")
    if any(m in _low for m in _markers):
        return True
    _ws = Path(workspace)
    for tok in re.findall(r"[\w./\\-]+\.(?:py|json|jsonl|md|txt|ps1|bak)", str(evidence_text)):
        for base in (_ws, _ws / "projects" / "tl-ego", _ws / "ego_sandbox"):
            try:
                if (base / tok.replace("\\", "/")).is_file():
                    return True
            except Exception:
                continue
    return False


_BACKFILL_LAST_TS = 0.0


def _backfill_aged_sandboxes(workspace, max_backfill: int = 10) -> int:
    """Coverage fix (audit 2026-08-28): live promotion picks only the newest
    candidate per tick within a 300s window, so most sandbox results aged out
    and never entered l5_goal_results.jsonl (~6% coverage). Append aged-out
    outcomes here, applying the same evidence gates, marked stage1_backfill."""
    sandbox_root = Path(workspace) / "ego_sandbox"
    result_path = Path(workspace) / ".lae" / "l5_goal_results.jsonl"
    cursor_path = Path(workspace) / ".lae" / "l5_backfill.cursor"
    if not sandbox_root.is_dir():
        return 0
    done = set()
    if cursor_path.exists():
        try:
            done = set(cursor_path.read_text(encoding="utf-8").split())
        except Exception:
            done = set()
    # Seed from the existing log so already-recorded goals are not re-appended.
    try:
        if result_path.exists() and result_path.stat().st_size > 0:
            for _line in result_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    _gid = json.loads(_line).get("goal_id")
                    if _gid:
                        done.add(str(_gid))
                except Exception:
                    continue
    except Exception:
        pass
    appended = 0
    new_done = []
    for sd in sorted(sandbox_root.iterdir()):
        if appended >= max_backfill:
            break
        if sd.name in done:
            continue
        sf = sd / ".lae" / "l5_response.json"
        if not sf.is_file():
            sf = sd / "result.json"
        if not sf.is_file():
            done.add(sd.name)
            new_done.append(sd.name)
            continue
        try:
            age_s = time.time() - sf.stat().st_mtime
        except Exception:
            continue
        if age_s < 300:
            continue  # still inside the live promotion window
        payload = _load_json_file(sf)
        if not payload or _is_pending_outcome(payload):
            done.add(sd.name)
            new_done.append(sd.name)
            continue
        goal_id = str(payload.get("goal_id") or sd.name)
        if goal_id in done:
            done.add(sd.name)
            new_done.append(sd.name)
            continue
        outcome = str(payload.get("outcome") or payload.get("status") or "unknown")[:40]
        evidence_raw = payload.get("evidence")
        if evidence_raw is None:
            evidence_raw = ""
        elif not isinstance(evidence_raw, str):
            try:
                evidence_raw = json.dumps(evidence_raw, ensure_ascii=False)
            except Exception:
                evidence_raw = str(evidence_raw)
        evidence_raw = evidence_raw or payload.get("message") or ""
        # ── Evidence key fix (2026-09-23): subagent 从不写 evidence 键,但写了
        # intent(81) lesson(80) next_impulse_hint(79) files_created(75)
        # tests_run(72) verdict(14) —— 82 个 result.json 里 78 个被当成
        # "无证据" 降级成 success_no_evidence,经验回路 95% 断裂。
        # 按优先级拼一条可验证证据串,键名对上真实产出。
        if not evidence_raw.strip():
            _parts = []
            for _k in ("intent", "verdict", "lesson", "files_created",
                       "tests_run", "delivery_confirmed", "parameter_update",
                       "expected", "actual", "next_action"):
                _v = payload.get(_k)
                if _v in (None, "", [], {}):
                    continue
                _s = _v if isinstance(_v, str) else json.dumps(_v, ensure_ascii=False)
                _parts.append(f"{_k}={_s}")
            evidence_raw = " | ".join(_parts)[:500]
        entry = {
            "goal_id": goal_id,
            "tick": payload.get("tick") if isinstance(payload.get("tick"), int) else payload.get("state_tick_measured"),
            "goal_type": str(payload.get("goal_type") or payload.get("action_type") or "")[:80],
            "outcome": outcome,
            "raw_outcome": outcome,
            "evidence": str(evidence_raw)[:500],
            "evidence_valid": bool(str(evidence_raw).strip()),
            "prediction_error": payload.get("prediction_error"),
            "source": "stage1_backfill",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # Same gates as the live path: empty evidence and unverifiable MUT
        # evidence must not count as success.
        if not entry["evidence_valid"] and entry["outcome"] == "success":
            entry["outcome"] = "success_no_evidence"
        elif "-MUT" in goal_id and entry["outcome"] == "success" and not _mut_evidence_verifiable(str(evidence_raw), workspace):
            entry["outcome"] = "success_no_evidence"
        elif str(outcome).lower().startswith("success") and _not_landed_text(outcome, evidence_raw):
            # Landing gate (2026-09-11): draft-only outcomes are not success.
            entry["outcome"] = "success_not_landed"
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            appended += 1
        except Exception:
            continue
        done.add(goal_id)
        done.add(sd.name)
        new_done.append(sd.name)
    if new_done:
        try:
            with open(cursor_path, "a", encoding="utf-8") as f:
                f.write("\n".join(new_done) + "\n")
        except Exception:
            pass
    return appended


def consume_action_outcome(
    workspace,
    resting_state,
    get_embedding_fn,
    write_consumption_log_fn,
    stdp_rates  # (A_PLUS, A_MINUS) tuple
):
    """Consume .l5_response.json and inject as high-weight evidence.
    
    Args:
        workspace: Path to workspace root
        resting_state: dict, mutable RESTING_STATE from tl_ego_server
        get_embedding_fn: callable(text) -> list[float]
        write_consumption_log_fn: callable(dict) -> None
        stdp_rates: tuple (A_PLUS, A_MINUS) for learning rate scaling
    
    Returns:
        dict or None: consumption result with prediction_error
    """
    A_PLUS, A_MINUS = stdp_rates
    response_path = workspace / ".lae" / "l5_response.json"
    consumed_path = response_path.parent / (response_path.name + ".consumed")
    
    # ── 0. Already consumed? Skip ──
    if consumed_path.exists():
        return None

    # ── 0.5 Backfill aged-out sandbox results (coverage fix 2026-08-28) ──
    global _BACKFILL_LAST_TS
    try:
        if time.time() - _BACKFILL_LAST_TS > 600:
            _BACKFILL_LAST_TS = time.time()
            _n_backfilled = _backfill_aged_sandboxes(workspace, max_backfill=10)
            if _n_backfilled:
                print(f"[Stage1] Backfilled {_n_backfilled} aged-out sandbox results into l5_goal_results.jsonl", flush=True)
    except Exception as _bf_err:
        print(f"[Stage1] backfill error (non-fatal): {_bf_err}", flush=True)


    
    # ── 1. Fallback: promote from sandbox if workspace root missing ──
    _just_promoted = False
    _promote_src = None
    if not response_path.exists():
        sandbox_root = workspace / "ego_sandbox"
        if sandbox_root.is_dir():
            candidates = []
            for sd in sorted(sandbox_root.iterdir()):
                # Primary: .lae/l5_response.json (standard response file)
                sf = sd / ".lae" / "l5_response.json"
                # Fallback: result.json (actual L5 sandbox output format)
                if not sf.is_file():
                    sf = sd / "result.json"
                if sf.is_file():
                    try:
                        age_s = max(0.0, time.time() - sf.stat().st_mtime)
                        if age_s < 300:
                            payload = _load_json_file(sf)
                            if _is_pending_outcome(payload):
                                continue
                            candidates.append((age_s, sf, payload))
                    except Exception:
                        pass
            if candidates:
                # ascending age → [0] = newest mtime = most recently finished subagent
                candidates.sort(key=lambda x: x[0])
                _, src, _payload = candidates[0]
                try:
                    response_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, response_path)
                    _just_promoted = True
                    _promote_src = src
                    print(f"[Stage1] Promoted l5_response.json from {src.parent.parent.name} (age={candidates[0][0]:.0f}s)", flush=True)
                except Exception as _promote_e:
                    print(f"[Stage1] Promote error: {_promote_e}", flush=True)
        if not response_path.exists():
            return None
    
    # ── 2. Race guard: only for pre-existing (non-promoted) files ──
    # Promoted files come from completed subagents → safe to consume immediately.
    # Pre-existing files might be a pending placeholder still being written.
    if not _just_promoted:
        try:
            file_age = max(0.0, time.time() - response_path.stat().st_mtime)
            if file_age < 2.0:
                print(f"[Stage1] Skipping l5_response.json (age={file_age:.1f}s < 2s race guard)", flush=True)
                return None
        except Exception:
            pass
    
    
    # ── Defensive JSON read: retry once if corrupt, then skip ──
    try:
        raw_text = response_path.read_text("utf-8", errors="replace")
        resp = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as _json_err:
        print(f"[Stage1] Corrupt l5_response.json ({_json_err}) — deleting and skipping", flush=True)
        try:
            response_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    except Exception as _read_err:
        print(f"[Stage1] Error reading l5_response.json: {_read_err}", flush=True)
        return None

    if _is_pending_outcome(resp):
        print(
            f"[Stage1] Skipping pending l5_response.json "
            f"(goal_id={resp.get('goal_id')!r} tick={resp.get('tick')!r})",
            flush=True,
        )
        return None

    # Archive and delete to prevent re-processing
    consumed_path = workspace / ".lae" / "l5_response.consumed.json"
    response_path.replace(consumed_path)

    tick = resp.get("tick", 0)
    action_type = resp.get("action_type", "unknown")
    outcome = resp.get("outcome", "unknown")
    expected = resp.get("expected_result", "")
    actual = resp.get("actual_result", "")
    raw_pred_error = resp.get("prediction_error")
    content = resp.get("content", "")
    task_id = resp.get("task_id", "")
    goal_id = _resolve_goal_id(resp, source_path=_promote_src or consumed_path, workspace=workspace)

    # If response lacks useful actual/evidence, enrich from sandbox result.json.
    # 2026-09-14: enrichment source must live in the SAME sandbox the resolved
    # goal_id points to — otherwise a result.json from an unrelated goal can
    # overwrite identity (the exact old-ID-binding bug measured this epoch).
    if _promote_src is not None:
        _src_parts = [p for p in Path(_promote_src).parts if p]
        _sandbox_dir = None
        if "ego_sandbox" in _src_parts:
            _i = _src_parts.index("ego_sandbox")
            if _i + 1 < len(_src_parts):
                _sandbox_dir = Path(*_src_parts[: _i + 2])
        if _sandbox_dir is not None:
            result_obj = _load_json_file(_sandbox_dir / "result.json")
            if result_obj:
                # envelope rule: result.json may CONFIRM the resolved id, never
                # override it with a different goal's id.
                if _looks_like_goal_id(result_obj.get("goal_id")) and not _looks_like_goal_id(goal_id):
                    goal_id = str(result_obj.get("goal_id")).strip()
            if not actual:
                actual = (
                    result_obj.get("lesson")
                    or result_obj.get("commander_message")
                    or result_obj.get("outcome")
                    or actual
                )
            # 2026-09-23: evidence 兜底同样按真实产出键拼(live path 原来只 enrich
            # actual,evidence 仍是空 → 照样被降级成 success_no_evidence)
            if not str(resp.get("evidence") or "").strip():
                _ev_parts = []
                for _k in ("intent", "verdict", "lesson", "files_created",
                           "tests_run", "delivery_confirmed", "expected",
                           "actual", "next_action"):
                    _v = result_obj.get(_k)
                    if _v in (None, "", [], {}):
                        continue
                    _s = _v if isinstance(_v, str) else json.dumps(_v, ensure_ascii=False)
                    _ev_parts.append(f"{_k}={_s}")
                if _ev_parts:
                    resp["evidence"] = " | ".join(_ev_parts)[:500]
            if (not outcome or outcome == "unknown") and result_obj.get("outcome"):
                outcome = result_obj.get("outcome")
            if not action_type or action_type == "unknown":
                action_type = result_obj.get("intent") or result_obj.get("project_name") or action_type
    success_criteria = resp.get("success_criteria", "")
    
    # ── Stage 1 Quality Fix (2026-06-23): Always compute PE from semantic distance ──
    # Subagent-provided prediction_error is subjective self-evaluation and
    # systematically biased (48% were 0.5 placeholder, 22% were 0.0).
    # We ALWAYS compute from semantic distance when expected/actual are present,
    # and ONLY fall back to provided value when embedding is unavailable.
    pred_error = None
    pred_error_source = "unknown"
    
    if expected and actual:
        try:
            exp_emb = get_embedding_fn(expected[:500])
            act_emb = get_embedding_fn(actual[:500])
            if exp_emb and act_emb and len(exp_emb) > 0 and len(act_emb) > 0:
                exp_v = np.array(exp_emb)
                act_v = np.array(act_emb)
                exp_v = exp_v / (np.linalg.norm(exp_v) + 1e-8)
                act_v = act_v / (np.linalg.norm(act_v) + 1e-8)
                similarity = float(np.dot(exp_v, act_v))
                pred_error = 1.0 - similarity  # 0=perfect match, 1=opposite
                pred_error_source = "computed_semantic_distance"
                if raw_pred_error is not None:
                    print(f"[Stage1] Overriding subagent-provided PE={raw_pred_error:.3f} "
                          f"with computed PE={pred_error:.3f} (source=computed_semantic_distance)", flush=True)
        except Exception as e:
            print(f"[Stage1] semantic error calc failed: {e}", flush=True)
    
    # ── Structured PE fallback: compare before/after state snapshots ──
    if pred_error is None:
        before = resp.get("state_before", {})
        after = resp.get("state_after", {})
        if before and after:
            try:
                # Compute PE from stagnation delta and dominant change
                stag_before = before.get("stagnation_streak", 0)
                stag_after = after.get("stagnation_streak", 0)
                dom_before = before.get("dominant_type") or before.get("dominant", "")
                dom_after = after.get("dominant_type") or after.get("dominant", "")
                
                stag_delta = stag_after - stag_before
                stag_pe = min(1.0, abs(stag_delta) / 5.0)  # normalize to [0,1]
                dom_pe = 0.0 if dom_before == dom_after else 0.6  # dominant change is significant
                
                pred_error = (stag_pe * 0.5 + dom_pe * 0.5)
                pred_error_source = "computed_structured_delta"
                print(f"[Stage1] Structured PE: stag_before={stag_before}→after={stag_after} "
                      f"(delta={stag_delta}), dom={dom_before}→{dom_after}, PE={pred_error:.3f}", flush=True)
            except Exception as e:
                print(f"[Stage1] structured PE calc failed: {e}", flush=True)
        
        # ── Last resort: outcome-based heuristic ──
        if pred_error is None:
            outcome_to_pe = {
                "success": 0.15,   # expected success → low error
                "partial": 0.45,   # partial result → medium error
                "failed": 0.75,    # unexpected failure → high error
                "timeout": 0.85,   # timeout → very high error
                "blocked": 0.70,   # blocked → high error
                "comm_artifact_built": 0.20,  # communication artifact delivered as expected
            }
            pred_error = outcome_to_pe.get(outcome, 0.5)
            pred_error_source = f"computed_outcome_heuristic({outcome})"
            if raw_pred_error is not None:
                print(f"[Stage1] Overriding subagent-provided PE={raw_pred_error:.3f} "
                      f"with outcome-heuristic PE={pred_error:.3f}", flush=True)
        
    # ── Inject as external_evidence with strength based on surprise ──
    # High prediction_error → high n_replace (force network to learn from surprise)
    # Low prediction_error → low n_replace (reinforce existing model)
    if pred_error > 0.7:
        n_replace = 8  # high surprise
        ttl = 10       # persist longer
    elif pred_error > 0.4:
        n_replace = 4  # medium surprise
        ttl = 6
    else:
        n_replace = 2  # low surprise
        ttl = 4
        
    # Map outcome to dominant type
    outcome_to_type = {
        "success": "action",     # successful action
        "partial": "integrator", # needs integration
        "failed": "memory",      # store failure for avoidance
        "timeout": "decider",    # decision needed
        "blocked": "sensory",    # need more perception
        "comm_artifact_built": "action"
    }
    domain = outcome_to_type.get(outcome, "memory")
        
    evidence_key = f"l5_outcome_{task_id}_{tick}"
    evidence_entry = {
        "domain": domain,
        "title": f"{action_type} {outcome}",
        "action": str(actual)[:200] if actual else str(expected)[:200],
        "request": f"prediction_error={pred_error:.3f}",
        "n_replace": n_replace,
        "state_snapshot": {
            "dominant": domain,
            "prediction_error": pred_error,
            "prediction_error_source": pred_error_source,
            "tick": tick
        }
    }
        
    if "external_evidence" not in resting_state:
        resting_state["external_evidence"] = {}
    if "evidence_ttl" not in resting_state:
        resting_state["evidence_ttl"] = {}
        
    resting_state["external_evidence"][evidence_key] = evidence_entry
    resting_state["evidence_ttl"][evidence_key] = ttl
        
    # ── STDP learning rate boost for high-error outcomes ──
    # (caller must apply this to global A_PLUS/A_MINUS)
    stdp_boost = pred_error > 0.5
    boost_factor = 3.0 if stdp_boost else 1.0
        
    # Write audit trail
    write_consumption_log_fn({
        "event": "action_outcome_consumed",
        "runtime_generation_id": generation_id(load_runtime_identity(workspace)),
        "tick": resting_state.get("tick", 0),
        "l5_tick": tick,
        "goal_id": goal_id,
        "action_type": action_type,
        "outcome": outcome,
        "prediction_error": pred_error,
        "prediction_error_source": pred_error_source,
        "n_replace": n_replace,
        "ttl": ttl,
        "domain": domain,
        "stdp_boost": stdp_boost,
        "boost_factor": boost_factor
    })

    # ── L5 Goal Engine result queue ──
    # The plugin asks subagents to append this directly, but Stage 1 is the
    # authoritative consumer of action outcomes. Mirror the consumed result
    # here so the goal lifecycle remains auditable even when an older
    # plugin/subagent task omits the JSONL append.
    try:
        result_path = workspace / ".lae" / "l5_goal_results.jsonl"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        # Safe truncation: ensure evidence is always a string before slicing
        _evidence_raw = (actual or content or expected) or ""
        if not isinstance(_evidence_raw, str):
            try:
                _evidence_raw = json.dumps(_evidence_raw, ensure_ascii=False)
            except Exception:
                _evidence_raw = str(_evidence_raw)
        result_entry = {
            "runtime_generation_id": generation_id(load_runtime_identity(workspace)),
            "goal_id": goal_id,
            "tick": tick,
            "goal_type": action_type[:80],
            "outcome": str(outcome)[:40],
            "raw_outcome": str(outcome)[:40],
            "evidence": _evidence_raw[:500],
            "evidence_valid": bool(str(_evidence_raw).strip()),
            "success_criteria": str(success_criteria or "")[:200],
            "prediction_error": pred_error,
            "prediction_error_source": pred_error_source,
            "next_action": "none" if outcome == "success" else "inspect failure and propose narrower follow-up goal",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "stage1_consequence_sensor",
        }
        # ── Evidence gate (2026-08-12): success without evidence is NOT success.
        # Stage3/autonomy audits and lesson mining treat empty evidence as noise,
        # which produced 87 sandbox analyses with zero closed-loop fixes.
        if not result_entry["evidence_valid"] and str(outcome) == "success":
            result_entry["outcome"] = "success_no_evidence"
            result_entry["next_action"] = "evidence_missing: re-run with a concrete state delta or artifact checksum"
        # ── MUT evidence quality gate (2026-08-28): self-modification goals must
        # cite a verifiable artifact (existing file path, checksum/diff/backup
        # marker, or test run). Self-narrative does not count.
        elif "-MUT" in str(goal_id) and result_entry["outcome"] == "success" and not _mut_evidence_verifiable(str(_evidence_raw), workspace):
            result_entry["outcome"] = "success_no_evidence"
            result_entry["next_action"] = "mut_evidence_unverifiable: cite a real file diff, backup, checksum, or test output"
        # ── Landing gate (2026-09-11): a success that only produced a draft or an
        # unapplied/undelivered artifact is not a landed outcome. Downgrade so it
        # cannot teach the loop that writing artifacts == changing the world.
        elif str(outcome).lower().startswith("success") and _not_landed_text(outcome, _evidence_raw):
            result_entry["outcome"] = "success_not_landed"
            result_entry["next_action"] = "landing_required: apply the patch / deliver the message, then re-verify"
        if result_path.exists() and result_path.stat().st_size > 0:
            # ── Dedup gate (2026-08-12): same goal_id consumed twice would
            # double-count the outcome (observed: l5g-20260812-000009 has both
            # a raw subagent append AND a Stage1 mirror). Skip if already logged.
            try:
                _existing_ids = set()
                for _line in result_path.read_text(encoding="utf-8").splitlines():
                    try:
                        _existing_ids.add(json.loads(_line).get("goal_id"))
                    except Exception:
                        continue
                if goal_id in _existing_ids:
                    print(f"[Stage1] goal_id {goal_id} already in results log - skip duplicate append", flush=True)
                    return {
                        "consumed": True,
                        "runtime_generation_id": generation_id(load_runtime_identity(workspace)),
                        "tick": tick,
                        "outcome": outcome,
                        "prediction_error": pred_error,
                        "prediction_error_source": pred_error_source,
                        "evidence_injected": evidence_key,
                        "stdp_boost_factor": boost_factor,
                        "dedup": True,
                    }
            except Exception as _dedup_e:
                print(f"[Stage1] dedup check failed (proceeding): {_dedup_e}", flush=True)
            with open(result_path, "rb+") as f:
                f.seek(-1, 2)
                if f.read(1) not in (b"\n", b"\r"):
                    f.seek(0, 2)
                    f.write(b"\n")
        with open(result_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Stage1] goal result append failed: {e}", flush=True)
        
    print(f"[Stage1] Consumed L5 outcome: tick={tick} action={action_type} "
          f"outcome={outcome} pred_error={pred_error:.3f} n_replace={n_replace} "
          f"pred_error_source={pred_error_source} "
          f"stdp_boost={'YES' if stdp_boost else 'NO'}", flush=True)
        
    return {
        "consumed": True,
        "runtime_generation_id": generation_id(load_runtime_identity(workspace)),
        "tick": tick,
        "outcome": outcome,
        "prediction_error": pred_error,
        "prediction_error_source": pred_error_source,
        "evidence_injected": evidence_key,
        "stdp_boost_factor": boost_factor,
    }

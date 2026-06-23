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
import time
import traceback
from pathlib import Path
import numpy as np


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
    if not response_path.exists():
        return None
    
    # ── Race guard: skip files written within the last 2 seconds ──
    # Prevents collision between auto resting loop and manual smoke tests.
    try:
        age_s = time.time() - response_path.stat().st_mtime
        if age_s < 2.0:
            print(f"[Stage1] Skipping l5_response.json (age={age_s:.1f}s < 2s race guard)", flush=True)
            return None
    except Exception:
        pass
    
    try:
        with open(response_path, "r", encoding="utf-8") as f:
            resp = json.load(f)
        
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
        goal_id = resp.get("goal_id") or task_id or f"l5-{action_type}-tick{tick}"
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
                    dom_before = before.get("dominant_type", "")
                    dom_after = after.get("dominant_type", "")
                    
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
            "blocked": "sensory"     # need more perception
        }
        domain = outcome_to_type.get(outcome, "memory")
        
        evidence_key = f"l5_outcome_{task_id}_{tick}"
        evidence_entry = {
            "domain": domain,
            "title": f"{action_type} {outcome}",
            "action": actual[:200] if actual else expected[:200],
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
            result_entry = {
                "goal_id": goal_id,
                "tick": tick,
                "goal_type": action_type,
                "outcome": outcome,
                "evidence": (actual or content or expected)[:500],
                "success_criteria": success_criteria,
                "prediction_error": pred_error,
                "prediction_error_source": pred_error_source,
                "next_action": "none" if outcome == "success" else "inspect failure and propose narrower follow-up goal",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "stage1_consequence_sensor",
            }
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
            "tick": tick,
            "prediction_error": pred_error,
            "prediction_error_source": pred_error_source,
            "evidence_injected": evidence_key,
            "stdp_boost_factor": boost_factor
        }
        
    except Exception as e:
        print(f"[Stage1] Error consuming outcome: {e}\n{traceback.format_exc()}", flush=True)
        return None

"""
L7 Orchestrator — Main Entry Point and Coordination Layer

Bootstraps L7 when TL-Ego starts. Coordinates:
- L7 Action Interface (external tools)
- Mission Planner (gap-driven exploration)
- L7→L5 Bridge (observation injection)

The orchestrator runs in a lightweight tick loop alongside L5:
- Every N L5 ticks, check L5 state via RESTING_STATE
- If gaps found, generate and execute a mission
- Inject results back into L5's observation pipeline
- Log all activity for diagnostics

Integration with TL-Ego:
- L7Orchestrator.l7_tick() is called after each L5 resting tick
- L7 API endpoints (/l7-status, /l7-explore) are added to do_GET/do_POST
- L7 observations are injected into RESTING_STATE["external_evidence"]
"""

import os, time, json, random, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

# ── Ensure l7/module dir is in sys.path for sibling imports ──
_l7_this_dir = os.path.dirname(os.path.abspath(__file__))
if _l7_this_dir not in sys.path:
    sys.path.insert(0, _l7_this_dir)

from l7_action_interface import execute as l7_execute, ALL_TOOLS
from l7_mission_planner import MissionPlanner, Mission
from l7_bridge import (
    action_result_to_observation,
    inject_observation,
    collect_l7_observations,
    get_l7_bridge_status,
    process_l7_results,
)

# ── Phase 2: Self-Model Architect ──
from l7_architecture_scanner import ArchitectureScanner
from l7_runtime_model import RuntimeModel
from l7_gap_analyzer import GapAnalyzer, format_gaps_for_bridge
from l7_self_mod_proposer import SelfModProposer

# ── Phase 3: Evolution Orchestrator ──
from l7_patch_executor import PatchExecutor, Patch
from l7_evolution_logger import EvolutionLogger
from l7_security_gate import SecurityGate

TZ_CST = timezone(timedelta(hours=8))


class L7Orchestrator:
    """Main L7 coordination layer. Single instance owned by TL-Ego."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.state_path = workspace / ".lae" / "l7"
        self.state_path.mkdir(parents=True, exist_ok=True)
        
        # Subsystems
        self.planner = MissionPlanner(workspace)
        
        # Phase 2: Self-Model Architect subsystems
        self.arch_scanner = ArchitectureScanner(workspace / 'projects' / 'tl-ego')
        self.runtime_model = RuntimeModel(workspace)
        self.gap_analyzer = GapAnalyzer(self.arch_scanner, self.runtime_model)
        self.proposer = SelfModProposer(workspace / 'projects' / 'tl-ego')
        
        # Phase 3: Evolution Orchestrator subsystems
        self.tl_ego_dir = workspace / 'projects' / 'tl-ego'
        self.patch_executor = PatchExecutor(self.tl_ego_dir)
        self.evolution_logger = EvolutionLogger(workspace)
        self.security_gate = SecurityGate(self.tl_ego_dir)
        
        # Tick tracking
        self._tick_counter = 0
        self.last_l5_tick = -1
        self.mission_interval = 10  # Check for missions every N L5 ticks
        self.urgent_mission_cooldown = 3  # Avoid repeated exploration during one unstable episode
        self.last_mission_tick = 0
        self._self_model_interval = 20  # Self-model analysis every N L5 ticks
        self._last_self_model_tick = 0
        self._evolution_interval = 5  # Evolution check every N L5 ticks
        self._last_evolution_tick = 0
        
        # State
        self.is_running = False
        self.last_gaps = {}
        self.last_status = {}
        self.last_self_model_result = {}
        self._pending_external_closures = []
        
        # Log
        self._log_file = self.state_path / "l7_activity.log"
        self._closure_file = self.state_path / "external_exploration_closure.jsonl"
        self._init_log()
        
        # External action interface reference (set after init)
        self._action_execute = l7_execute
        self.is_running = True

    def _init_log(self):
        """Initialize log file."""
        if not self._log_file.exists():
            self._log_file.write_text(
                json.dumps({"l7_start": datetime.now(TZ_CST).isoformat(), "version": "0.1.0", "phase": 1}) + "\n",
                "utf-8"
            )

    def _log(self, event: str, data: dict = None):
        """Log an event to the L7 activity log."""
        entry = {
            "ts": datetime.now(TZ_CST).isoformat(),
            "l7_tick": self._tick_counter,
            "event": event,
            "data": data or {},
        }
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _audit_external_closure(self, entry: dict):
        record = {"ts": datetime.now(TZ_CST).isoformat(), **entry}
        try:
            with open(self._closure_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _evaluate_pending_external_closures(self, l5_state: dict):
        pending = self._pending_external_closures
        self._pending_external_closures = []
        for item in pending:
            before = item["before"]
            target = item["target_type"]
            before_active = set(before.get("active_types", []) or [])
            after_active = set(l5_state.get("active_types", []) or [])
            before_counts = before.get("type_counts", {}) or {}
            after_counts = l5_state.get("type_counts", {}) or {}
            before_total = float(sum(before_counts.values()) or 0.0)
            after_total = float(sum(after_counts.values()) or 0.0)
            before_share = (float(before_counts.get(target, 0)) / before_total) if before_total else 0.0
            after_share = (float(after_counts.get(target, 0)) / after_total) if after_total else 0.0
            share_delta = round(after_share - before_share, 4)
            # P3: also score pre-KWTA seed share. Rare mission targets can be
            # seeded strongly then wiped by attractor competition; seed share is
            # still evidence that L7 bias reached L5.
            before_seed_share = float((before.get("seed_type_share") or {}).get(target, 0.0) or 0.0)
            after_seed_share = float((l5_state.get("seed_type_share") or {}).get(target, 0.0) or 0.0)
            if after_seed_share <= 0.0:
                after_seed_counts = l5_state.get("seed_type_counts") or {}
                _st = float(sum(after_seed_counts.values()) or 0.0)
                if _st > 0:
                    after_seed_share = float(after_seed_counts.get(target, 0) or 0) / _st
            seed_share_delta = round(after_seed_share - before_seed_share, 4)
            dominant_after = l5_state.get("dominant_type")
            dominant_changed = before.get("dominant_type") != dominant_after
            soft_presence = target in set(l5_state.get("l7_soft_presence") or [])
            # Activation is multi-signal: new presence, share gain, seed bias hit,
            # soft presence after L7 protect, or becoming dominant.
            activated = (
                (target in after_active and target not in before_active)
                or (share_delta >= 0.03)
                or (seed_share_delta >= 0.15)
                or (after_seed_share >= 0.20)
                or soft_presence
                or (dominant_changed and dominant_after == target)
            )
            changes = {
                "dominant_changed": dominant_changed,
                "dominant_after": dominant_after,
                "concentration_delta": round(l5_state.get("concentration", 0.0) - before.get("concentration", 0.0), 4),
                "stagnation_delta": l5_state.get("stagnation_streak", 0) - before.get("stagnation_streak", 0),
                "prediction_error_delta": round(l5_state.get("prediction_error", 0.0) - before.get("prediction_error", 0.0), 4),
                "target_share_delta": share_delta,
                "target_seed_share_before": round(before_seed_share, 4),
                "target_seed_share_after": round(after_seed_share, 4),
                "target_seed_share_delta": seed_share_delta,
                "target_soft_presence": soft_presence,
                "target_activated": activated,
            }
            evaluation = {
                "phase": "evaluated",
                "mission_id": item["mission_id"],
                "observation_id": item["observation_id"],
                "target_type": target,
                "source_urls": item["source_urls"],
                "evidence_quality": item["evidence_quality"],
                "ticks_elapsed": l5_state.get("tick", 0) - before.get("tick", 0),
                "changes": changes,
                "state_changed": activated or changes["dominant_changed"] or any(
                    changes[key] != 0 for key in (
                        "concentration_delta", "stagnation_delta",
                        "prediction_error_delta", "target_share_delta",
                    )
                ),
            }
            feedback = self.planner.record_external_evaluation(evaluation)
            evaluation["feedback_effective"] = feedback["effective"]
            evaluation["effective"] = feedback["effective"]
            evaluation["target_hit"] = feedback.get("target_hit", False)
            evaluation["state_improved"] = feedback.get("state_improved", False)
            evaluation["cooldown_success"] = feedback.get("cooldown_success", feedback["effective"])
            self._audit_external_closure(evaluation)

    def extract_l5_state(self, resting_state: dict) -> dict:
        """Extract a clean L5 state snapshot from RESTING_STATE dict.
        
        Returns a flat dict suitable for MissionPlanner.analyze_l5_state().
        """
        trajectory = resting_state.get("trajectory", [])
        last_point = trajectory[-1] if trajectory else {}
        previous_point = trajectory[-2] if len(trajectory) >= 2 else {}
        
        # Get drives
        drives = {}
        try:
            from stage2_homeostatic_drives import load_drives
            drives = load_drives(self.workspace)
        except Exception:
            drives = {}
        
        # Infer active/inactive types from last trajectory point
        type_counts = last_point.get("type_counts", {})
        active_types = [t for t, c in type_counts.items() if c > 0] if type_counts else []
        all_types = type_counts.keys() if type_counts else []
        inactive_types = [t for t in [
            "sensory", "memory", "integrator", "action", "decider",
            "curiosity", "doubt", "social", "novelty", "stability",
            "veto", "rule_modification", "consolidation", "planning",
            "conflict", "forgetting", "meta", "embedding",
        ] if t not in active_types] if active_types else []

        # Prefer latest pre-KWTA seed snapshot when present (same tick), else trajectory.
        seed_snap = resting_state.get("_pre_kwta_seed_state") or {}
        seed_type_counts = seed_snap.get("seed_type_counts") or last_point.get("seed_type_counts") or {}
        seed_type_share = seed_snap.get("seed_type_share") or last_point.get("seed_type_share") or {}
        if not seed_type_share and seed_type_counts:
            _st = float(sum(seed_type_counts.values()) or 0.0)
            if _st > 0:
                seed_type_share = {t: round(c / _st, 4) for t, c in seed_type_counts.items()}

        # Check comm
        comm = resting_state.get("comm", {}) or resting_state.get("_comm", {})
        
        state = {
            "dominant_type": resting_state.get("last_dominant", last_point.get("dominant", "unknown")),
            "stagnation_streak": resting_state.get("stagnation_streak", 0),
            "concentration": last_point.get("concentration", 0.5),
            "concentration_drop": max(
                0.0,
                previous_point.get("concentration", last_point.get("concentration", 0.5))
                - last_point.get("concentration", 0.5),
            ),
            "competition": last_point.get("competition", 0.5),
            "tick": resting_state.get("tick", 0),
            "metacontemplate_runs": resting_state.get("metacontemplate_runs", 0),
            "comm_motivation": comm.get("motivation", 0.0),
            "prediction_error": resting_state.get("last_prediction_error", 0.5),
            "active_types": active_types,
            "inactive_types": inactive_types,
            "type_counts": type_counts,
            "seed_type_counts": seed_type_counts,
            "seed_type_share": seed_type_share,
            "l7_soft_presence": last_point.get("l7_soft_presence") or seed_snap.get("l7_targets") or [],
            "drives": drives,
        }
        return state

    def l7_tick(self, resting_state: dict) -> dict:
        """Run one L7 tick after an L5 resting tick.
        
        This is the main integration hook called from TL-Ego.
        
        Steps:
        1. Extract L5 state from RESTING_STATE
        2. Check if a mission is needed (based on mission_interval + gaps)
        3. If mission needed, generate → execute → inject results
        4. Clean up stale observations
        5. Log activity
        
        Returns: status dict
        """
        self._tick_counter += 1
        l5_tick = resting_state.get("tick", 0)
        
        result = {
            "l7_tick": self._tick_counter,
            "l5_tick": l5_tick,
            "action": "noop",
            "mission": None,
            "injection": None,
        }
        
        # Step 1: Extract L5 state
        l5_state = self.extract_l5_state(resting_state)
        self._evaluate_pending_external_closures(l5_state)
        
        # Step 2: Check if mission is needed. A sharp concentration collapse
        # under high competition warrants an earlier check, while high
        # competition alone remains a normal productive-disagreement state.
        preliminary_gaps = self.planner.analyze_l5_state(l5_state)
        preliminary_gaps["competition"] = l5_state.get("competition", 0.0)
        preliminary_gaps["concentration_drop"] = l5_state.get("concentration_drop", 0.0)
        self.last_gaps = preliminary_gaps
        urgent_dynamic_risk = preliminary_gaps["dynamic_instability"]
        needs_check = (
            l5_tick - self.last_mission_tick >= self.mission_interval
            or self.last_mission_tick == 0  # Always check on first call
            or (
                urgent_dynamic_risk
                and l5_tick - self.last_mission_tick >= self.urgent_mission_cooldown
            )
        )
        
        if needs_check:
            self.last_mission_tick = l5_tick
            gaps = preliminary_gaps
            
            # Step 3: Generate and execute mission if gaps exist
            mission = self.planner.generate_mission(l5_state)
            
            if mission:
                result["action"] = "explore"
                result["mission"] = {
                    "id": mission.id,
                    "target_type": mission.target_type,
                    "goal": mission.goal[:100],
                    "action": mission.action,
                }
                
                self._log("explore_start", {
                    "mission_id": mission.id,
                    "target_type": mission.target_type,
                    "goal": mission.goal[:200],
                    "gaps": {k: v for k, v in gaps.items() if k != "priority_missions" or len(gaps.get("priority_missions", []))},
                })
                
                # Execute via Action Interface
                mission_result = self.planner.execute_mission(mission, self._get_action_interface())
                
                if mission_result:
                    # Step 4: Inject into L5 observation pipeline
                    target_type = mission.target_type
                    obs = action_result_to_observation(mission_result, target_type=target_type)
                    injection_status = inject_observation(obs, resting_state)
                    
                    result["injection"] = {
                        "injected": injection_status.get("injected", False),
                        "observation_id": injection_status.get("observation_id"),
                        "trigger_types": injection_status.get("trigger_types", []),
                        "queue_size": injection_status.get("queue_size", 0),
                    }

                    evidence_record = {
                        "phase": "injected" if injection_status.get("injected") else "rejected",
                        "mission_id": mission.id,
                        "observation_id": injection_status.get("observation_id"),
                        "target_type": target_type,
                        "result_status": mission_result.get("status"),
                        "result_tool": mission_result.get("tool"),
                        "result_count": obs.get("count", 0),
                        "source_urls": obs.get("files", []),
                        "source_domains": obs.get("source_domains", []),
                        "search_backend": obs.get("search_backend"),
                        "evidence_quality": obs.get("evidence_quality", 0.0),
                        "reason": injection_status.get("reason", obs.get("evidence_reason")),
                    }
                    self._audit_external_closure(evidence_record)
                    if injection_status.get("injected") and mission_result.get("tool") == "web_search":
                        self._pending_external_closures.append({
                            **evidence_record,
                            "before": l5_state,
                        })
                    elif not injection_status.get("injected"):
                        # Rejected searches never reach evaluate(); still feed the
                        # planner so sticky targets can cool down.
                        reject_fb = self.planner.record_rejected_mission(evidence_record)
                        evidence_record["feedback_effective"] = reject_fb.get("effective", False)
                        evidence_record["effective"] = reject_fb.get("effective", False)
                    
                    self._log("explore_complete", {
                        "mission_id": mission.id,
                        "result_status": mission_result.get("status"),
                        "result_tool": mission_result.get("tool"),
                        "result_count": obs.get("count", 0),
                        "evidence_quality": obs.get("evidence_quality", 0.0),
                        "injected": injection_status.get("injected", False),
                        "trigger_types": injection_status.get("trigger_types", []),
                        "queue_size": injection_status.get("queue_size", 0),
                    })

                    # A search result is only a lead. When it passes the
                    # attribution/relevance gate, fetch its first source so
                    # L5 receives page text rather than search snippets alone.
                    source_urls = obs.get("files", [])
                    if (
                        mission_result.get("tool") == "web_search"
                        and injection_status.get("injected")
                        and source_urls
                    ):
                        fetch_result = self._get_action_interface().execute(
                            "web_fetch", url=source_urls[0]
                        )
                        if fetch_result.get("status") == "ok":
                            fetch_obs = action_result_to_observation(
                                fetch_result, target_type=target_type
                            )
                            fetch_status = inject_observation(fetch_obs, resting_state)
                            self._audit_external_closure({
                                "phase": "fetched" if fetch_status.get("injected") else "fetch_rejected",
                                "mission_id": mission.id,
                                "observation_id": fetch_status.get("observation_id"),
                                "target_type": target_type,
                                "result_status": fetch_result.get("status"),
                                "result_tool": fetch_result.get("tool"),
                                "source_urls": fetch_obs.get("files", []),
                                "source_domains": [],
                                "search_backend": obs.get("search_backend"),
                                "evidence_quality": obs.get("evidence_quality", 0.0),
                                "reason": fetch_status.get("reason"),
                            })
                            self._log("fetch_complete", {
                                "mission_id": mission.id,
                                "url": source_urls[0],
                                "chars": fetch_result.get("data", {}).get("chars", 0),
                                "injected": fetch_status.get("injected", False),
                            })
                        else:
                            self._log("fetch_failed", {
                                "mission_id": mission.id,
                                "url": source_urls[0],
                                "error": fetch_result.get("error", "unknown fetch error"),
                            })
                else:
                    self._log("explore_failed", {
                        "mission_id": mission.id,
                        "error": "Execution returned no result",
                    })
            else:
                result["action"] = "idle_no_gap"
        
        # Step 5: Clean up stale observations
        active_obs = collect_l7_observations(resting_state)
        result["queue_size"] = len(active_obs)
        
        self.last_status = result
        return result

    def self_model_tick(self, resting_state: dict) -> dict:
        """Phase 2: Self-Model Architect tick.
        
        Runs alongside l7_tick() on a separate schedule.
        
        Steps:
        1. Capture runtime snapshots (every tick)
        2. Run full gap analysis (every self_model_interval ticks)
        3. Generate proposals from gaps
        4. Inject priority gaps into L5 observation pipeline
        
        Returns: self-model analysis result dict
        """
        l5_tick = resting_state.get("tick", 0)
        result = {
            "model_tick": self._tick_counter,
            "l5_tick": l5_tick,
            "captured": False,
            "analyzed": False,
            "proposals": 0,
            "gaps": [],
        }
        
        # Step 1: Always capture runtime snapshot
        try:
            snap = self.runtime_model.capture_tick(resting_state)
            result["captured"] = True
            result["tick_state"] = {
                "dominant": snap["dominant_type"],
                "conc": snap["concentration"],
                "div": snap["diversity"],
                "stag": snap["stagnation_streak"],
            }
        except Exception as e:
            self._log("self_model_capture_error", {"error": str(e)})
        
        # Step 2+3: Full analysis on interval
        needs_analysis = (
            l5_tick - self._last_self_model_tick >= self._self_model_interval
            or self._last_self_model_tick == 0
        )
        
        if needs_analysis and resting_state.get("trajectory"):
            self._last_self_model_tick = l5_tick
            result["analyzed"] = True
            
            try:
                # Full gap analysis
                gaps = self.gap_analyzer.analyze_all(resting_state)
                result["gaps"] = [
                    {"type": g["type"], "severity": g["severity"], "desc": g["description"][:120]}
                    for g in gaps[:10]
                ]
                result["total_gaps"] = len(gaps)
                
                # Generate proposals from structural + runtime gaps
                new_props = self.proposer.generate_proposals(gaps)
                result["proposals"] = len(new_props)
                
                # Inject actionable gaps back into L5 via bridge
                bridge_data = format_gaps_for_bridge(gaps)
                if bridge_data.get("has_gaps"):
                    obs = {
                        "source": "l7_self_model",
                        "status": "ok",
                        "tool": "self_model_analysis",
                        "intent_type": "self_model_feedback",
                        "domain": "meta",
                        "trigger_types": ["meta", "planning", "curiosity"],
                        "categories": bridge_data,
                        "summary": bridge_data.get("summary", "Self-model analysis complete"),
                    }
                    inject_observation(obs, resting_state)
                
                self._log("self_model_analysis", {
                    "total_gaps": len(gaps),
                    "new_proposals": len(new_props),
                    "top_gap": gaps[0]["type"] if gaps else "none",
                })
                
                self.last_self_model_result = result
                
            except Exception as e:
                import traceback
                self._log("self_model_analysis_error", {"error": str(e)})
                result["error"] = str(e)
        
        return result

    def get_self_model_status(self) -> dict:
        """Get Phase 2 Self-Model Architect status for /l7-model endpoint."""
        # Run architecture scan on demand for status
        arch_report = {}
        arch_issues = []
        try:
            arch_report = self.arch_scanner.scan_all()
            arch_issues = self.arch_scanner.find_code_structure_issues(arch_report)
        except Exception as e:
            arch_report = {"error": str(e)}
        
        return {
            "phase": 2,
            "architecture": {
                "modules_scanned": len(arch_report.get("scanned_modules", [])),
                "total_functions": arch_report.get("total_functions", 0),
                "stages_found": len(arch_report.get("stages_found", [])),
                "stages_missing": arch_report.get("stages_missing", []),
                "structural_issues": len(arch_issues),
            },
            "runtime": self.runtime_model.get_status(),
            "gaps": self.gap_analyzer.get_gap_report(),
            "proposals": self.proposer.get_status(),
            "last_self_model": {
                "l5_tick": self.last_self_model_result.get("l5_tick", 0),
                "total_gaps": self.last_self_model_result.get("total_gaps", 0),
                "proposals_generated": self.last_self_model_result.get("proposals", 0),
            } if self.last_self_model_result else {},
        }

    def evolution_tick(self, resting_state: dict) -> dict:
        """Phase 3: Evolution Orchestrator tick.

        Runs after self_model_tick(). If there are pending proposals,
        attempts to apply the highest-priority one via security gate.
        """
        result = {"captured": False, "applied": False, "gaps": 0, "proposals": 0}

        # Check if evolution is due
        l5_tick = resting_state.get("tick", 0)
        if l5_tick - self._last_evolution_tick < self._evolution_interval:
            return result
        if l5_tick < 10:
            return result  # Don't evolve too early

        self._last_evolution_tick = l5_tick
        result["captured"] = True

        # Get pending proposals
        draft_props = self.proposer.list_proposals(status="draft")
        if not draft_props:
            self._log("evolution_no_proposals", {
                "l5_tick": l5_tick,
                "last_tick": self._last_evolution_tick,
                "interval": self._evolution_interval,
            })
            return result

        result["proposals"] = len(draft_props)

        # Pick the highest-priority proposal (low risk first)
        risk_order = {"low": 0, "medium": 1, "high": 2}
        best_prop = min(draft_props, key=lambda p: risk_order.get(p.get("risk", "high"), 99))

        if not best_prop:
            return result

        # Convert proposal to patch
        proposal_obj = self.proposer.get_proposal(best_prop["id"])
        if not proposal_obj:
            return result

        patch = self.patch_executor.create_patch_from_proposal(proposal_obj)
        if not patch:
            self._log("evolution_no_patch", {"proposal_id": best_prop["id"]})
            self.proposer.update_status(
                best_prop["id"], "failed", "governance: proposal could not be converted to a bounded patch"
            )
            return result

        # 2026-09-11: a proposal whose content is comments only has nothing to
        # execute. Appending its block would just bloat the module (197 pending
        # drafts ≈ 300 KB of dead comments), so record the decision and let it
        # leave the draft queue without touching source.
        try:
            from l7_security_gate import patch_has_executable_code
            if not patch_has_executable_code(patch):
                self._log("evolution_doc_only_recorded", {
                    "patch_id": patch.id,
                    "intent": patch.intent,
                    "target": str(patch.target_file),
                })
                result["doc_only_recorded"] = True
                self.proposer.update_status(
                    best_prop["id"], "reviewed",
                    "doc-only proposal: nothing executable; recorded without file write"
                )
                return result
        except Exception as _dce:
            self._log("evolution_doc_only_check_error", {"error": str(_dce)})

        # Security gate validation
        gate_result = self.security_gate.validate_patch(patch)
        if not gate_result["passed"]:
            self._log("evolution_gate_blocked", {
                "patch_id": patch.id,
                "reason": gate_result.get("summary", "gate failed"),
            })
            self.proposer.update_status(
                best_prop["id"], "reviewed", f"security gate blocked: {gate_result.get('summary', 'gate failed')}"
            )
            return result

        # Value check: evaluate patch against ego's value vector
        # Low-risk patches must still align with core values before auto-apply
        try:
            from value_check import evaluate_patch, VALUE_THRESHOLD
            # 2026-09-11: judge only what the patch ADDS. hunk["new"] is the whole
            # appended file, so value-checking it scored the file instead of the
            # change — which is why every check returned the identical 0.794.
            try:
                from l7_security_gate import patch_added_code
                proposed_code = patch_added_code(patch)
            except Exception:
                proposed_code = "".join(
                    hunk.get("new", "") for hunk in getattr(patch, "hunks", [])
                )
            value_result = evaluate_patch(patch, proposed_code)
            if not value_result["passed"]:
                self._log("evolution_value_blocked", {
                    "patch_id": patch.id,
                    "value_score": value_result.get("value_score"),
                    "recommendation": value_result.get("recommendation"),
                    "violations": value_result.get("violations_summary", [])[:3],
                })
                result["value_blocked"] = True
                result["value_score"] = value_result.get("value_score")
                self.proposer.update_status(
                    best_prop["id"], "reviewed", f"value gate blocked: score={value_result.get('value_score')}"
                )
                return result
            # 2026-09-11: the gate's own recommendation was ignored before — a
            # 0.6-0.8 score ("human_review") passed straight into the apply
            # branch. When apply was then skipped, the same proposal was
            # re-evaluated every tick forever (651 checks, zero applies).
            # Queue it once so it leaves the draft loop.
            if value_result.get("recommendation") != "auto_approve":
                self._log("evolution_value_review_queued", {
                    "patch_id": patch.id,
                    "value_score": value_result.get("value_score"),
                    "recommendation": value_result.get("recommendation"),
                })
                result["value_review_queued"] = True
                result["value_score"] = value_result.get("value_score")
                self.proposer.update_status(
                    best_prop["id"], "reviewed",
                    f"value gate: {value_result.get('recommendation')} "
                    f"(score={value_result.get('value_score')}) — queued for human review"
                )
                return result
        except Exception as _vce:
            self._log("evolution_value_check_error", {"error": str(_vce)})

        # Apply patch (all non-high risk; high requires human review)
        if gate_result.get("risk") in ("low", "medium"):
            apply_result = self.patch_executor.apply_patch(patch, dry_run=False)
            if apply_result["status"] == "applied":
                self.evolution_logger.log_patch(
                    patch.id, patch.intent, str(patch.target_file),
                    "applied", effect_score=0.5
                )
                result["applied"] = True
                self.proposer.update_status(
                    best_prop["id"], "applied", "security and value gates passed; bounded patch applied"
                )
                self._log("evolution_applied", {
                    "patch_id": patch.id,
                    "intent": patch.intent,
                    "target": str(patch.target_file),
                    "risk": gate_result.get("risk"),
                })
            else:
                self.evolution_logger.log_patch(
                    patch.id, patch.intent, str(patch.target_file),
                    "failed", error=apply_result.get("error")
                )
                self._log("evolution_failed", {
                    "patch_id": patch.id,
                    "error": apply_result.get("error"),
                })
                self.proposer.update_status(
                    best_prop["id"], "failed", apply_result.get("error", "patch application failed")
                )
        else:
            # 2026-09-11: this branch used to be an empty no-op, so a high-risk
            # patch re-ran silently every tick and never left the draft queue.
            self._log("evolution_high_risk_deferred", {
                "patch_id": patch.id,
                "risk": gate_result.get("risk"),
                "lines_changed": gate_result.get("lines_changed"),
            })
            result["deferred"] = True
            self.proposer.update_status(
                best_prop["id"], "reviewed",
                f"risk={gate_result.get('risk')} (lines={gate_result.get('lines_changed')}) "
                "— deferred for human review"
            )

        return result

    def execute_proposal(self, proposal_id: str, dry_run: bool = False) -> dict:
        """Execute a specific proposal as a patch.

        Args:
            proposal_id: Phase 2 proposal ID
            dry_run: If True, run all checks but don't apply

        Returns: result dict
        """
        proposal_obj = self.proposer.get_proposal(proposal_id)
        if not proposal_obj:
            self._log("execute_missing_proposal", {"proposal_id": proposal_id})
            return {"status": "error", "error": f"Proposal {proposal_id} not found"}

        patch = self.patch_executor.create_patch_from_proposal(proposal_obj)
        if not patch:
            self._log("execute_no_patch", {"proposal_id": proposal_id})
            return {"status": "error", "error": "Could not create patch from proposal"}

        # Security gate
        gate_result = self.security_gate.validate_patch(patch)

        if dry_run:
            return {
                "status": "dry_run",
                "proposal_id": proposal_id,
                "patch": patch.to_dict(),
                "gate": gate_result,
            }

        if not gate_result["passed"]:
            self._log("execute_gate_blocked", {
                "proposal_id": proposal_id,
                "patch_id": patch.id,
                "reason": gate_result.get("summary", "gate validation failed"),
            })
            self.proposer.update_status(
                proposal_id, "reviewed", f"security gate blocked: {gate_result.get('summary', 'gate validation failed')}"
            )
            return {
                "status": "blocked",
                "reason": gate_result.get("summary", "gate validation failed"),
                "patch": patch.to_dict(),
                "gate": gate_result,
            }

        # Value check: evaluate patch against ego's value vector
        try:
            from value_check import evaluate_patch, VALUE_THRESHOLD
            proposed_code = "".join(
                hunk.get("new", "") for hunk in getattr(patch, "hunks", [])
            )
            value_result = evaluate_patch(patch, proposed_code)
            if not value_result["passed"]:
                self._log("execute_value_blocked", {
                    "proposal_id": proposal_id,
                    "patch_id": patch.id,
                    "value_score": value_result.get("value_score"),
                })
                self.proposer.update_status(
                    proposal_id, "reviewed", f"value gate blocked: score={value_result.get('value_score')}"
                )
                return {
                    "status": "value_blocked",
                    "reason": f"Value alignment check failed (score={value_result.get('value_score', 0):.3f} < {VALUE_THRESHOLD})",
                    "value_result": value_result,
                    "patch": patch.to_dict(),
                    "gate": gate_result,
                }
        except Exception as _vce:
            self._log("value_check_error", {"proposal_id": proposal_id, "error": str(_vce)})

        apply_result = self.patch_executor.apply_patch(patch)

        if apply_result["status"] == "applied":
            self.evolution_logger.log_patch(
                patch.id, patch.intent, str(patch.target_file),
                "applied", effect_score=0.5
            )
            self._log("execute_applied", {
                "proposal_id": proposal_id,
                "patch_id": patch.id,
                "target": str(patch.target_file),
                "intent": patch.intent,
            })
            self.proposer.update_status(
                proposal_id, "applied", "security and value gates passed; bounded patch applied"
            )
        else:
            self.evolution_logger.log_patch(
                patch.id, patch.intent, str(patch.target_file),
                "failed", error=apply_result.get("error")
            )
            self._log("execute_failed", {
                "proposal_id": proposal_id,
                "patch_id": patch.id,
                "error": apply_result.get("error"),
            })
            self.proposer.update_status(
                proposal_id, "failed", apply_result.get("error", "patch application failed")
            )

        return {
            "status": apply_result["status"],
            "patch": patch.to_dict(),
            "apply_result": apply_result,
            "gate": gate_result,
        }

    def get_phase3_status(self) -> dict:
        """Get Phase 3 status for API."""
        return {
            "patch_executor": self.patch_executor.get_status(),
            "evolution_logger": self.evolution_logger.get_summary() if hasattr(self, 'evolution_logger') else {},
            "pending_draft": len(self.proposer.list_proposals(status="draft")),
            "evolution_tick": self._last_evolution_tick,
            "evolution_interval": self._evolution_interval,
        }

    def _get_action_interface(self):
        """Get the action interface execute function."""
        return type("ActionInterface", (), {"execute": staticmethod(l7_execute)})()

    def get_status(self) -> dict:
        """Get full L7 status for /l7-status endpoint."""
        return {
            "version": "0.1.0",
            "phase": 1,
            "running": self.is_running,
            "l7_tick": self._tick_counter,
            "mission_history": len(self.planner.mission_history),
            "last_status": self.last_status,
            "last_gaps": {
                "dormant_types": self.last_gaps.get("dormant_types", []),
                "stagnation_risk": self.last_gaps.get("stagnation_risk", False),
                "low_concentration": self.last_gaps.get("low_concentration", False),
                "low_novelty": self.last_gaps.get("low_novelty", False),
                "low_diversity": self.last_gaps.get("low_diversity", False),
                "dynamic_instability": self.last_gaps.get("dynamic_instability", False),
                "competition": self.last_gaps.get("competition", 0.0),
                "concentration_drop": self.last_gaps.get("concentration_drop", 0.0),
                "priority_missions": [p.get("reason", "") for p in self.last_gaps.get("priority_missions", [])],
            } if self.last_gaps else {},
            "tools_available": list(ALL_TOOLS.keys()),
        }

    def run_immediate_exploration(self, resting_state: dict, target_type: str = None) -> dict:
        """Trigger an immediate exploration mission (used from /l7-explore endpoint).
        
        Args:
            resting_state: L5 RESTING_STATE dict
            target_type: Optional specific L5 type to target. If None, auto-detect.
        
        Returns: result dict
        """
        l5_state = self.extract_l5_state(resting_state)
        
        if target_type:
            # Force a mission targeting a specific type
            mission = Mission(
                mission_type="external_explore",
                target_type=target_type,
                goal=f"Immediate exploration triggered for '{target_type}'",
                expected_outcome=f"Activate {target_type} via external stimulus",
                priority="high",
                action="web_search",
                params={"query": self.planner._generate_search_query(target_type, "")}
            )
        else:
            mission = self.planner.generate_mission(l5_state)
        
        if not mission:
            return {"status": "no_mission", "reason": "No gaps found, L5 seems healthy"}
        
        mission_result = self.planner.execute_mission(mission, self._get_action_interface())
        if not mission_result:
            return {"status": "error", "reason": "Mission execution failed"}
        
        obs = action_result_to_observation(mission_result, target_type=mission.target_type)
        injection_status = inject_observation(obs, resting_state)

        evidence_record = {
            "phase": "injected" if injection_status.get("injected") else "rejected",
            "mission_id": mission.id,
            "observation_id": injection_status.get("observation_id"),
            "target_type": mission.target_type,
            "result_status": mission_result.get("status"),
            "result_tool": mission_result.get("tool"),
            "result_count": obs.get("count", 0),
            "source_urls": obs.get("files", []),
            "source_domains": obs.get("source_domains", []),
            "search_backend": obs.get("search_backend"),
            "evidence_quality": obs.get("evidence_quality", 0.0),
            "reason": injection_status.get("reason", obs.get("evidence_reason")),
        }
        self._audit_external_closure(evidence_record)
        # Same closure contract as l7_tick: queue for next-tick effectiveness scoring.
        if injection_status.get("injected") and mission_result.get("tool") == "web_search":
            self._pending_external_closures.append({
                **evidence_record,
                "before": l5_state,
            })
        elif not injection_status.get("injected"):
            reject_fb = self.planner.record_rejected_mission(evidence_record)
            evidence_record["feedback_effective"] = reject_fb.get("effective", False)
            evidence_record["effective"] = reject_fb.get("effective", False)

        # Fetch first source page so promote/seed-bias has real text, not snippets only.
        source_urls = obs.get("files", []) or []
        fetch_status = None
        if (
            mission_result.get("tool") == "web_search"
            and injection_status.get("injected")
            and source_urls
        ):
            fetch_result = self._get_action_interface().execute(
                "web_fetch", url=source_urls[0]
            )
            if fetch_result.get("status") == "ok":
                fetch_obs = action_result_to_observation(
                    fetch_result, target_type=mission.target_type
                )
                fetch_status = inject_observation(fetch_obs, resting_state)
                self._audit_external_closure({
                    "phase": "fetched" if fetch_status.get("injected") else "fetch_rejected",
                    "mission_id": mission.id,
                    "observation_id": fetch_status.get("observation_id"),
                    "target_type": mission.target_type,
                    "result_status": fetch_result.get("status"),
                    "result_tool": fetch_result.get("tool"),
                    "source_urls": fetch_obs.get("files", []),
                    "source_domains": [],
                    "search_backend": obs.get("search_backend"),
                    "evidence_quality": obs.get("evidence_quality", 0.0),
                    "reason": fetch_status.get("reason"),
                })

        self._log("immediate_explore", {
            "mission_id": mission.id,
            "target_type": mission.target_type,
            "result_status": mission_result.get("status"),
            "injection": injection_status,
            "fetch": fetch_status,
            "pending_closures": len(self._pending_external_closures),
        })
        
        return {
            "status": "ok",
            "mission": mission.to_dict(),
            "action_result": {
                "tool": mission_result.get("tool"),
                "status": mission_result.get("status"),
                "summary": mission_result.get("summary", "")[:200],
            },
            "injection": injection_status,
            "fetch": fetch_status,
            "pending_closures": len(self._pending_external_closures),
        }

    def has_pending_observations(self, resting_state: dict) -> bool:
        """Quick check if there are unprocessed L7 observations."""
        obs = collect_l7_observations(resting_state)
        return len(obs) > 0

# [L7 Patch 83dec717] STAGNATION_BREAK — STAGNATION_BREAK: Stagnation streak=3 ticks (threshold=3) (2026-09-11 05:54:38)
# Risk: low | WARNING: method-level code, not auto-activated
# --- Original proposed code ---
# # [L7 Proposal] STAGNATION_BREAK
# # Gap: Stagnation streak=3 ticks (threshold=3)
# # In L7Orchestrator._bg_init() or l7_init.py:
# # 
# # When stagnation is detected, dispatch urgent exploration:
# if self.planner._last_stag_count >= 3 and self.planner._last_stag_count % 3 == 0:
#     # Force external exploration with urgency
#     self._force_exploration(target="curiosity")
# 
# --- End ---


"""
L9 Autonomy Monitor — Closed-Loop Verification of L6→L7→L8→L9 Circle

Monitors the complete self-improvement pipeline and detects when the loop
stops closing. Reports metrics to .lae/l9_autonomy_log.jsonl for dashboard
and TL-Ego self-model consumption.

Detection rules:
  - Loop stall: no L7 proposals applied in N ticks
  - False signal: same gap type repeatedly reviewed but never resolved
  - Governance drift: approval rate trending up or down outside bounds
"""

import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

TZ_CST = timezone(timedelta(hours=8))


class L9AutonomyMonitor:
    """Monitors the L6→L7→L8→L9 self-improvement loop for stalls and drift."""

    def __init__(self, workspace: Path = None):
        self.workspace = workspace or Path(__file__).parent
        self._l9_dir = self.workspace / ".lae" / "l9"
        self._l9_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._l9_dir / "autonomy_log.jsonl"
        self._state_path = self._l9_dir / "autonomy_state.json"

    def _log(self, entry: dict):
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load_state(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text("utf-8"))
            except Exception:
                pass
        return {
            "total_checks": 0,
            "stalls_detected": 0,
            "false_signals_detected": 0,
            "last_check_tick": 0,
            "last_stall_tick": 0,
        }

    def _save_state(self, state: dict):
        state["last_updated"] = datetime.now(TZ_CST).isoformat()
        self._state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), "utf-8"
        )

    def check_loop(self, tick: int, l8_stats: dict, l7_proposals: List[dict],
                   gap_snapshot: dict, runtime_generation_id: str = "") -> dict:
        """Run loop health check and log findings.

        Args:
            tick: Current TL-Ego tick.
            l8_stats: from L8Governance.get_stats()
            l7_proposals: from SelfModProposer.list_proposals()
            gap_snapshot: from GapAnalyzer.get_gap_report()
            runtime_generation_id: the caller tags each check with the running
                generation so the audit log can be split per boot. Added
                2026-10-01: tl_ego_server passes it, the signature did not accept
                it, and the TypeError aborted every governance check AFTER the L8
                review had already run — silent loss of the L9 half of the loop.

        Returns:
            dict with findings, stall flags, and recommendations.
        """
        state = self._load_state()
        state["total_checks"] += 1
        state["last_check_tick"] = tick
        if runtime_generation_id:
            state["last_generation_id"] = str(runtime_generation_id)

        findings = []

        # 1. Check for loop stall (no proposals applied)
        # list_proposals() returns dicts with status field — never bare status strings.
        if isinstance(l7_proposals, list):
            total = len(l7_proposals)
            applied = 0
            draft = 0
            reviewed = 0
            for item in l7_proposals:
                if isinstance(item, dict):
                    st = item.get("status")
                else:
                    st = item
                if st == "applied":
                    applied += 1
                elif st == "draft":
                    draft += 1
                elif st == "reviewed":
                    reviewed += 1
        else:
            total = 0
            applied = 0
            draft = 0
            reviewed = 0

        if total == 0:
            findings.append({
                "type": "loop_stalled",
                "severity": "critical",
                "detail": "No proposals generated at all — L7 proposer is not converting gaps to proposals.",
                "remediation": "Check L7 proposer gap-type matching in generate_proposals().",
            })
            state["stalls_detected"] += 1
            state["last_stall_tick"] = tick
        elif applied == 0 and total > 5 and draft == total:
            # Only call it stalled when NOTHING has left draft. Having applied/reviewed
            # means the loop is moving even if backlog remains.
            findings.append({
                "type": "loop_stalled",
                "severity": "high",
                "detail": f"{total} proposals exist but 0 applied — governance or execution gate is blocking.",
                "remediation": "Check L8 governance approval rate and L5 three-gate apply mechanism.",
            })
            state["stalls_detected"] += 1
            state["last_stall_tick"] = tick
        elif draft > 50 and applied > 0:
            findings.append({
                "type": "draft_backlog",
                "severity": "medium",
                "detail": f"{draft} drafts still pending while {applied} applied / {reviewed} reviewed.",
                "remediation": "Promote medium-risk flags to reviewed and clear comment-only GENERIC_FIX backlog.",
            })

        # 2. Check governance health
        # Stats historically used both "approve" and "approved" keys — accept both.
        total_reviewed = l8_stats.get("total_reviewed", 0)
        # L8 persists canonical keys and legacy aliases with the same values.
        # Prefer canonical values; only fall back to the legacy key for old stats.
        approved_count = l8_stats.get("approved")
        if approved_count is None:
            approved_count = l8_stats.get("approve", 0)
        approved_count = int(approved_count or 0)
        if total_reviewed > 0:
            approval_rate = approved_count / total_reviewed
            if approval_rate < 0.2 and total_reviewed >= 10 and applied == 0:
                findings.append({
                    "type": "governance_too_strict",
                    "severity": "medium",
                    "detail": f"Approval rate {approval_rate:.0%} over {total_reviewed} reviews — governance may be over-filtering.",
                    "remediation": "Consider relaxing medium-risk review criteria.",
                })
            elif approval_rate > 0.95 and total_reviewed >= 10:
                findings.append({
                    "type": "governance_too_lax",
                    "severity": "medium",
                    "detail": f"Approval rate {approval_rate:.0%} over {total_reviewed} reviews — governance may not be catching issues.",
                    "remediation": "Consider tightening review criteria for edge cases.",
                })

        # 3. Check gap resolution
        total_gaps = gap_snapshot.get("total_gaps", 0)
        if total_gaps > 0:
            gaps_c_by_s = gap_snapshot.get("by_severity", {})
            high_critical = gaps_c_by_s.get("high", 0) + gaps_c_by_s.get("critical", 0)
            if high_critical > 10:
                findings.append({
                    "type": "gap_accumulation",
                    "severity": "high",
                    "detail": f"{high_critical} high/critical gaps unresolved — self-improvement loop is not keeping up.",
                    "remediation": "Prioritize high-severity gap types in proposer routing.",
                })

        # 4. Overall verdict
        if any(f["severity"] == "critical" for f in findings):
            verdict = "loop_broken"
        elif any(f["severity"] == "high" for f in findings):
            verdict = "loop_degraded"
        else:
            verdict = "loop_healthy"

        report = {
            "ts": datetime.now(TZ_CST).isoformat(),
            "tick": tick,
            "verdict": verdict,
            "findings": findings,
            "metrics": {
                "total_proposals": total,
                "applied_proposals": applied,
                "draft_proposals": draft,
                "reviewed_proposals": reviewed,
                "total_reviewed": total_reviewed,
                "approved_count": approved_count,
                "total_gaps": total_gaps,
                "stalls_detected": state["stalls_detected"],
            },
        }

        self._log(report)
        self._save_state(state)
        return report

    def get_status(self) -> dict:
        """Get L9 monitor status."""
        state = self._load_state()
        recent = []
        if self._log_path.exists():
            try:
                with open(self._log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            recent.append(json.loads(line))
            except Exception:
                pass
        return {
            "state": state,
            "recent_checks": recent[-5:],
        }


# ── Convenience ──

def create_monitor(workspace: Path = None) -> L9AutonomyMonitor:
    return L9AutonomyMonitor(workspace)

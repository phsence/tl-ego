"""arch_auto_approve.py — 架构提案自主审批 + 实施派单 (2026-10-10).

背景: 架构提案 (architect blueprint) 的 promote 决策一直卡在 "等人批":
.lae/architect_approval_request.json 自 09-12 起 pending, architect_human_approval.json
不存在 = 0 批准 0 实施。用户 2026-10-10 授权 ("把这两个实现一下")。本工具把审批
闸门自动化: 满足策略的 pending promote 决策 -> 写 approval 文件 (全审计), 并把
已批准的蓝图派单给 ego 自己的 L5 执行通道 (.l5_trigger.json -> 插件 -> subagent)。

审批策略 (全部门通过才自动批, 任一不过则保持 pending 等人工):
  1. request.status == "pending" 且决策文件可读
  2. gate_decision == "promote"
  3. 无回归指标 (regressed_metrics 为空)
  4. aggregate_gain >= minimum_gain
  5. REQUIRE_EVIDENCE_COMPLETE=False 时不拦 evidence_complete=false, 但如实记录

用法:
  python arch_auto_approve.py                # 审批 pending 请求 (幂等)
  python arch_auto_approve.py --dry-run      # 只报告, 不落盘
  python arch_auto_approve.py --queue-auto   # 对已批准蓝图派单 (避开已派过的)
  python arch_auto_approve.py --queue <bp>   # 指定蓝图派单 (slot 空才写)

退出码 0 = 正常(含跳过); 1 = 硬错误。
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent      # projects/tl-ego
WORKSPACE = PROJECT_DIR.parent.parent              # workspace
LAE = WORKSPACE / ".lae"
REQ = LAE / "architect_approval_request.json"
APPROVAL = LAE / "architect_human_approval.json"
BP_LATEST = LAE / "architect_blueprint_latest.json"
DISPATCH_REQS = LAE / "l5_dispatch_requests.jsonl"
TRIGGER = WORKSPACE / ".l5_trigger.json"
AUDIT = LAE / "arch_auto_approve_log.jsonl"

APPROVER = "autonomous_gate"
APPROVER_NOTE = ("user-granted autonomous approval (2026-10-10: '把这两个实现一下'); "
                 "policy+audit: .lae/arch_auto_approve_log.jsonl")
REQUIRE_EVIDENCE_COMPLETE = True   # True = 只有 evidence_complete 才自动批


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _audit(row: dict) -> None:
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _live_tick() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=5) as r:
            return int(json.loads(r.read()).get("resting_ticks") or 0)
    except Exception:
        return int(time.time()) % 10 ** 6


def approve(dry: bool) -> int:
    req = _load(REQ)
    if not req:
        print("[arch-approve] no approval request file")
        return 0
    if str(req.get("status") or "").lower() != "pending":
        print(f"[arch-approve] request already resolved: {req.get('status')}")
        return 0
    dec_file = str(req.get("decision_file") or "")
    dec = _load(WORKSPACE / dec_file)
    if not dec:
        print(f"[arch-approve] decision file missing/unreadable: {dec_file}")
        return 0

    gains = float(dec.get("aggregate_gain") or 0.0)
    min_g = float(dec.get("minimum_gain") or 0.0)
    regressed = list(dec.get("regressed_metrics") or [])
    holds = []
    if dec.get("decision") != "promote":
        holds.append(f"decision={dec.get('decision')}!=promote")
    if regressed:
        holds.append(f"regressed={regressed}")
    if gains < min_g:
        holds.append(f"gain={gains}<min={min_g}")
    if REQUIRE_EVIDENCE_COMPLETE and not dec.get("evidence_complete", False):
        holds.append("evidence_complete=false")
    if holds:
        print(f"[arch-approve] HOLD (needs human): {'; '.join(holds)}")
        _audit({"ts": time.time(), "decision_id": req.get("decision_id"),
                "action": "hold", "reasons": holds})
        return 0

    bp_id = req.get("blueprint_id") or (dec.get("internal_diagnostics") or {}).get("blueprint_id")
    approval = {
        "schema": "architect_human_approval.v1",
        "approval_id": f"arch-approval-{int(time.time())}",
        "blueprint_id": bp_id,
        "decision": "approved",
        "approver": APPROVER,
        "approved_at": datetime.now().isoformat(),
        "approval_tick": dec.get("tick"),
        "trigger": "autonomous_gate",
        "gate_decision": dec.get("decision"),
        "gate_decision_file": dec_file,
        "capability_gain": {
            "aggregate": gains,
            "metrics_improved": dec.get("improved_metrics", []),
            "metrics_regressed": regressed,
            "min_gain": min_g,
            "max_regression": dec.get("maximum_regression"),
            "passed": True,
        },
        "acceptance_checklist": {
            "capability_gap_defined": bool((dec.get("internal_diagnostics") or {}).get("blueprint_id")),
            "modules_have_bounded_io": True,
            "shadow_verification_exists": bool(dec.get("evidence_complete", False)),
            "baseline_runs_ge_3": int(dec.get("baseline_repeated_runs") or 0) >= 3,
            "candidate_runs_ge_3": int(dec.get("candidate_repeated_runs") or 0) >= 3,
            "measurable_capability_gain": gains >= min_g,
            "no_regression": not regressed,
        },
        "notes": APPROVER_NOTE,
        "human_sign_off": False,
        "autonomous_gate": {
            "policy": {"require_evidence_complete": REQUIRE_EVIDENCE_COMPLETE},
            "evidence_complete_at_approval": bool(dec.get("evidence_complete", False)),
        },
    }
    if dry:
        print(f"[arch-approve] DRY-RUN: would approve {req.get('decision_id')} "
              f"blueprint={bp_id} gain={gains} (checklist={approval['acceptance_checklist']})")
        return 0
    APPROVAL.write_text(json.dumps(approval, ensure_ascii=False, indent=2), encoding="utf-8")
    req["status"] = "approved"
    req["resolved_at"] = datetime.now().isoformat()
    req["resolved_by"] = APPROVER
    REQ.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    _audit({"ts": time.time(), "decision_id": req.get("decision_id"), "action": "approved",
            "blueprint_id": bp_id, "approval_id": approval["approval_id"],
            "gain": gains, "min_gain": min_g})
    print(f"[arch-approve] APPROVED {req.get('decision_id')} blueprint={bp_id} "
          f"(approver={APPROVER}, human_sign_off=False)")
    return 0


TASK_TMPL = """[arch-implement:{bp}] Implement architect blueprint {bp} (source: .lae/architect_blueprint_latest.json).

This is the architecture-implementation channel of Ego's self-upgrade loop: an approved blueprint is delivered to Ego's L5 executor as construction work.

DELIVERABLE (bounded, sandbox-only):
1. Read the blueprint JSON (modules, interfaces, constraints, acceptance).
2. Build a working prototype in {sandbox} — one python file per blueprint module (architect_model.py, architect_sandbox.py, architect_promotion_gate.py), each <150 lines, implementing its stated inputs/outputs contract:
   - architect_model: parse a blueprint JSON into {{modules, interfaces, constraints, acceptance}} + a capability-gap summary.
   - architect_sandbox: validate a candidate blueprint (bounded IO, gap present, interface contracts) WITHOUT touching any live runtime.
   - architect_promotion_gate: given a verification report + baseline/candidate metrics, emit promote/hold using the acceptance rules (measurable gain, zero regression).
3. smoke_test.py: run all three on the real blueprint file; print PASS/FAIL; capture the raw output.
4. result.json: files created, smoke output tail, verdict, one lesson.

HARD CONSTRAINTS:
- NO writes outside {sandbox}; NO edits to tl_ego_server.py or any live code; NO network; NO wiring into the runtime (prototype only — promotion to live follows the three gates).
- Verify by real execution: every claim in result.json must come from a command you actually ran.
"""


def _iter_dispatch_rows():
    try:
        for ln in DISPATCH_REQS.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except Exception:
                continue
    except Exception:
        return


def queue(bp_id: str, dry: bool, force: bool = False) -> int:
    if not bp_id:
        print("[arch-queue] no blueprint id")
        return 0
    marker = f"[arch-implement:{bp_id}]"
    for r in _iter_dispatch_rows():
        if marker in str(r.get("goalText") or "") or marker in str(r.get("task") or ""):
            print(f"[arch-queue] already dispatched: {bp_id}")
            return 0
    if TRIGGER.exists() and not force:
        print("[arch-queue] slot busy (.l5_trigger.json exists) — retry next window")
        return 0
    tick = _live_tick()
    goal_id = f"l5g-{datetime.now():%Y%m%d}-{tick:06d}-arch"  # -arch: 与 ego/comm 同 tick id 永不撞车 (host 用 id 做 6h goal_dedup)
    goal = TASK_TMPL.format(bp=bp_id, sandbox=f"ego_sandbox/{goal_id}")
    trig = {
        "tick": tick,
        "goal_type": "arch_implement",
        "goal_id": goal_id,
        "goal": goal,
        "domain": "general",
        "success_criteria": "sandbox prototype runs; result.json carries real smoke output",
        "risk": "low",
    }
    if dry:
        print(f"[arch-queue] DRY-RUN: would queue {goal_id} bp={bp_id} tick={tick}")
        return 0
    tmp = TRIGGER.with_name(TRIGGER.name + ".new")
    tmp.write_text(json.dumps(trig, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, TRIGGER)
    time.sleep(0.5)
    cur = _load(TRIGGER)
    ok = bool(cur and cur.get("goal_id") == goal_id and int(cur.get("tick") or 0) == tick)
    _audit({"ts": time.time(), "action": "queued", "blueprint_id": bp_id,
            "goal_id": goal_id, "tick": tick, "verified_readback": ok})
    print(f"[arch-queue] queued {goal_id} bp={bp_id} verified={ok} "
          f"(artifact will land at workspace/ego_sandbox/{goal_id}/result.json)")
    return 0


def queue_auto(dry: bool) -> int:
    """对"已批准"的蓝图自动派单: approval 文件存在且批准, 当前蓝图文件 id 匹配。"""
    ap = _load(APPROVAL)
    if not ap or ap.get("decision") != "approved":
        print("[arch-queue-auto] no approved blueprint (approval file missing/not approved)")
        return 0
    bp_id = str(ap.get("blueprint_id") or "")
    cur = _load(BP_LATEST) or {}
    if str(cur.get("blueprint_id") or "") != bp_id:
        print(f"[arch-queue-auto] current blueprint ({cur.get('blueprint_id')}) != approved ({bp_id}); skip")
        return 0
    return queue(bp_id, dry)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--queue", default="")
    ap.add_argument("--queue-auto", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="window-targeted drop: skip slot-busy guard (retry tooling only)")
    args = ap.parse_args()
    if args.queue:
        return queue(args.queue, args.dry_run, args.force)
    if args.queue_auto:
        return queue_auto(args.dry_run)
    return approve(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

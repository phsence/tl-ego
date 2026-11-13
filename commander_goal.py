"""Commander goal ingestion — pure module.

Safe to import from ANY process (shim 8770, main server 8766, tests):
no tl_ego_server import, no single-instance checks, stdlib only.

Three jobs:
  1. build_commander_goal()  — commander goal dict (goal_id matches
     ^l5g-\\d{8}-\\d{6} prefix required by server _record_goal_terminal)
  2. write_trigger()         — .l5_comm_trigger.json (2nd trigger channel,
     consumed by OpenClaw tl-ego plugin, kind="comm")
  3. write_outbox()          — typed events (accepted/terminal) to
     .lae/outbox/user_reports.jsonl for Hermes cron delivery

Protocol facts sourced from docs/commander-trigger-protocol.md (Task 0).
"""
import hashlib
import json
import time
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parents[2]
COMM_TRIGGER_FILE = _WORKSPACE / ".l5_comm_trigger.json"
OUTBOX_DIR = _WORKSPACE / ".lae" / "outbox"
OUTBOX_FILE = OUTBOX_DIR / "user_reports.jsonl"

EXECUTION_POLICY = (
    "sandbox-autonomous: plugin may dispatch and execute the bounded task inside "
    "ego_sandbox/<goal_id>/; no config edits/deletes/restarts/external messages/"
    "device control; live code mutation remains denied and must use the three-gate "
    "repair channel; cleanup: delete all temp files (.bat/.ps1/.py/.tmp) created "
    "outside the sandbox during execution"
)

_PRIORITY_MAP = {"low": 0.95, "medium": 0.96, "high": 0.98, "critical": 0.99}


def _timestamp_parts(now):
    """'2026-08-29T12:00:00' -> ('2026-08-29T12:00:00', '20260829', '120000')"""
    digits = now.replace("-", "").replace(":", "").replace("T", "")
    return now, digits[:8], digits[8:14]


def build_commander_goal(task, kind="directive", success_criteria="",
                         priority="high", horizon="medium", now=None, tick=None):
    """Build a commander goal row. kind: 'directive' | 'feedback'.

    tick: pass the current EGO tick (never unix time — the plugin consumes the
    trigger tick into .l5_trigger.consumed; a unix-time tick would poison the
    consumed marker and block all future ego triggers).  Falls back to a safe
    ledger-derived tick when omitted.
    """
    task = (task or "").strip()
    if not task:
        raise ValueError("commander goal: empty task")
    if kind not in ("directive", "feedback"):
        raise ValueError(f"commander goal: bad kind {kind!r}")
    if now is None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if tick is None:
        tick = load_commander_tick()
    tick = int(tick)
    if tick >= 10**7:  # poison guard: refuse unix-time ticks
        tick = load_commander_tick()
    iso, date_part, time_part = _timestamp_parts(now)
    digest = hashlib.sha256(task.encode("utf-8")).hexdigest()[:4]
    # ^l5g-\d{8}-\d{6} prefix (server terminal-write hard constraint), + stable hash
    goal_id = f"l5g-{date_part}-{time_part}-C{digest}"
    goal_type = "commander_directive" if kind == "directive" else "commander_feedback"
    prio = _PRIORITY_MAP.get(str(priority).lower(), 0.96)
    goal = {
        "goal_id": goal_id,
        "ts": iso,
        "tick": int(tick),
        "title": f"Commander {kind}: {task[:60]}",
        "status": "sandbox_autonomous_running",
        "priority": prio,
        "goal": task,
        "task": task,
        "success_criteria": success_criteria or "task completed inside sandbox with result.json",
        "goal_type": goal_type,
        "source": "user_directive",
        "origin": "commander",
        "horizon": horizon,
        "domain": "commander",
        "risk": "low",
        "terminal_state": None,
        "autonomy_mode": "sandbox_autonomous",
        "proposal_only": False,
        "live_mutation_authority": "denied",
        "execution_policy": EXECUTION_POLICY,
    }
    if kind == "feedback":
        goal["feedback_route"] = "sandbox_three_gate"
    return goal


def parse_directive_body(body):
    """Validate + normalize POST /directive body. Raises ValueError on empty task.

    Returns {task, kind, success_criteria, priority, horizon} with defaults.
    """
    body = body or {}
    task = str(body.get("task", "") or "").strip()
    if not task:
        raise ValueError("task required")
    kind = "feedback" if str(body.get("kind", "")) == "feedback" else "directive"
    return {
        "task": task,
        "kind": kind,
        "success_criteria": str(body.get("success_criteria", "") or ""),
        "priority": str(body.get("priority", "") or "high"),
        "horizon": str(body.get("horizon", "") or "medium"),
    }


COMMANDER_INDEX_PATH = _WORKSPACE / ".lae" / "commander_goal_index.json"
_COMMANDER_INDEX_MAX = 5000


def load_commander_index(index_path=None):
    """Read commander goal index {goal_id: {task, goal_type}}."""
    index_path = Path(index_path) if index_path else COMMANDER_INDEX_PATH
    try:
        if index_path.exists():
            return json.loads(index_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {}


def save_commander_index(idx, index_path=None):
    """Write index, capped at latest _COMMANDER_INDEX_MAX entries."""
    index_path = Path(index_path) if index_path else COMMANDER_INDEX_PATH
    try:
        if len(idx) > _COMMANDER_INDEX_MAX:
            idx = dict(list(idx.items())[-_COMMANDER_INDEX_MAX:])
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[commander-index] save error: {e}", flush=True)


def register_commander_goal(goal, index_path=None):
    """Register a commander goal at creation time (called from _append_l5_goal)."""
    idx = load_commander_index(index_path=index_path)
    idx[goal["goal_id"]] = {"task": goal.get("task", ""),
                            "goal_type": goal.get("goal_type")}
    save_commander_index(idx, index_path=index_path)


def is_commander_goal(goal_id, index_path=None):
    """O(1) lookup — used by the terminal hook to decide outbox emission."""
    return str(goal_id) in load_commander_index(index_path=index_path)


def commander_task_text(goal_id, index_path=None):
    """Task text for the outbox report."""
    return load_commander_index(index_path=index_path).get(str(goal_id), {}).get("task", "")


def wait_until_dispatched(goal_id, timeout_s=3600, poll_s=5,
                          workspace=None, log=lambda m: None):
    """Keep the comm trigger alive until the plugin dispatches the goal.

    The plugin DELETES .l5_comm_trigger.json on pid_cooldown blocks (anti-pile
    guard) — a commander trigger written during a cooldown window would be
    silently eaten.  This keeper rewrites the trigger every poll until the
    goal_id appears in ego_dispatch_state.json dispatched_goals (or timeout).

    Run in a daemon thread from the /directive route.
    """
    workspace = Path(workspace) if workspace else _WORKSPACE
    trigger_path = workspace / ".l5_comm_trigger.json"
    state_path = workspace / ".lae" / "ego_dispatch_state.json"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if goal_id in (state.get("dispatched_goals") or {}):
                log(f"dispatched: {goal_id}")
                return True
        except Exception:
            pass
        try:
            # 2026-10-15: re-assert whenever the slot holds someone ELSE's trigger,
            # not only when the file is missing. The core's own comm impulses rewrite
            # .l5_comm_trigger.json every few minutes (latest-wins), which used to
            # silently drop a queued commander directive before the dispatcher's next
            # window (measured: directive l5g-20261015-085100-Ca647 overwritten 3x in
            # 8 min while the global slot was busy). A commander order wins until it ships.
            _need = True
            if trigger_path.exists():
                try:
                    _cur = json.loads(trigger_path.read_text(encoding="utf-8"))
                    _need = (_cur.get("goal_id") != goal_id)
                except Exception:
                    _need = True
            if _need:
                g = {"goal_id": goal_id,
                     # 2026-10-16 root-cause fix: write_trigger() reads goal["goal"];
                     # the keeper supplied "task" instead → KeyError 'goal' on every
                     # rewrite (761 keeper errors since the 10-15 re-assert landed;
                     # Ca647/Cfd43/Cf606/C7a2d all survived only by winning the
                     # race against the next comm impulse).
                     "goal": commander_task_text(goal_id) or goal_id,
                     "goal_type": "commander_directive",
                     "tick": load_commander_tick(workspace),
                     "success_criteria": "task completed inside sandbox with result.json",
                     "risk": "low", "priority": 0.96,
                     "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
                write_trigger(g, trigger_dir=workspace)
                log(f"trigger rewritten for {goal_id}")
        except Exception as e:
            log(f"keeper error: {e}")
        time.sleep(poll_s)
    log(f"timeout waiting for dispatch: {goal_id}")
    return False


def load_commander_tick(workspace=None):
    """Current ego tick (RESTING_STATE tick), read from the freshest ledger row.

    NEVER use unix time here: the plugin saveConsumedTick()s the trigger tick —
    a unix-time tick would poison .l5_trigger.consumed and make ALL future ego
    triggers (tick ~4-5k) fail the `trigger.tick <= consumed.tick` guard.
    """
    workspace = Path(workspace) if workspace else _WORKSPACE
    try:
        ledger = workspace / ".lae" / "l5_goals.jsonl"
        if ledger.exists():
            with open(ledger, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-50:]
            for line in reversed(lines):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r.get("tick"), int) and r.get("tick") and r["tick"] < 10**7:
                    return r["tick"] + 1
    except Exception:
        pass
    return 1  # safe fallback: above fresh-process 0, far below poison territory


def write_trigger(goal, trigger_dir=None):
    """Write .l5_comm_trigger.json (2nd channel; plugin consumes kind='comm').

    Fields per checkAndConsumeCommTrigger(): tick, goal_type, goal, domain,
    goal_id, success_criteria, risk.  Extras are carried for observability.
    """
    trigger_dir = Path(trigger_dir) if trigger_dir else _WORKSPACE
    trigger_dir = Path(trigger_dir)
    trigger = {
        "tick": goal.get("tick", 0),
        "goal_type": goal["goal_type"],
        "goal": goal["goal"],
        "goal_id": goal["goal_id"],
        "success_criteria": goal["success_criteria"],
        "risk": goal.get("risk", "low"),
        "domain": goal.get("domain", "commander"),
        "reason": f"commander directive ({goal.get('kind_hint', 'task')})",
        "intent_type": "commander_directive" if goal["goal_type"] == "commander_directive" else "commander_feedback",
        "source": "user_directive",
        "priority": goal.get("priority"),
        "ts": goal.get("ts"),
    }
    target = trigger_dir / ".l5_comm_trigger.json"
    target.write_text(json.dumps(trigger, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target


def write_outbox(goal, event, outcome=None, report=None, outbox_dir=None):
    """Append one typed event row to user_reports.jsonl.

    event: 'accepted' | 'terminal'  (terminal carries outcome + report)
    """
    outbox_dir = Path(outbox_dir) if outbox_dir else OUTBOX_DIR
    outbox_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "event": event,
        "goal_id": goal["goal_id"],
        "task": goal.get("task", ""),
        "goal_type": goal.get("goal_type"),
        "source": "user_directive",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if event == "terminal":
        row["outcome"] = outcome or "unknown"
        row["report"] = (report or "")[:2000]
    target = outbox_dir / "user_reports.jsonl"
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return target

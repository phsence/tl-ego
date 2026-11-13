#!/usr/bin/env python3
"""P4: ego_internal bridge — wraps real ego systems as tool_router executors.

Upgrades 4 of the 5 ego_internal tools from stub to real:
- recall_memory  → MemoryBridge.retrieve()
- remember        → MemoryBridge._write_hy() with content adaptation
- check_status    → calls ego /status endpoint via HTTP
- set_goal        → stub (no goal-management HTTP API found in ego project)

tts_speak remains stub (TTS integration is outside this project's scope).

Architecture:
  tool_router → ego_internal_bridge → real ego subsystem
                              ↓ (error)
                         graceful fallback to stub
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)


# ── Lazy load memory bridge (avoid circular imports) ──────────────────
_bridge = None
_bridge_load_attempted = False
_bridge_error = None


def _get_bridge():
    """Lazy-load MemoryBridge singleton."""
    global _bridge, _bridge_load_attempted, _bridge_error
    if _bridge_load_attempted:
        return _bridge
    _bridge_load_attempted = True
    try:
        from memory_bridge import get_bridge as _mb_get_bridge
        _bridge = _mb_get_bridge()
    except Exception as e:
        _bridge_error = str(e)
        _bridge = None
    return _bridge


# ── Real ego_internal executors ───────────────────────────────────────
def _real_recall_memory(query: str, limit: int = 5) -> dict:
    """recall_memory(query, limit). Uses MemoryBridge.retrieve()."""
    bridge = _get_bridge()
    if bridge is None:
        return {
            "status": "error",
            "error": f"MemoryBridge unavailable: {_bridge_error}",
            "fallback": "stub",
        }
    try:
        context = bridge.retrieve(query, limit=limit)
        if not context:
            return {
                "status": "ok",
                "matches": [],
                "total": 0,
                "context_text": "",
            }
        # Parse the formatted context string into rough match list
        lines = [ln.strip() for ln in context.split("\n") if ln.strip()]
        return {
            "status": "ok",
            "matches": [{"content": ln[:300], "score": 1.0} for ln in lines[:limit]],
            "total": len(lines),
            "context_text": context,
            "source": "MemoryBridge.retrieve()",
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "fallback": "stub",
        }


def _real_remember(content: str, tags: list = None, importance: float = 0.5) -> dict:
    """remember(content, tags, importance). Writes to Hy-Memory via MemoryBridge._write_hy().

    Adapts the (content, tags, importance) shape to the contemplation format
    that MemoryBridge.write() expects, but uses _write_hy() directly because
    we don't have a full contemplation result.
    """
    bridge = _get_bridge()
    if bridge is None:
        return {
            "status": "error",
            "error": f"MemoryBridge unavailable: {_bridge_error}",
            "fallback": "stub",
        }
    try:
        # Format memory text
        import time as _time
        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        tag_str = ", ".join(tags) if tags else "general"
        text = (
            f"[{ts}] [tag={tag_str}] [importance={importance:.2f}]\n"
            f"{content}"
        )
        ok = bridge._write_hy(text)
        if ok:
            return {
                "status": "ok",
                "memory_id": f"hy_{hash(content) % 100000:05d}",
                "stored_at": ts,
                "source": "MemoryBridge._write_hy()",
            }
        else:
            return {
                "status": "error",
                "error": "MemoryBridge._write_hy() returned False",
                "fallback": "stub",
            }
    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "fallback": "stub",
        }


def _live_state_from_status(data: dict) -> dict:
    """Live cognitive-state scalars ego needs for honest self-report.

    2026-10-16: ego told the commander it 'cannot see' stagnation/competition/
    phase. Not true — /status carries them; this function's caller used to
    hand-pick 8 keys and drop the rest, so the LLM never received them.
    ponytail: explicit key list, not a full dump — /status also carries huge
    nested blobs (edge_view, snn, continuity) that would blow up context.
    """
    snap = data.get("last_neural_snapshot") or {}
    ch = data.get("dominant_channels") or {}
    comm = data.get("comm") or {}
    live = {k: data.get(k) for k in (
        "resting_dominant", "raw_attractor_dominant", "stagnation_streak",
        "resting_competition", "resting_concentration",
        "raw_attractor_competition", "raw_attractor_concentration",
        "gate_divergent",
    )}
    live.update({k: snap.get(k) for k in (
        "dominant", "phase", "competition", "concentration", "prev_dominant",
        "transition", "entropy_bits", "prediction_error", "n_activated",
        "diversity_n", "noise_mode", "stagnation_streak",
    )})
    for ch_name in ("observed_raw", "arbitration_candidate", "behavioral"):
        v = (ch.get(ch_name) or {}).get("value")
        if v is not None:
            live[f"channel_{ch_name}"] = v
    for k in ("motivation", "suppress_until", "message_count"):
        if k in comm:
            live[f"comm_{k}"] = comm[k]
    # keep zeros/False (stagnation_streak=0 is a value, not absence)
    return {k: v for k, v in live.items() if v is not None}


def _real_check_status() -> dict:
    """check_status(). Queries ego HTTP /status endpoint."""
    # Default ego port
    ego_url = os.environ.get("EGO_STATUS_URL", "http://127.0.0.1:8766/status")
    # Intermittent TimeoutError observed when ego is mid-contemplate
    # (single-busy-server): 5s was too tight. 15s + one retry.
    last_err = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(ego_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            return {
                "status": "ok",
                # /status exposes the resting-loop tick + dominant under the
                # `resting_*` keys (see tl_ego_server.py _status_payload).
                # Older callers may still expect the short names; map both.
                "tick": data.get("tick") or data.get("resting_ticks"),
                "dominant": (
                    data.get("dominant")
                    or data.get("dominant_type")
                    or data.get("resting_dominant")
                ),
                "resting_ticks": data.get("resting_ticks"),
                "resting_dominant": data.get("resting_dominant"),
                "neurons": data.get("neurons"),
                "edges": data.get("full_edges") or data.get("edges"),
                "uptime_s": data.get("uptime_s"),
                "source": f"GET {ego_url}",
                # 2026-10-16: was raw_keys[:10] — debug noise that actively
                # misled ego into thinking /status only exposes those keys.
                "live_state": _live_state_from_status(data),
            }
        except urllib.error.URLError as e:
            last_err = f"ego /status unreachable: {e.reason}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return {
        "status": "error",
        "error": last_err,
        "ego_url": ego_url,
        "fallback": "stub",
    }


def _real_set_goal(goal: str, priority: str = "medium", horizon: str = "medium",
                   kind: str = "directive") -> dict:
    """Commander goal ingestion via 8766 POST /directive (HTTP loopback).

    kind='directive' → commander_directive (task); kind='feedback' →
    commander_feedback (defect critique → improvement work order).
    On success the goal enters the L5 pipeline (source=user_directive) and is
    dispatched through the comm trigger channel. On server outage the tool
    degrades to an L4 memory record — never hard-fails the AgentLoop.
    """
    import json as _json
    import time as _time
    import urllib.request as _ur
    goal = (goal or "").strip()
    if not goal:
        return {"status": "error", "error": "empty goal"}
    _kind = "feedback" if str(kind) == "feedback" else "directive"
    payload = _json.dumps({
        "task": goal,
        "kind": _kind,
        "priority": priority,
        "horizon": horizon,
    }).encode("utf-8")
    req = _ur.Request("http://127.0.0.1:8766/directive", data=payload,
                      headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=15) as _r:
            _resp = _json.loads(_r.read().decode("utf-8"))
    except Exception as _e:
        # Main server unreachable → degrade to memory, never break the loop.
        _real_remember(f"[GOAL|{priority}|{horizon}] {goal}",
                       tags=["goal", horizon, "commander"], importance=0.8)
        return {"status": "degraded", "goal": goal,
                "note": f"L5 ingest unavailable ({type(_e).__name__}); recorded to memory only",
                "set_at": _time.strftime("%Y-%m-%dT%H:%M:%S")}
    if _resp.get("status") == "queued":
        _real_remember(f"[COMMANDER-GOAL|{_resp.get('goal_id')}] {goal}",
                       tags=["goal", "commander"], importance=0.8)
        return {"status": "ok", "goal_id": _resp.get("goal_id"), "queued": True,
                "permission": _resp.get("permission", "sandbox"),
                "set_at": _time.strftime("%Y-%m-%dT%H:%M:%S")}
    return {"status": "error", "goal": goal, "detail": _resp.get("error", "unknown")}


def _real_list_goals(source: str = "user_directive", limit: int = 10,
                     status: str = None, ledger_path: str = None) -> dict:
    """Query commander goals from the L5 ledger (ego self-tracking organ).

    Reads the tail of l5_goals.jsonl, merges creation rows (source=user_directive)
    with terminal rows (same goal_id), returns most recent first.
    """
    import json as _json
    from pathlib import Path as _P
    if ledger_path:
        _p = _P(ledger_path)
    else:
        _p = _P(__file__).resolve().parents[2] / ".lae" / "l5_goals.jsonl"
    if not _p.exists():
        return {"goals": [], "count": 0, "note": "ledger missing"}
    try:
        with open(_p, "r", encoding="utf-8", errors="replace") as _f:
            _lines = _f.readlines()[-4000:]
    except Exception as _e:
        return {"goals": [], "count": 0, "error": f"{type(_e).__name__}: {_e}"}
    _created, _terminal = {}, {}
    for _line in _lines:
        _line = _line.strip()
        if not _line:
            continue
        try:
            _r = _json.loads(_line)
        except Exception:
            continue
        _gid = _r.get("goal_id")
        if not _gid:
            continue
        if _r.get("source") == source and "task" in _r:
            _created[_gid] = _r
        if _r.get("terminal_state") in ("succeeded", "failed", "timed_out",
                                        "cancelled", "superseded_by_late_success"):
            _terminal[_gid] = _r
    _out = []
    for _gid, _c in _created.items():
        _t = _terminal.get(_gid, {})
        _out.append({
            "goal_id": _gid,
            "task": _c.get("task", ""),
            "goal_type": _c.get("goal_type"),
            "status": _c.get("status"),
            "priority": _c.get("priority"),
            "horizon": _c.get("horizon"),
            "created_ts": _c.get("ts") or _c.get("created_at"),
            "terminal_state": _t.get("terminal_state"),
            "outcome": _t.get("outcome"),
            "terminal_ts": _t.get("terminal_ts"),
            "reason": str(_t.get("reason") or "")[:200],
        })
    _out.sort(key=lambda g: g.get("created_ts") or "", reverse=True)
    if status:
        _out = [g for g in _out if g.get("terminal_state") == status
                or g.get("status") == status]
    try:
        _limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        _limit = 10
    _out = _out[:_limit]
    return {"goals": _out, "count": len(_out), "source": source}


# ── Bind to action_tools ──────────────────────────────────────────────
def install_ego_internal_bridge() -> int:
    """Replace stub executors for 4 ego_internal tools with real ones.

    Returns count of executors upgraded.
    """
    from action_tools import ACTION_TOOLS, ActionTool

    upgrades = {
        "recall_memory": _real_recall_memory,
        "remember": _real_remember,
        "set_goal": _real_set_goal,
        "check_status": _real_check_status,
        "list_goals": _real_list_goals,
    }
    count = 0
    for tool in ACTION_TOOLS:
        if tool.name in upgrades:
            # Mark as upgraded (we replace, don't double-bind)
            tool.executor = upgrades[tool.name]
            tool.category = "ego_internal_real"
            count += 1
    return count


# Auto-install on import
install_ego_internal_bridge()

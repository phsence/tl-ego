#!/usr/bin/env python3
"""P1: Unified tool router — SINGLE ENTRY POINT for all tool execution.

Three trigger sources can call this:
- internal_l10:  ego's autonomous L10 decision layer (P2)
- external_openai: clients via OpenAI-compatible API (P3)
- evolution_l5_l9: L5→L9 evolution pipeline (future)

All three funnel through ToolRouter.execute(name, args, source) so:
1. Execution is consistent (same executor, same error handling)
2. Every call gets logged to L4 memory with source tag
3. ego can introspect: "what tools did I run today?" (P6 self-cognition)

Architecture:
  source → ToolRouter.execute() → ActionTool.executor(**args)
                                ↓
                          result / error
                                ↓
                          _inject_to_memory(name, args, result, source)
                                ↓
                          return {"status", "result"|"error", "memory_id"}
"""
import json
import os
import sqlite3
import time
import uuid
import threading
from typing import Optional

from action_tools import ACTION_TOOLS, get_action_tool
from executor_registry import bind_executors, get_executor

# Pre-install all known bridges so executors are upgraded before first use.
# Order matters: ego_internal_bridge upgrades stubs; L7 bridge adds new tools.
try:
    import ego_internal_bridge  # noqa: F401  (upgrades 4 executors)
except ImportError:
    pass
try:
    import ego_tool_l7_bridge  # noqa: F401  (registers 5 L7 tools)
except ImportError:
    pass
try:
    import ego_minecraft_bridge  # noqa: F401  (upgrades 4 MC tools + mc_world_state)
except ImportError:
    pass
try:
    import ego_mc_body_bridge  # noqa: F401  (numen 身体工具 mc_* + 无前缀别名: scan_nearby_entities/scan_blocks/attack/eat/...)
except ImportError:
    pass


# ── Memory persistence (P7: SQLite, cross-process) ─────────────────────
# Replaces the P1 in-memory list so ego_memory_api (8772) can read
# executions recorded by ego_openai_shim (8770) in a separate process.
_MEMORY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lae", "tool_executions.db")
_MEMORY_LOCK = threading.Lock()


def _db_connect() -> sqlite3.Connection:
    """Open a SQLite connection (WAL mode for cross-process reads)."""
    os.makedirs(os.path.dirname(_MEMORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_MEMORY_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tool_executions (
            memory_id   TEXT PRIMARY KEY,
            memory_type TEXT,
            tool        TEXT,
            args        TEXT,
            result      TEXT,
            source      TEXT,
            ts          REAL,
            iso_ts      TEXT,
            error       TEXT
        )"""
    )
    return conn


def _inject_to_memory(name: str, args: dict, result: dict, source: str, error: Optional[str] = None) -> str:
    """Inject a tool execution record into L4 memory (SQLite, cross-process)."""
    memory_id = f"tool_{uuid.uuid4().hex[:12]}"
    try:
        # 2026-09-09 auto-binding: every real action is queued as an episodic
        # memory candidate (vec=None; the server's binding worker embeds it
        # off the hot path). Zero-throw: memory recording must never fail
        # because of binding.
        from neural_direct import queue_episodic_binding
        queue_episodic_binding({"memory_id": memory_id, "tool": name, "args": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args), "result": json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result), "error": error}, None)
    except Exception:
        pass
    record = {
        "memory_id": memory_id,
        "memory_type": "tool_execution",
        "tool": name,
        "args": args,
        "result": result,
        "source": source,  # "internal_l10" | "external_openai" | "evolution_l5_l9"
        "ts": time.time(),
        "iso_ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "error": error,
    }
    with _MEMORY_LOCK:
        conn = _db_connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO tool_executions
                   (memory_id, memory_type, tool, args, result, source, ts, iso_ts, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["memory_id"],
                    record["memory_type"],
                    record["tool"],
                    json.dumps(record["args"], ensure_ascii=False),
                    json.dumps(record["result"], ensure_ascii=False),
                    record["source"],
                    record["ts"],
                    record["iso_ts"],
                    record["error"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return memory_id


def get_memory_log(source: Optional[str] = None, limit: int = 100,
                   tool: Optional[str] = None, since: Optional[str] = None,
                   order: str = "desc") -> list[dict]:
    """Read memory log from SQLite. Filters pushed into SQL (audit finding D).

    order="desc" → newest first; "asc" → chronological.
    since: ISO timestamp lower bound on iso_ts.
    """
    order = "ASC" if order == "asc" else "DESC"
    clauses, params = [], []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if tool:
        clauses.append("tool = ?")
        params.append(tool)
    if since:
        clauses.append("iso_ts >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT memory_id, memory_type, tool, args, result, source, ts, iso_ts, error "
        f"FROM tool_executions {where} ORDER BY rowid {order} LIMIT ?"
    )
    params.append(limit)
    with _MEMORY_LOCK:
        conn = _db_connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    log = []
    for row in rows:
        log.append({
            "memory_id": row[0],
            "memory_type": row[1],
            "tool": row[2],
            "args": json.loads(row[3]) if row[3] else {},
            "result": json.loads(row[4]) if row[4] else {},
            "source": row[5],
            "ts": row[6],
            "iso_ts": row[7],
            "error": row[8],
        })
    return log


def clear_memory_log() -> None:
    """Clear the memory log (for tests / reset)."""
    with _MEMORY_LOCK:
        conn = _db_connect()
        try:
            conn.execute("DELETE FROM tool_executions")
            conn.commit()
        finally:
            conn.close()


# ── Decision envelope (credit assignment) ─────────────────────────────
# /contemplate writes the decision context under the job id BEFORE tools run,
# and /feedback joins the measured outcome back onto it. Without this the
# teacher sample stays pending_outcome forever and ego learns nothing.
_ENVELOPE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lae", "decision_envelopes.db")
_ENVELOPE_LOCK = threading.Lock()
_ENVELOPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_envelopes (
    decision_id TEXT PRIMARY KEY,
    route       TEXT,
    state_before TEXT,
    tool_calls  TEXT,
    actions     TEXT,
    outcome     TEXT,
    updated_ts  REAL
)"""


def _envelope_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_ENVELOPE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_ENVELOPE_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_ENVELOPE_SCHEMA)
    return conn


def upsert_decision_envelope(decision_id: str, route: str = "", state_before=None, actions=None) -> dict:
    """Write/refresh the decision context. Merges: later calls add, never clobber."""
    if not decision_id:
        return {"written": 0, "reason": "missing_decision_id"}
    with _ENVELOPE_LOCK:
        conn = _envelope_connect()
        try:
            row = conn.execute(
                "SELECT route, state_before, tool_calls, actions FROM decision_envelopes WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                route_e, before_e, calls_e, actions_e = route, state_before, None, None
            else:
                route_e = route or row[0]
                # state_before is written once (the query); L10 appends actions later.
                before_e = row[1] if row[1] is not None else state_before
                calls_e = row[2]
                actions_e = row[3]
            if actions:
                prev = json.loads(actions_e) if actions_e else []
                for act in actions if isinstance(actions, list) else [actions]:
                    if act not in prev:
                        prev.append(act)
                actions_e = json.dumps(prev, ensure_ascii=False)
            conn.execute(
                "INSERT OR REPLACE INTO decision_envelopes "
                "(decision_id, route, state_before, tool_calls, actions, outcome, updated_ts) "
                "VALUES (?,?,?,?,?,(SELECT outcome FROM decision_envelopes WHERE decision_id=?),?)",
                (decision_id, route_e,
                 # carry-over columns are already JSON text; only new values need encoding
                 before_e if isinstance(before_e, str) or before_e is None
                 else json.dumps(before_e, ensure_ascii=False),
                 calls_e, actions_e, decision_id, time.time()),
            )
            conn.commit()
            return {"written": 1, "decision_id": decision_id, "path": str(_ENVELOPE_DB_PATH)}
        finally:
            conn.close()


def get_decision_envelope(decision_id: str) -> dict:
    """Read back the full envelope for one decision."""
    if not decision_id:
        return {"decision_id": decision_id, "error": "missing_decision_id"}
    with _ENVELOPE_LOCK:
        conn = _envelope_connect()
        try:
            row = conn.execute(
                "SELECT route, state_before, tool_calls, actions, outcome, updated_ts "
                "FROM decision_envelopes WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return {"decision_id": decision_id, "error": "not_found"}
    return {
        "decision_id": decision_id,
        "route": row[0],
        "state_before": json.loads(row[1]) if row[1] else None,
        "tool_calls": json.loads(row[2]) if row[2] else [],
        "actions": json.loads(row[3]) if row[3] else [],
        "outcome": json.loads(row[4]) if row[4] else None,
        "updated_ts": row[5],
    }


def record_action_outcome(decision_id: str, outcome: str, evidence="", judge: str = "neutral",
                          reason: str = "", tool: str = "", args=None, source: str = "") -> dict:
    """Join a measured /feedback outcome onto the envelope (the credit edge).

    The envelope must already exist: an outcome with no recorded decision is
    evidence of nothing and would silently credit the wrong decision.
    """
    if not decision_id:
        return {"updated": 0, "reason": "missing_decision_id"}
    if get_decision_envelope(decision_id).get("error") == "not_found":
        return {"updated": 0, "reason": "unknown_decision_id"}
    with _ENVELOPE_LOCK:
        conn = _envelope_connect()
        try:
            row = conn.execute("SELECT outcome FROM decision_envelopes WHERE decision_id=?", (decision_id,)).fetchone()
            prev = json.loads(row[0]) if row and row[0] else []
            if not isinstance(prev, list):
                prev = [{"outcome": str(prev)}]
            prev.append({
                "outcome": str(outcome or "unknown")[:80],
                "judge": str(judge or "neutral")[:80],
                "reason": str(reason or "")[:500],
                "tool": str(tool or "")[:120],
                "args": args if isinstance(args, dict) else {},
                "source": str(source or "")[:120],
                "evidence": str(evidence or "")[:2000],
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            conn.execute("UPDATE decision_envelopes SET outcome=?, updated_ts=? WHERE decision_id=?",
                         (json.dumps(prev, ensure_ascii=False), time.time(), decision_id))
            conn.commit()
            return {"updated": 1, "decision_id": decision_id, "outcomes": len(prev)}
        finally:
            conn.close()


# ── Router ──────────────────────────────────────────────────────────────
class ToolRouter:
    """Unified tool execution router.

    Usage:
        router = ToolRouter()
        result = router.execute("setblock", {"x": 10, "y": 64, "z": -5, "block": "stone"},
                                source="external_openai")
        # → {"status": "ok", "result": {...}, "memory_id": "..."}
    """

    _initialized = False
    _lock = threading.Lock()

    def __init__(self):
        if not ToolRouter._initialized:
            with ToolRouter._lock:
                if not ToolRouter._initialized:
                    bind_executors()
                    ToolRouter._initialized = True

    def execute(self, name: str, args: dict, source: str) -> dict:
        """Execute a tool by name. Returns a structured result.

        Args:
            name: tool name (e.g. "setblock")
            args: tool arguments dict
            source: who triggered this call (for memory attribution)

        Returns:
            {"status": "ok"|"error", "result"|"error": ..., "memory_id": str}
        """
        if source not in ("internal_l10", "external_openai", "evolution_l5_l9"):
            return {
                "status": "error",
                "error": f"invalid source: {source!r}",
                "memory_id": None,
            }

        tool = get_action_tool(name)
        if tool is None:
            err = f"unknown tool: {name!r}"
            memory_id = _inject_to_memory(name, args, {"status": "error"}, source, error=err)
            return {"status": "error", "error": err, "memory_id": memory_id}

        executor = tool.executor
        if executor is None:
            err = f"tool {name!r} has no executor bound"
            memory_id = _inject_to_memory(name, args, {"status": "error"}, source, error=err)
            return {"status": "error", "error": err, "memory_id": memory_id}

        # Execute
        try:
            result = executor(**args)
            if not isinstance(result, dict):
                result = {"value": result}
            if "status" not in result:
                result["status"] = "ok"
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            memory_id = _inject_to_memory(name, args, {"status": "error"}, source, error=err_msg)
            return {"status": "error", "error": err_msg, "memory_id": memory_id}

        # Normalize in-band failure (audit 2026-09-05 finding C): executors may
        # report failure via status/ok/success/exit_code without raising.
        # Outer status must reflect it — no more error-wrapped-as-ok.
        inner = str(result.get("status", "")).lower()
        failed = (
            inner in ("error", "failed", "failure", "timeout")
            or result.get("ok") is False
            or result.get("success") is False
            or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0)
        )
        if failed:
            err_msg = str(result.get("error") or f"executor reported failure (status={result.get('status')!r})")
            result["status"] = "error"
            memory_id = _inject_to_memory(name, args, result, source, error=err_msg)
            return {"status": "error", "error": err_msg, "result": result, "memory_id": memory_id}

        # Inject success to memory
        memory_id = _inject_to_memory(name, args, result, source, error=None)
        return {"status": "ok", "result": result, "memory_id": memory_id}

    def list_available_tools(self) -> list[dict]:
        """List all action tools in OpenAI schema format."""
        from action_tools import list_action_tools
        return list_action_tools()


# ── Singleton ──────────────────────────────────────────────────────────
_router_instance: Optional[ToolRouter] = None
_singleton_lock = threading.Lock()


def get_router() -> ToolRouter:
    """Get singleton router instance."""
    global _router_instance
    if _router_instance is None:
        with _singleton_lock:
            if _router_instance is None:
                _router_instance = ToolRouter()
    return _router_instance

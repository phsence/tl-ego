#!/usr/bin/env python3
"""P1: Executor registry — binds concrete Python callables to action tools.

Each executor signature: async or sync def executor(**kwargs) -> dict

Executors are PURE functions: they call external systems (numen, L4 DB, TTS)
and return {"status": "ok"|"error", "result"/"error": ...}.

They do NOT inject memory — that's tool_router's job (separation of concerns).

P1 ships STUB executors that return realistic mock responses. P2/P3 will
swap in real implementations one by one as we connect to live systems.
"""
import os
from typing import Callable
from action_tools import ACTION_TOOLS, ActionTool


# ── STUB executors (P1) — return realistic mock responses ──────────────
# These prove the routing pipeline works end-to-end. P2/P3 will swap them
# for real implementations.

def _stub_recall_memory(query: str, limit: int = 5) -> dict:
    """Stub: pretends to recall from L4."""
    return {
        "status": "ok",
        "matches": [
            {"ts": "2026-08-20T10:00:00", "content": f"[stub] memory matching '{query}' #{i}", "score": 0.9 - i * 0.1}
            for i in range(min(limit, 3))
        ],
        "total": min(limit, 3),
    }


def _stub_remember(content: str, tags: list = None, importance: float = 0.5) -> dict:
    """Stub: pretends to write to L4."""
    return {
        "status": "ok",
        "memory_id": f"mem_stub_{hash(content) % 10000:04d}",
        "stored_at": "2026-08-20T12:00:00",
    }


def _stub_set_goal(goal: str, priority: str = "medium", horizon: str = "medium") -> dict:
    """Stub: pretends to set goal."""
    return {
        "status": "ok",
        "goal_id": f"goal_stub_{hash(goal) % 1000:03d}",
        "goal": goal,
        "priority": priority,
        "horizon": horizon,
    }


def _stub_check_status() -> dict:
    """Stub: pretends to query /status."""
    return {
        "status": "ok",
        "tick": 100,
        "dominant": "integrator",
        "neurons": 10000000,
        "edges": 200000,
    }


def _stub_tts_speak(text: str, speed: float = 1.0) -> dict:
    """Stub: pretends to speak."""
    return {
        "status": "ok",
        "spoken": text,
        "duration_estimate": len(text) * 0.05,  # ~50ms per char
    }


def _stub_setblock(x: int, y: int, z: int, block: str) -> dict:
    """Stub: pretends to place block."""
    return {
        "status": "ok",
        "placed": f"{block} @ ({x},{y},{z})",
        "world": "minecraft",
    }


def _stub_move(x: int, y: int, z: int) -> dict:
    """Stub: pretends to pathfind + move."""
    return {
        "status": "ok",
        "destination": (x, y, z),
        "path_length": 42,  # stub
        "eta_seconds": 8,
    }


def _stub_chat(message: str) -> dict:
    """Stub: pretends to chat in world."""
    return {
        "status": "ok",
        "channel": "world",
        "message": message,
    }


def _stub_look_at(target: str) -> dict:
    """Stub: pretends to turn camera."""
    return {
        "status": "ok",
        "looking_at": target,
        "pan": 0,
        "tilt": 0,
    }


def _stub_nod() -> dict:
    return {"status": "ok", "action": "nod", "duration_ms": 800}


def _stub_shake_head() -> dict:
    return {"status": "ok", "action": "shake_head", "duration_ms": 800}


def _stub_capture_photo(filename: str = None) -> dict:
    return {
        "status": "ok",
        "saved_to": filename or "auto_named.jpg",
        "size_bytes": 124000,
    }


# ── REAL executor: video generation (ego writes the script, forge renders it) ──
_PROJ = os.path.dirname(os.path.abspath(__file__))
_VIDEO_DIR = os.path.join(_PROJ, "videos")


def _make_video(title: str = "", outro: str = "", scenes: list = None,
                voice: str = None, size: list = None, fps: int = None) -> dict:
    """Render a narrated short video from ego-authored lines. Bounded + measured.

    Untrusted input is clamped here (scenes/lines/chars/size/fps), the spec goes to
    disk as JSON and the forge runs as a subprocess with a hard timeout, so a bad
    goal can never wedge the ego service.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import tempfile as _tf
    import time as _t

    if not isinstance(scenes, list) or not scenes:
        return {"status": "error", "error": "scenes must be a non-empty list"}
    clean = []
    total_lines = 0
    for sc in scenes[:12]:
        if not isinstance(sc, dict):
            continue
        lines = [str(x).strip()[:160] for x in (sc.get("lines") or []) if str(x).strip()][:6]
        if not lines or total_lines >= 40:
            continue
        total_lines += len(lines)
        item = {"lines": lines}
        if sc.get("caption"):
            item["caption"] = str(sc["caption"]).strip()[:40]
        if sc.get("photo"):
            p = str(sc["photo"]).strip()
            cand = p if os.path.isabs(p) else os.path.join(_PROJ, p)
            if os.path.exists(cand):
                item["photo"] = os.path.abspath(cand)
        clean.append(item)
    if not clean:
        return {"status": "error", "error": "no usable scene (each scene needs >=1 non-empty line)"}

    w, h = 1280, 720
    if isinstance(size, (list, tuple)) and len(size) == 2:
        try:
            w, h = int(size[0]), int(size[1])
        except Exception:
            pass
    w, h = max(320, min(1920, w)), max(320, min(1920, h))

    spec = {"scenes": clean, "size": [w, h], "fps": max(10, min(60, int(fps or 24)))}
    if title:
        spec["title"] = str(title).strip()[:24]
    if outro:
        spec["outro"] = str(outro).strip()[:24]
    if voice:
        spec["voice"] = str(voice).strip()[:64]

    os.makedirs(_VIDEO_DIR, exist_ok=True)
    stamp = _t.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(_VIDEO_DIR, f"ego_{stamp}.mp4")
    tmpd = _tf.mkdtemp(prefix="egospec_")
    spec_path = os.path.join(tmpd, "spec.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        _json.dump(spec, f, ensure_ascii=False)

    try:
        cp = _sp.run([_sys.executable, os.path.join(_PROJ, "ego_video_forge.py"),
                      "--spec", spec_path, "--out", out],
                     capture_output=True, text=True, timeout=900, cwd=_PROJ)
    except _sp.TimeoutExpired:
        return {"status": "error", "error": "forge timed out after 900s", "artifact": out}
    finally:
        try:
            import shutil as _sh
            _sh.rmtree(tmpd, ignore_errors=True)
        except Exception:
            pass

    if cp.returncode != 0:
        return {"status": "error", "error": (cp.stderr or cp.stdout or "")[-400:], "exit_code": cp.returncode}
    payload = None
    for ln in reversed((cp.stdout or "").strip().splitlines()):
        try:
            payload = _json.loads(ln)
            break
        except Exception:
            continue
    if not payload or payload.get("status") != "ok":
        return {"status": "error", "error": "forge returned no result", "stdout": (cp.stdout or "")[-300:]}
    payload["spec_summary"] = {"scenes": len(clean), "lines": total_lines, "size": [w, h]}
    return payload


# ── Bind executors to action tools ─────────────────────────────────────
EXECUTOR_MAP: dict[str, Callable] = {
    "recall_memory": _stub_recall_memory,
    "remember": _stub_remember,
    "set_goal": _stub_set_goal,
    "check_status": _stub_check_status,
    "tts_speak": _stub_tts_speak,
    "setblock": _stub_setblock,
    "move": _stub_move,
    "chat": _stub_chat,
    "look_at": _stub_look_at,
    "nod": _stub_nod,
    "shake_head": _stub_shake_head,
    "capture_photo": _stub_capture_photo,
    "make_video": _make_video,
}


def bind_executors() -> None:
    """Attach executor callable to each ACTION_TOOL instance."""
    bound = 0
    for tool in ACTION_TOOLS:
        if tool.name in EXECUTOR_MAP:
            # Don't overwrite real executors installed by bridges (P4+).
            if tool.executor is None:
                tool.executor = EXECUTOR_MAP[tool.name]
                bound += 1
    return bound


def get_executor(name: str) -> Callable | None:
    """Look up executor for a tool name."""
    return EXECUTOR_MAP.get(name)

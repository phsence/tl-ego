#!/usr/bin/env python3
"""Cross-process LLM concurrency gate for TL-Ego."""

from __future__ import annotations

import os
import random
import time

import requests


_TOKEN_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".lae", "llm_gate_tokens"
)
_LIMIT = int(os.environ.get("EGO_LLM_CONCURRENCY", "6") or 6)
_MAX_WAIT = float(os.environ.get("EGO_LLM_GATE_MAX_WAIT", "20") or 20)
_STALE_SEC = 60.0
_429_RETRIES = int(os.environ.get("EGO_LLM_429_RETRIES", "3") or 3)


def _ensure_dir() -> None:
    try:
        os.makedirs(_TOKEN_DIR, exist_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        except Exception:
            return False
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _active_tokens() -> list[str]:
    """Return live token paths, reclaiming stale/dead-owner tokens."""
    _ensure_dir()
    out: list[str] = []
    try:
        for filename in os.listdir(_TOKEN_DIR):
            if not filename.endswith(".tok"):
                continue
            path = os.path.join(_TOKEN_DIR, filename)
            try:
                pid = int(filename.split("-")[0])
            except ValueError:
                pid = 0
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                age = _STALE_SEC + 1
            if not _pid_alive(pid) or age > _STALE_SEC:
                try:
                    os.remove(path)
                except OSError:
                    pass
            else:
                out.append(path)
    except OSError:
        pass
    return out


def _acquire(timeout: float | None = None) -> str | None:
    """Block until a global slot is free; return the owned token path."""
    _ensure_dir()
    deadline = time.time() + (timeout if timeout is not None else _MAX_WAIT)
    token = f"{os.getpid()}-{time.time():.3f}-{random.randrange(10**9)}.tok"
    path = os.path.join(_TOKEN_DIR, token)
    while time.time() < deadline:
        if len(_active_tokens()) < _LIMIT:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                return path
            except FileExistsError:
                time.sleep(0.05)
        else:
            time.sleep(0.08 + random.random() * 0.05)
    return None


def _release(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def llm_post_json(url, headers=None, json=None, timeout=120, retries=None):
    """requests.post with the shared concurrency gate and 429 backoff."""
    # 2026-10-30 step-5-preview: 默认深思会耗尽 max_tokens → 正文为空(实测长提示
    # +1024 tokens: finish=length, content=''); 只对 step-5 系列统一压到 low,
    # 在闸门这一个点改, 所有走闸门的调用方(server/agent_loop/tool_caller/shim)都生效.
    if (isinstance(json, dict) and "reasoning" not in json
            and "step-5" in str(json.get("model", ""))):
        json = dict(json, reasoning={"effort": os.environ.get("EGO_LLM_REASONING", "low")})
    retries = _429_RETRIES if retries is None else retries
    token = _acquire()
    try:
        attempt = 0
        while True:
            response = requests.post(url, headers=headers, json=json, timeout=timeout)
            if response.status_code == 429 and attempt < retries:
                wait = (2**attempt) * (0.5 + random.random() * 0.5)
                print(
                    f"[llm-gate] 429 on {url} (attempt {attempt + 1}/{retries}), "
                    f"retry in {wait:.1f}s",
                    flush=True,
                )
                time.sleep(wait)
                attempt += 1
                continue
            return response
    finally:
        _release(token)


def gate_stats() -> dict:
    """Return current token count and configured limit."""
    return {
        "live_tokens": len(_active_tokens()),
        "limit": _LIMIT,
        "token_dir": _TOKEN_DIR,
    }


if __name__ == "__main__":
    print(gate_stats())

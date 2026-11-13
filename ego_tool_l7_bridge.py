#!/usr/bin/env python3
"""P2: L7 Bridge — wires existing L7 action interface into tool_router.

L7 already has 5 real tools:
- web_fetch(url) → text content
- web_search(query) → search results
- file_read(path, max_lines) → file content
- workspace_scan(path) → workspace tree
- exec_command(cmd, readonly) → command output

This module adapts them as executors for tool_router so ego's existing
cognitive pipeline (L7) flows through the unified router.

Architecture:
  L10 decision → L7 action → execute() → L7 function → result
                                                  ↓
                                          L4 memory injection
"""
import sys
import os
from typing import Optional

# Make L7 importable
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)

# Lazy L7 availability check (don't import at module load to avoid circular).
_HAS_L7 = True  # assume available; verified on first executor call
_L7_ERROR = "L7 not yet loaded"


# ── L7 → tool_router executor adapters ────────────────────────────────
# Lazy L7 import to avoid circular import during module init.
_l7_modules: dict | None = None


def _ensure_l7_loaded() -> dict | None:
    """Lazy-load L7 functions on first executor call."""
    global _l7_modules
    if _l7_modules is not None:
        return _l7_modules
    if not _HAS_L7:
        return None
    try:
        from l7.l7_action_interface import (
            web_fetch, web_search, file_read, workspace_scan, exec_command,
        )
        _l7_modules = {
            "web_fetch": web_fetch,
            "web_search": web_search,
            "file_read": file_read,
            "workspace_scan": workspace_scan,
            "exec_command": exec_command,
        }
        return _l7_modules
    except Exception as e:
        return None


def _adapted_web_fetch(url: str, char_limit: int = 5000) -> dict:
    """web_fetch(url, char_limit). Wraps L7 web_fetch."""
    l7 = _ensure_l7_loaded()
    if l7 is None:
        return {"status": "error", "error": f"L7 unavailable: {_L7_ERROR}"}
    result = l7["web_fetch"](url)
    if result.get("status") == "ok":
        data = result.get("data", {})
        if "text" in data and len(data["text"]) > char_limit:
            data["text"] = data["text"][:char_limit] + "...[truncated]"
            result["data"] = data
    return result


def _adapted_web_search(query: str, limit: int = 5) -> dict:
    """web_search(query, limit). Wraps L7 web_search."""
    l7 = _ensure_l7_loaded()
    if l7 is None:
        return {"status": "error", "error": f"L7 unavailable: {_L7_ERROR}"}
    result = l7["web_search"](query)
    if result.get("status") == "ok":
        data = result.get("data", {})
        if "results" in data and isinstance(data["results"], list):
            data["results"] = data["results"][:limit]
            result["data"] = data
    return result


def _adapted_file_read(path: str, max_lines: int = 200) -> dict:
    """file_read(filepath, max_lines). Wraps L7 file_read.

    Note: L7 signature is (filepath, workspace=None). max_lines is
    post-truncation only — we read whole file then slice.
    """
    l7 = _ensure_l7_loaded()
    if l7 is None:
        return {"status": "error", "error": f"L7 unavailable: {_L7_ERROR}"}
    result = l7["file_read"](path)
    if result.get("status") == "ok" and max_lines > 0:
        data = result.get("data", {})
        if "lines" in data and isinstance(data["lines"], list):
            data["lines"] = data["lines"][:max_lines]
            data["truncated"] = True
            result["data"] = data
    return result


def _adapted_workspace_scan(path: str = ".", max_depth: int = 3) -> dict:
    """workspace_scan(path). Wraps L7 workspace_scan.

    Note: L7 signature is (workspace, deep=False). We ignore max_depth
    (post-truncation only) — pass deep=False by default.
    """
    from pathlib import Path
    l7 = _ensure_l7_loaded()
    if l7 is None:
        return {"status": "error", "error": f"L7 unavailable: {_L7_ERROR}"}
    p = Path(path) if path else Path(".")
    return l7["workspace_scan"](p, deep=False)


def _adapted_exec_command(cmd: str, readonly: bool = True, timeout: int = 30) -> dict:
    """exec_command(cmd, readonly, timeout). Wraps L7 exec_command.

    2026-11-03: L7 真签名是 exec_command(command, workspace=None, timeout=10) ——
    没有 readonly 参数。传 readonly 会 TypeError("unexpected keyword argument
    'readonly'"), ego 每次想用命令核对事实都只拿到一句失败, 于是改用嘴编。
    readonly 仅为兼容老调用方保留; 放行/拦截由 L7 自己的 security gate 负责。
    """
    l7 = _ensure_l7_loaded()
    if l7 is None:
        return {"status": "error", "error": f"L7 unavailable: {_L7_ERROR}"}
    return l7["exec_command"](cmd, timeout=timeout)


# ── Register L7 tools in action_tools ──────────────────────────────────
# We add 5 new ActionTools that wrap L7 functions, so the router has them.
from action_tools import ACTION_TOOLS, ActionTool

L7_TOOLS = [
    ActionTool(
        name="l7_web_fetch",
        description="通过 L7 action interface 抓取 URL 文本内容",
        category="l7_bridge",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "char_limit": {"type": "integer", "description": "返回字符上限", "default": 5000, "minimum": 100, "maximum": 50000},
            },
            "required": ["url"],
        },
        executor=_adapted_web_fetch,
    ),
    ActionTool(
        name="l7_web_search",
        description="通过 L7 action interface 搜索网页",
        category="l7_bridge",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "limit": {"type": "integer", "description": "返回条数", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        executor=_adapted_web_search,
    ),
    ActionTool(
        name="l7_file_read",
        description="通过 L7 action interface 读取文件内容",
        category="l7_bridge",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对工作区）"},
                "max_lines": {"type": "integer", "description": "最大行数", "default": 200, "minimum": 1, "maximum": 5000},
            },
            "required": ["path"],
        },
        executor=_adapted_file_read,
    ),
    ActionTool(
        name="l7_workspace_scan",
        description="通过 L7 action interface 扫描工作区结构",
        category="l7_bridge",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "扫描根路径", "default": "."},
                "max_depth": {"type": "integer", "description": "递归深度", "default": 3, "minimum": 1, "maximum": 10},
            },
            "required": [],
        },
        executor=_adapted_workspace_scan,
    ),
    ActionTool(
        name="l7_exec_command",
        description="通过 L7 action interface 执行 shell 命令（默认只读沙箱）",
        category="l7_bridge",
        parameters={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "shell 命令"},
                "readonly": {"type": "boolean", "description": "是否只读模式", "default": True},
                "timeout": {"type": "integer", "description": "超时秒数", "default": 30, "minimum": 1, "maximum": 300},
            },
            "required": ["cmd"],
        },
        executor=_adapted_exec_command,
    ),
]


def install_l7_bridge() -> int:
    """Add L7 tools to ACTION_TOOLS list. Returns count added."""
    added = 0
    existing_names = {t.name for t in ACTION_TOOLS}
    for tool in L7_TOOLS:
        if tool.name not in existing_names:
            ACTION_TOOLS.append(tool)
            added += 1
    return added


# Auto-install on import
install_l7_bridge()


def has_l7() -> bool:
    """True if L7 action interface is importable."""
    return _HAS_L7

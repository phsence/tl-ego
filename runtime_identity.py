"""Boot-stable runtime identity for TL-Ego causal attribution."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = "ego_runtime_identity.v1"
_RELATIVE_PATH = Path(".lae") / "runtime_identity.json"


class RuntimeIdentityMismatch(ValueError):
    """Raised when evidence belongs to another TL-Ego runtime generation."""


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _generation_timestamp(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit()) or "unknown"


def create_runtime_identity(
    *,
    workspace: str | Path,
    server_path: str | Path,
    pid: int | None = None,
    parent_pid: int | None = None,
    python_executable: str | None = None,
    boot_timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Create and atomically persist one identity for the current server boot."""
    workspace_path = Path(workspace).resolve()
    server_file = Path(server_path).resolve()
    process_id = int(os.getpid() if pid is None else pid)
    parent_process_id = int(os.getppid() if parent_pid is None else parent_pid)
    timestamp = boot_timestamp or datetime.now(timezone.utc).astimezone().isoformat()
    generation_nonce = nonce or secrets.token_hex(8)
    generation_id = (
        f"ego-{_generation_timestamp(timestamp)}-{process_id}-"
        f"{hashlib.sha256(generation_nonce.encode('utf-8')).hexdigest()[:12]}"
    )
    identity = {
        "schema": _SCHEMA,
        "runtime_generation_id": generation_id,
        "boot_timestamp": timestamp,
        "pid": process_id,
        "parent_pid": parent_process_id,
        "python_executable": str(python_executable or sys.executable),
        "workspace": str(workspace_path),
        "server_path": str(server_file),
        "server_sha256": _file_sha256(server_file),
    }

    path = workspace_path / _RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{process_id}.tmp")
    temporary.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return identity


def load_runtime_identity(workspace: str | Path) -> dict[str, Any] | None:
    path = Path(workspace).resolve() / _RELATIVE_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
        return None
    return value


def generation_id(identity: dict[str, Any] | None) -> str | None:
    value = (identity or {}).get("runtime_generation_id")
    return str(value) if value else None


def require_same_generation(
    record: dict[str, Any],
    *,
    expected_generation_id: str,
) -> dict[str, Any]:
    actual = record.get("runtime_generation_id") if isinstance(record, dict) else None
    if not actual or actual != expected_generation_id:
        raise RuntimeIdentityMismatch(
            "cross_generation_rejected: "
            f"expected={expected_generation_id!r}, actual={actual!r}"
        )
    return record

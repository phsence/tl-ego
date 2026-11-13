#!/usr/bin/env python3
"""P5 tests: ego_memory_api (self-cognition query endpoint)."""
import json
import sys
import os
import time
import threading
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Pre-import bridges and trigger some executions for log to be non-empty
from tool_router import get_router, get_memory_log, clear_memory_log
import ego_memory_api as mem_api

# Setup: clear log, run a few executions
clear_memory_log()
r = get_router()
r.execute("setblock", {"x": 1, "y": 2, "z": 3, "block": "stone"}, source="internal_l10")
r.execute("chat", {"message": "hi"}, source="external_openai")
r.execute("nod", {}, source="evolution_l5_l9")

# Start memory API server on test port
HOST = "127.0.0.1"
PORT = 8773

_server = mem_api.HTTPServer((HOST, PORT), mem_api.MemoryHandler)
_thread = threading.Thread(target=_server.serve_forever, daemon=True)
_thread.start()
time.sleep(0.5)


def _get(path: str):
    req = urlopen(f"http://{HOST}:{PORT}{path}", timeout=5)
    return req.status, json.loads(req.read())


def test_health():
    status, body = _get("/health")
    assert status == 200
    assert body["status"] == "ok"


def test_list_all_executions():
    status, body = _get("/v1/memory/tool_executions")
    assert status == 200
    assert body["count"] >= 3
    assert all("memory_id" in rec for rec in body["data"])
    assert all("source" in rec for rec in body["data"])


def test_filter_by_source():
    status, body = _get("/v1/memory/tool_executions?source=internal_l10")
    assert status == 200
    assert all(rec["source"] == "internal_l10" for rec in body["data"])
    assert body["source_filter"] == "internal_l10"


def test_summary():
    status, body = _get("/v1/memory/summary")
    assert status == 200
    assert body["total"] >= 3
    assert "by_tool" in body
    assert "by_source" in body
    assert "internal_l10" in body["by_source"]
    assert "external_openai" in body["by_source"]
    assert "evolution_l5_l9" in body["by_source"]


def test_limit():
    status, body = _get("/v1/memory/tool_executions?limit=1")
    assert status == 200
    assert len(body["data"]) <= 1


def test_404():
    try:
        _get("/v1/nonexistent")
    except HTTPError as e:
        assert e.code == 404
        return
    raise AssertionError("expected 404")


def run_all():
    tests = [
        test_health,
        test_list_all_executions,
        test_filter_by_source,
        test_summary,
        test_limit,
        test_404,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    _server.shutdown()
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)

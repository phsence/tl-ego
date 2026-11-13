#!/usr/bin/env python3
"""P5: Self-cognition query endpoint — ego can ask 'what did I do today?'

Reads from tool_router's in-memory log and exposes:
- GET /v1/memory/tool_executions          → all recent executions
- GET /v1/memory/tool_executions?source=X → filter by source
- GET /v1/memory/summary                  → stats by tool/source

This is part of the unified tool_router memory injection (P1).

In production this would be backed by the real L4 memory store; for now
it reads from the in-memory log.
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from tool_router import get_router, get_memory_log


SHIM_VERSION = "0.1.0"


def _summary(log: list) -> dict:
    """Build summary stats from memory log."""
    by_tool: dict[str, int] = {}
    by_source: dict[str, int] = {}
    errors = 0
    for rec in log:
        by_tool[rec["tool"]] = by_tool.get(rec["tool"], 0) + 1
        by_source[rec["source"]] = by_source.get(rec["source"], 0) + 1
        if rec.get("error"):
            errors += 1
    return {
        "total": len(log),
        "errors": errors,
        "by_tool": by_tool,
        "by_source": by_source,
        "first_ts": log[0]["iso_ts"] if log else None,
        "last_ts": log[-1]["iso_ts"] if log else None,
    }


class MemoryHandler(BaseHTTPRequestHandler):
    """HTTP handler for ego's self-cognition memory queries."""

    def log_message(self, fmt, *args):
        """Suppress default access log."""
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._json(200, {"status": "ok", "service": "ego-memory-api", "version": SHIM_VERSION})
        elif parsed.path == "/v1/memory/tool_executions":
            source = qs.get("source", [None])[0]
            limit = int(qs.get("limit", ["100"])[0])
            log = get_memory_log(source=source, limit=limit)
            self._json(200, {
                "object": "list",
                "count": len(log),
                "source_filter": source,
                "data": log,
            })
        elif parsed.path == "/v1/memory/summary":
            source = qs.get("source", [None])[0]
            log = get_memory_log(source=source, limit=10000)
            self._json(200, _summary(log))
        else:
            self._json(404, {"error": f"not found: {parsed.path}"})

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(host: str = "127.0.0.1", port: int = 8772):
    """Start the memory API server."""
    get_router()  # pre-warm
    server = HTTPServer((host, port), MemoryHandler)
    print(f"[ego-memory-api] v{SHIM_VERSION} listening on http://{host}:{port}")
    print(f"  GET /v1/memory/tool_executions?source=internal_l10&limit=50")
    print(f"  GET /v1/memory/summary")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ego-memory-api] shutting down")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8772)
    args = parser.parse_args()
    run_server(args.host, args.port)

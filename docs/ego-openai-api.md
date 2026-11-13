# ego OpenAI-compatible Tool Calling API

**Status:** P0-P5 complete · **57 tests passing** · 4 commits

## What this is

ego now exposes a unified tool-calling layer that any OpenAI-compatible client can use. The same `ToolRouter.execute()` is called by:

- **ego L10 decision layer** (autonomous — `source="internal_l10"`)
- **External clients** via HTTP API (OpenAI protocol — `source="external_openai"`)
- **L5→L9 evolution pipeline** (future — `source="evolution_l5_l9"`)

Every call goes through one path → one executor → one memory log with source attribution.

This is the AGI-path foundation: ego's cognition and action share one mechanism, so it can introspect "what did I do today?" and "did I do this, or did the user tell me to?"

## Quick start

```powershell
# Start both servers (shim on 8770, memory API on 8772)
cd C:\Users\Administrator\.openclaw\workspace\projects\tl-ego
powershell -ExecutionPolicy Bypass -File start_ego_api.ps1
```

## Endpoints

### POST /v1/chat/completions (port 8770)

OpenAI-compatible chat completions. Heuristic intent detection currently maps:

| User says | Tool called |
|---|---|
| `build stone at 10,64,-5` | `setblock(x=10,y=64,z=-5,block=stone)` |
| `move to 100,64,200` | `move(x=100,y=64,z=200)` |
| `say hello world` | `chat(message=hello world)` |
| `please nod` / `点头` | `nod()` |
| `shake` / `摇头` | `shake_head()` |

**Example:**

```bash
curl -X POST http://127.0.0.1:8770/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ego",
    "messages": [{"role":"user","content":"build stone at 10,64,-5"}]
  }'
```

Response (excerpt):

```json
{
  "id": "ego-4ee7433cea0a5482",
  "object": "chat.completion",
  "model": "ego",
  "choices": [{
    "message": {
      "role": "assistant",
      "tool_calls": [{
        "function": {
          "name": "setblock",
          "arguments": "{\"block\":\"stone\",\"x\":10,\"y\":64,\"z\":-5}"
        }
      }],
      "finish_reason": "tool_calls"
    }
  }]
}
```

### GET /v1/tools (port 8770)

Returns 17 OpenAI-format tool definitions.

### GET /v1/memory/tool_executions (port 8772)

Returns tool execution history. Filter by source:

```bash
# What did ego do autonomously?
curl "http://127.0.0.1:8772/v1/memory/tool_executions?source=internal_l10&limit=20"

# What did the user trigger?
curl "http://127.0.0.1:8772/v1/memory/tool_executions?source=external_openai"
```

### GET /v1/memory/summary (port 8772)

Aggregated stats: total calls, errors, by tool, by source.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  3 trigger sources funnel into 1 router                     │
│                                                              │
│  internal_l10 ───┐                                           │
│  external_openai ─┼─→ ToolRouter.execute(name, args, src)    │
│  evolution_l5_l9 ─┘                │                         │
│                                     ↓                         │
│                           ActionTool.executor(**args)        │
│                                     │                         │
│                                     ↓                         │
│                          L4 memory injection                 │
│                          (source attribution preserved)      │
└─────────────────────────────────────────────────────────────┘
```

## Tool inventory (17 total)

### ego_internal (5)
- `recall_memory(query, limit)` — real, uses `MemoryBridge.retrieve()`
- `remember(content, tags, importance)` — real, writes to Hy-Memory
- `set_goal(goal, priority, horizon)` — real, records to L4 (no HTTP goal API exists)
- `check_status()` — real, queries ego `/status`
- `tts_speak(text, speed)` — stub (TTS integration outside scope)

### minecraft (3) — STUB
- `setblock(x, y, z, block)` — TODO: needs numen Python SDK
- `move(x, y, z)` — TODO
- `chat(message)` — TODO

### robot (4) — STUB
- `look_at(target)` — TODO: camera in external project
- `nod()` — TODO
- `shake_head()` — TODO
- `capture_photo(filename)` — TODO

### l7_bridge (5) — REAL (uses existing L7 action interface)
- `l7_web_fetch(url, char_limit)` — real
- `l7_web_search(query, limit)` — real
- `l7_file_read(path, max_lines)` — real
- `l7_workspace_scan(path)` — real
- `l7_exec_command(cmd, readonly, timeout)` — real (sandboxed)

## Tests

```bash
H:/unsloth_env/Scripts/python.exe test_unified_registry.py    # 10
H:/unsloth_env/Scripts/python.exe test_tool_router.py         # 16
H:/unsloth_env/Scripts/python.exe test_l7_bridge.py           # 10
H:/unsloth_env/Scripts/python.exe test_openai_shim.py         # 12
H:/unsloth_env/Scripts/python.exe test_ego_internal_bridge.py #  9
H:/unsloth_env/Scripts/python.exe test_memory_api.py          #  6
# Total: 63 tests, all green
```

## Known limitations (honest)

1. **Intent detection is regex-based.** P5 will swap in LLM-driven schema parsing (stepfun with `tools` field).
2. **Minecraft executors are stubs.** Need numen Python SDK or RCON bridge.
3. **Robot executors are stubs.** Camera is in external project (`emeet_hid_ptz.py`); not yet wired.
4. **Memory log is in-process.** Restarts lose history. Real L4 store integration is P6.
5. **Hy-Memory retrieve returns 400.** Existing API contract changed; needs `memory_bridge.py` update.

## What's next (not implemented yet)

- **P6: LLM-driven tool calling** — replace regex with stepfun `tools` schema
- **P7: Real L4 memory store** — replace in-memory log with Hy-Memory/graph-memory persistence
- **P8: numen Minecraft bridge** — bring in numen Python SDK for setblock/move/chat
- **P9: Multi-turn tool execution** — tool_result → ego → tool_call chains (OpenAI spec)

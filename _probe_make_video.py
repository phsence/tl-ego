#!/usr/bin/env python3
"""Probe: is make_video reachable through ego's own ToolRouter (registry + executor + memory)? """
import json
import tool_router

r = tool_router.get_router()
names = [t["function"]["name"] for t in r.list_available_tools()]
print("tools", len(names), "has_make_video", "make_video" in names)

res = r.execute(
    "make_video",
    {
        "title": "router 通路验证",
        "outro": "通了。",
        "scenes": [
            {"caption": "通路测试", "lines": ["这条走的是 ego 自己的 ToolRouter。"]},
        ],
        "size": [640, 360],
        "fps": 20,
    },
    source="evolution_l5_l9",
)
out = {
    "status": res.get("status"),
    "artifact": (res.get("result") or {}).get("artifact"),
    "duration": (res.get("result") or {}).get("duration"),
    "bytes": (res.get("result") or {}).get("bytes"),
    "memory_id": bool(res.get("memory_id")),
    "error": res.get("error"),
}
print(json.dumps(out, ensure_ascii=False))

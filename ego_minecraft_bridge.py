#!/usr/bin/env python3
"""ego_minecraft_bridge.py — real Minecraft executors for tool_router.

Replaces the fake-success stubs (executor_registry._stub_setblock/_stub_move/
_stub_chat) with real calls into the numen-viewer control plane on port 3007:

  POST-free GET API (numen-viewer.js):
    /ego                          → bot state (pos, heldItem, chat log)
    /action?act=goto&x=&y=&z=     → numen goto <who> x y z   (walk)
    /action?act=mine&block=       → numen mine <who> block
    /action?act=chat&message=     → say [observer->ego] msg (world chat)
    /action?act=cancel            → numen cancel
    /numen?cmd=<rcon cmd>         → raw RCON (server-side safe-filtered)
    /scan?x=&y=&z=&r=             → blocks around a position
    /view/focus?name=             → camera focus (observer-side)

Tools wired (robot/camera tools stay stubs by user decision):
  setblock → /numen?cmd=setblock x y z block      (server console)
  move     → /action?act=goto
  chat     → /action?act=chat
  look_at  → /view/focus?name=<target>            (observer camera)
  mc_world_state (NEW) → /ego (+ /scan around bot)

Every executor verifies liveness first: if 3007 is down or the Fabric bot
is offline it returns status=error — NEVER a fake success. This closes the
"stub absorbed the call, LLM believes it worked" failure mode.
"""
import json
import os
import urllib.parse
import urllib.request

MC_BASE = os.environ.get("EGO_MC_VIEWER_URL", "http://127.0.0.1:3007")
_TIMEOUT = float(os.environ.get("EGO_MC_TIMEOUT", "12"))


def _get(path: str, params: dict | None = None) -> dict:
    """GET {MC_BASE}{path}?params → parsed JSON dict. Raises on HTTP/network error."""
    url = MC_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _mc_alive() -> tuple[bool, str]:
    try:
        d = _get("/ego")
        ego = d.get("ego") or {}
        online = bool(ego.get("online")) or bool(d.get("botOnline"))
        if not online:
            return False, "fabric bot 离线（3007 可达但 ego 假玩家不在线）"
        return True, d
    except Exception as e:
        return False, f"numen-viewer 不可达 ({MC_BASE}): {type(e).__name__}: {e}"


def _real_mc_setblock(x: int, y: int, z: int, block: str) -> dict:
    ok, info = _mc_alive()
    if not ok:
        return {"status": "error", "error": f"setblock 失败: {info}"}
    block = str(block).strip().replace("minecraft:", "")
    # server-side safe filter allows [a-zA-Z0-9 _\-.,:] — plain ids pass
    r = _get("/numen", {"cmd": f"setblock {int(x)} {int(y)} {int(z)} {block}"})
    placed_ok = bool(r.get("ok"))
    return {
        "status": "ok" if placed_ok else "error",
        "placed": f"{block} @ ({x},{y},{z})",
        "server_reply": (r.get("result") or r.get("error") or "")[:200],
        "error": None if placed_ok else f"RCON 拒绝: {r.get('result') or r.get('error')}",
    }


def _real_mc_move(x: int, y: int, z: int) -> dict:
    ok, info = _mc_alive()
    if not ok:
        return {"status": "error", "error": f"move 失败: {info}"}
    # 2026-09-11: y 幻觉钳制 — ego 世界模型的 y 常是模拟值(实测 320/323, 真实地面 139),
    # 错的 y 直接 NO-PATH(服务端日志: goal=432,323,324 no path to target), 身体永远走不到,
    # 战斗永远够不着。水平移动的 y 交给寻路; 目标 y 超出身体高度 ±32 就用真实高度。
    try:
        real_y = float(((info.get("ego") or {}).get("pos") or {}).get("y"))
        if not (real_y - 32 <= y <= real_y + 32):
            y = int(round(real_y))
    except (TypeError, ValueError, AttributeError):
        pass
    r = _get("/action", {"act": "goto", "x": int(x), "y": int(y), "z": int(z)})
    # l5g-20261101-063707 world-acceptance classifier: the viewer's ok=true is
    # transport-level only. The world refuses the command itself with
    # "Incorrect argument for command" in server_reply (measured 2026-11-01 on
    # the durable move log: 1885 of 1908 transport-ok rows were world-refused,
    # only 21 accepted of 6461). A refusal must not be recorded as success, or
    # action_outcomes believes the body moved when the world never accepted.
    _mv_reply = (r.get("result") or "")[:200]
    # l5g-20261101-063707 world-acceptance classifier: the viewer's ok=true is
    # transport-level only. The world refuses the command itself with
    # "Incorrect argument for command" in server_reply (measured 2026-11-01 on
    # the durable move log: 1885 of 1908 transport-ok rows were world-refused,
    # only 21 accepted of 6461). A refusal must not be recorded as success, or
    # action_outcomes believes the body moved when the world never accepted.
    _mv_reply = (r.get("result") or "")[:200]
    # l5g-20261107-067247: refusal-marker coverage gap. The 2026-11-01
    # classifier only knew "Incorrect argument"; the numen bridge also refuses
    # with "没有名为 '<who>' 的同伴" (measured 2026-11-07: 15/15 such rows carry
    # status=ok, 13 of them today, 0 accepted). A refusal is not a success.
    moved = (bool(r.get("ok"))
             and "Incorrect argument" not in _mv_reply
             and "没有名为" not in _mv_reply)
    return {
        "status": "ok" if moved else "error",
        "destination": [int(x), int(y), int(z)],
        "command": r.get("cmd"),
        "server_reply": (r.get("result") or "")[:200],
        "error": None if moved else f"goto 被拒绝: {r.get('result')}",
    }


def _real_mc_chat(message: str) -> dict:
    ok, info = _mc_alive()
    if not ok:
        return {"status": "error", "error": f"chat 失败: {info}"}
    msg = str(message)[:60]  # viewer enforces 60 chars anyway
    r = _get("/action", {"act": "chat", "message": msg})
    sent = bool(r.get("ok"))
    return {
        "status": "ok" if sent else "error",
        "channel": "world",
        "message": msg,
        "server_reply": (r.get("result") or "")[:200],
        "error": None if sent else f"say 被拒绝: {r.get('result')}",
    }


def _coord_like(t: str) -> bool:
    """'415,35,1093' / '(410.5 35.0 -1)' → True; an entity name → False."""
    parts = t.replace("(", " ").replace(")", " ").replace(",", " ").split()
    if len(parts) != 3:
        return False
    try:
        [float(p) for p in parts]
    except ValueError:
        return False
    return True


def _real_mc_look_at(target: str) -> dict:
    # Observer-side camera focus — a NAME-only API (numen-viewer /view/focus).
    # 2026-09-17 实测: 历史 304 次调用里 299 次传的是 'x,y,z' 坐标 → 全失败
    # (「看不到 415,35,1093（不在观察者视距内或未上线）」)。坐标永远匹配不到
    # 同伴名，所以直接给 ego 一个能干这事的工具，别让它每轮白烧一次决策。
    t = str(target).strip()
    if _coord_like(t):
        return {
            "status": "error",
            "looking_at": t,
            "error": "MC 里 look_at 只认实体名；想看某坐标处有什么，用 look_around / scan_blocks / inspect_block",
        }
    try:
        r = _get("/view/focus", {"name": t})
        ok = bool(r.get("ok"))
        return {
            "status": "ok" if ok else "error",
            "looking_at": t,
            "error": None if ok else r.get("error", "focus failed"),
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _real_mc_world_state() -> dict:
    ok, info = _mc_alive()
    if not ok:
        return {"status": "error", "error": info}
    d = info if isinstance(info, dict) else {}
    ego = d.get("ego") or {}
    # 2026-09-23 修: viewer 的 egoCache 在视距外走 RCON 兜底,只查
    # `data get entity Ego Pos` → health/heldItem/onGround 全是 null。
    # ego 调 mc_world_state 问身体状态,拿到"我没血、我没手"。
    # 直接复用 numen MCP 的 get_self_status 补齐(现成执行器,不重造轮子)。
    if ego.get("health") is None or ego.get("heldItem") is None:
        try:
            import ego_mc_body_bridge as _body
            ss = _body.mc_get_self_status().get("result") or {}
            if isinstance(ss, dict):
                ego = {
                    **ego,
                    # numen MCP 用 hp/max_hp,viewer/ego 侧约定 health —
                    # 键名不同,直接赋值会静默丢成 None(实测踩过)
                    "health": ss.get("hp"),
                    "maxHealth": ss.get("max_hp"),
                    "hunger": ss.get("hunger"),
                    "onGround": ss.get("on_ground"),
                    "heldItem": (ss.get("equipment") or {}).get("mainhand"),
                    "self_status_source": "numen_mcp",
                }
        except Exception as e:
            ego = {**ego, "self_status_error": f"{type(e).__name__}: {e}"}
    state = {
        "status": "ok",
        "bot_online": True,
        "position": ego.get("pos"),
        "health": ego.get("health"),
        "max_health": ego.get("maxHealth"),
        "hunger": ego.get("hunger"),
        "held_item": ego.get("heldItem"),
        "on_ground": ego.get("onGround"),
        "recent_chat": [
            {"who": c.get("who"), "msg": str(c.get("msg", ""))[:120]}
            for c in (d.get("chat") or [])[-5:]
        ],
        "rcon_authed": d.get("rconAuthed"),
        "source": f"GET {MC_BASE}/ego",
    }
    # best-effort block scan around the bot (non-fatal)
    # 2026-09-23 修: 原来 except 一律写成 None,ego 拿到
    # "nearby_blocks": null 完全看不出是 viewer 挂了还是自己没 spawn —
    # 等于告诉 ego"世界是空的"。现在把原因带回去,至少能自诊断。
    pos = ego.get("pos") or {}
    try:
        scan = _get("/scan", {
            "x": int(pos.get("x", 0)), "y": int(pos.get("y", 0)),
            "z": int(pos.get("z", 0)), "r": 2,
        })
        if scan.get("ok") and scan.get("blocks") is not None:
            blocks = scan["blocks"]
            # 80 条上限的立方体罗列对 ego 没有信息量;按种类汇总成可读摘要
            counts = {}
            for b in blocks:
                counts[b.get("name", "?")] = counts.get(b.get("name", "?"), 0) + 1
            state["nearby_blocks"] = {
                "ok": True,
                "count": len(blocks),
                "by_type": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            }
        else:
            state["nearby_blocks"] = {
                "ok": False,
                "error": scan.get("error") or "scan returned no blocks",
            }
    except Exception as e:
        state["nearby_blocks"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return state


def install_minecraft_bridge() -> int:
    """Replace stub executors for the 4 MC tools; append mc_world_state."""
    from action_tools import ACTION_TOOLS, ActionTool

    upgrades = {
        "setblock": _real_mc_setblock,
        "move": _real_mc_move,
        "chat": _real_mc_chat,
        "look_at": _real_mc_look_at,
    }
    count = 0
    for tool in ACTION_TOOLS:
        if tool.name in upgrades:
            tool.executor = upgrades[tool.name]
            tool.category = "minecraft_real"
            count += 1

    names = {t.name for t in ACTION_TOOLS}
    if "mc_world_state" not in names:
        ACTION_TOOLS.append(ActionTool(
            name="mc_world_state",
            description="查询 ego 在 Minecraft 世界中的实时状态（位置/手持/附近方块/最近聊天）",
            category="minecraft_real",
            parameters={"type": "object", "properties": {}, "required": []},
            executor=_real_mc_world_state,
        ))
        count += 1
    return count


if __name__ == "__main__":
    assert _coord_like("415,35,1093") and _coord_like("(410.5 35.0 -1)")
    assert not _coord_like("pillager") and not _coord_like("EgoOwner") and not _coord_like("x,y,z")
    print("_coord_like self-check OK")


# Auto-install on import (same pattern as ego_internal_bridge / l7 bridge)
install_minecraft_bridge()

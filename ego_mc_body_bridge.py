#!/usr/bin/env python3
"""MC 身体工具桥：把 numen MCP (8765) 的 38 工具暴露给 ego 的 8770 工具面。

每个 ego_action 工具执行时，转发到 numen MCP 的同名工具（companion=ego）。
2026-09-07 指挥官指令：8770 工具面补全 MC 身体能力（attack/scan/goto/follow 等），
让 ego 大脑能直接发起身体动作，不再依赖 tl-ego-bridge 的二次路由。
"""
import json
import os
import urllib.error
import urllib.request
from typing import Callable

MCP_URL = "http://127.0.0.1:8765/mcp"
EGO_NAME = "ego"

_MCP_TOOLS_CACHE = None

# 2026-09-17: numen MCP 现在要 Bearer token，不带 = 每个工具调用 401 Unauthorized
# （实测 internal_l10 的 look_around/mc_follow/mc_get_self_status 全 401）。token 从活跃
# 客户端的 config/numen/mcp_server.json 读；换客户端/换 token 就改这两条路径或设 NUMEN_MCP_CFG。
_TOKEN_PATHS = [
    os.environ.get("NUMEN_MCP_CFG", ""),
    r"C:\tools\prom-client\config\numen\mcp_server.json",
    r"C:\tools\mc-client\config\numen\mcp_server.json",
]


def _read_mcp_token() -> str:
    for p in _TOKEN_PATHS:
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return str(json.load(f).get("token") or "")
            except Exception:
                continue
    return ""


_MCP_TOKEN = _read_mcp_token()


def _mcp_call(name: str, args: dict, timeout: float = 55.0) -> dict:
    """Call a numen MCP tool, return parsed result text (or error dict)."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 901,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }).encode()
    headers = {"Content-Type": "application/json"}
    if _MCP_TOKEN:
        headers["Authorization"] = "Bearer " + _MCP_TOKEN
    req = urllib.request.Request(MCP_URL, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            return {"status": "error", "error": str(data["error"])}
        content = data.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""
        # numen 返回 JSON 文本；尝试解析，失败则原样返回
        try:
            inner = json.loads(text)
            return {"status": "ok", "result": inner} if isinstance(inner, dict) else {"status": "ok", "result": text}
        except (json.JSONDecodeError, TypeError):
            return {"status": "ok", "result": text}
    except urllib.error.HTTPError as e:
        # MCP alive but rejecting (401/403...) is a token problem - must not be masked
        return {"status": "error", "error": f"HTTP {e.code} {e.reason}: numen MCP reachable but rejected the call (check _MCP_TOKEN)"}
    except Exception as e:
        # 2026-10-19: numen MCP(8765) transport death != world death - numen-viewer(3007)
        # is still alive (measured /ego /scan RCON). Connection-class errors fall back to
        # the viewer for tools with real equivalent endpoints; others report the dead
        # transport by name. HTTP rejections stay loud above so token bugs are not masked.
        fb = _viewer_fallback(name, args)
        if fb is not None:
            return fb
        return {"status": "error", "error": f"{type(e).__name__}: {e} (numen MCP 8765 unreachable; no viewer fallback for tool {name!r})"}


def _with_companion(args: dict) -> dict:
    args = dict(args or {})
    args.setdefault("companion", EGO_NAME)
    return args

def _viewer_fallback(name: str, args: dict) -> dict | None:
    """On connection-class failure of numen MCP(8765), substitute the real
    equivalent endpoints on numen-viewer(3007).

    Covers only tools measured to have a true readback equivalent:
    get_self_status(/ego), look_around|scan_blocks(/scan), goto(/action via
    ego_minecraft_bridge._real_mc_move - reuse, do not reinvent). Everything
    else returns None = no fallback, caller reports the original error -
    entity tools (scan_nearby_entities etc.) have no viewer endpoint and are
    deliberately NOT faked.
    """
    try:
        import ego_minecraft_bridge as _mv
    except Exception:
        return None
    try:
        if name in ("get_self_status", "mc_get_self_status"):
            ok, info = _mv._mc_alive()
            if not ok:
                return None
            ego = (info.get("ego") or {}) if isinstance(info, dict) else {}
            return {"status": "ok", "result": {
                "name": ego.get("name"), "position": ego.get("pos"),
                "yaw": ego.get("yaw"), "on_ground": ego.get("onGround"),
                "velocity": ego.get("velocity"), "hp": ego.get("health"),
                "note": "viewer fallback: numen MCP 8765 unreachable; hp/heldItem may be null (viewer RCON limit)",
            }, "fallback": "viewer_3007"}
        if name in ("look_around", "mc_look_around", "scan_blocks", "mc_scan_blocks"):
            ok, info = _mv._mc_alive()
            if not ok:
                return None
            pos = ((info.get("ego") or {}).get("pos") or {}) if isinstance(info, dict) else {}
            r = int(args.get("radius") or (8 if "look" in name else 32))
            r = max(1, min(r, 16))
            scan = _mv._get("/scan", {"x": int(pos.get("x", 0)), "y": int(pos.get("y", 0)),
                                      "z": int(pos.get("z", 0)), "r": r})
            if not scan.get("ok") or scan.get("blocks") is None:
                return None
            blocks = scan["blocks"]
            if "scan_blocks" in name:
                want = {str(b).replace("minecraft:", "") for b in (args.get("block_ids") or [])}
                if want:
                    blocks = [b for b in blocks
                              if str(b.get("name", "")).replace("minecraft:", "") in want]
            counts = {}
            for b in blocks:
                counts[b.get("name", "?")] = counts.get(b.get("name", "?"), 0) + 1
            return {"status": "ok", "result": {
                "summary": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
                "total_in_radius": len(blocks), "radius_searched": r,
                "matches": blocks[:50],
                "note": "viewer fallback: numen MCP 8765 unreachable; cube scan r<=16, not the MCP radial scan",
            }, "fallback": "viewer_3007"}
        if name in ("goto", "mc_goto"):
            # reuse the 3007 control plane's real pathfinding (incl. y-clamp)
            return _mv._real_mc_move(int(args.get("x", 0)), int(args.get("y", 0)), int(args.get("z", 0)))
    except Exception:
        return None
    return None


# ── 执行器：名字与 numen MCP 工具一一对应 ──────────────────────────────

def mc_attack(entity_ids: list = None, **kw) -> dict:
    """攻击近身敌对生物。不传 entity_ids = 攻击身边所有敌对目标。"""
    args = {}
    if entity_ids:
        args["entity_ids"] = entity_ids
    return _mcp_call("attack", _with_companion(args))


def mc_scan_nearby_entities(type_filter: str = "hostile", radius: int = 16, **kw) -> dict:
    """扫描附近实体。type_filter: hostile|player|animal|all"""
    return _mcp_call("scan_nearby_entities",
                     _with_companion({"type_filter": type_filter, "radius": radius}))


def mc_goto(x: int, y: int, z: int, **kw) -> dict:
    """走到指定坐标（真实寻路，会搭方块绕障碍）。"""
    return _mcp_call("goto", _with_companion({"x": int(x), "y": int(y), "z": int(z)}))


def mc_follow(target: str = "EgoOwner", **kw) -> dict:
    """跟随指定玩家/实体。"""
    return _mcp_call("follow", _with_companion({"target": target}))


def mc_mine(block: str = "iron_ore", **kw) -> dict:
    """去挖指定方块。"""
    return _mcp_call("mine", _with_companion({"block": block}))


def mc_collect_items(**kw) -> dict:
    """拾取身边掉落物。"""
    return _mcp_call("collect_items", _with_companion({}))


def mc_craft(recipe: str = "", **kw) -> dict:
    """合成物品。"""
    return _mcp_call("craft", _with_companion({"recipe": recipe}))


def mc_eat(item: str = "", **kw) -> dict:
    """进食恢复饥饿/血量。item 空 = 让 MCP 从背包自己挑。"""
    # 2026-09-17: MCP eat 的参数名是 item_id（不是 item）；空串要整字段省略，
    # 否则 MCP 校验吃掉整个调用（实测 schema: {"item_id": "minecraft:cooked_beef"}）
    a = {}
    v = item or kw.get("item_id") or ""
    if v:
        a["item_id"] = v if v.startswith("minecraft:") else "minecraft:" + v
    return _mcp_call("eat", _with_companion(a))


def mc_scan_blocks(block_ids: list = None, radius: int = 32, **kw) -> dict:
    """扫描附近方块（numen scan_blocks 直通；不给名单时给常用矿石/木头兜底）。"""
    ids = block_ids or ["minecraft:coal_ore", "minecraft:iron_ore", "minecraft:copper_ore",
                        "minecraft:gold_ore", "minecraft:diamond_ore", "minecraft:oak_log",
                        "minecraft:stone", "minecraft:water"]
    return _mcp_call("scan_blocks", _with_companion({"block_ids": ids, "radius": int(radius)}))


def mc_equip_item(slot: int = 0, **kw) -> dict:
    """装备背包物品到主手。"""
    return _mcp_call("equip_item", _with_companion({"slot": int(slot)}))


def mc_get_self_status(**kw) -> dict:
    """查询自己身体状态（位置/血量/饥饿/手持）。"""
    return _mcp_call("get_self_status", _with_companion({}))


def mc_get_world_info(**kw) -> dict:
    """查询世界信息（时间/天气/维度）。"""
    return _mcp_call("get_world_info", _with_companion({}))


def mc_look_around(**kw) -> dict:
    """环视周围地形。"""
    return _mcp_call("look_around", _with_companion({}))


def mc_inspect_block(x: int, y: int, z: int, **kw) -> dict:
    """查看指定坐标方块。"""
    return _mcp_call("inspect_block", _with_companion({"x": int(x), "y": int(y), "z": int(z)}))


def mc_task_status(task_id: str = "", **kw) -> dict:
    """查询后台任务状态。"""
    args = {"task_id": task_id} if task_id else {}
    return _mcp_call("task_status", _with_companion(args))


def mc_task_stop(task_id: str = "", **kw) -> dict:
    """叫停后台任务。"""
    args = {"task_id": task_id} if task_id else {}
    return _mcp_call("task_stop", _with_companion(args))


def mc_blueprint(blueprint: str = "", **kw) -> dict:
    """按蓝图建造。"""
    return _mcp_call("blueprint", _with_companion({"blueprint": blueprint}))


def mc_build(ops: list = None, **kw) -> dict:
    """执行方块操作列表。"""
    return _mcp_call("build", _with_companion({"ops": ops or []}))


def mc_fish(**kw) -> dict:
    """钓鱼。"""
    return _mcp_call("fish", _with_companion({}))


def mc_sleep(**kw) -> dict:
    """睡觉。"""
    return _mcp_call("sleep", _with_companion({}))


def mc_drop_items(item: str = "", count: int = 1, **kw) -> dict:
    """丢弃物品。"""
    return _mcp_call("drop_items", _with_companion({"item": item, "count": int(count)}))


def mc_transfer(target: str = "", item: str = "", count: int = 1, **kw) -> dict:
    """把物品转移给指定玩家。"""
    return _mcp_call("transfer", _with_companion({"target": target, "item": item, "count": int(count)}))


def mc_interact_at(x: int, y: int, z: int, **kw) -> dict:
    """右键交互指定坐标（开箱/开门/用工作台）。"""
    return _mcp_call("interact_at", _with_companion({"x": int(x), "y": int(y), "z": int(z)}))


def mc_locate_structure(structure: str = "", **kw) -> dict:
    """定位最近的结构（村庄/要塞等）。"""
    return _mcp_call("locate_structure", _with_companion({"structure": structure}))


def mc_locate_biome(biome: str = "", **kw) -> dict:
    """定位最近的生物群系。"""
    return _mcp_call("locate_biome", _with_companion({"biome": biome}))


def mc_scaffold_materials(items: list = None, **kw) -> dict:
    """设置搭路材料（寻路时可放置的方块）。"""
    return _mcp_call("scaffold_materials", _with_companion({"items": items or ["dirt", "cobblestone"]}))


def install_mc_body_bridge() -> int:
    """把 MC 身体工具注册进 ACTION_TOOLS + executor。返回注册数量。"""
    from action_tools import ACTION_TOOLS, ActionTool, get_action_tool

    MC_TOOLS = [
        ("mc_attack", "攻击近身敌对生物（不传 entity_ids = 攻击身边所有敌对目标）。铁剑/铁甲已装备。",
         {"entity_ids": {"type": "array", "items": {"type": "integer"},
                          "description": "scan_nearby_entities 返回的实体 id；省略=打所有近身敌对"}},
         mc_attack),
        ("mc_scan_nearby_entities", "扫描附近实体（敌对/玩家/动物），返回类型+距离+血量",
         {"type_filter": {"type": "string", "enum": ["hostile", "player", "animal", "all"],
                           "description": "过滤类型", "default": "hostile"},
          "radius": {"type": "integer", "description": "扫描半径(格)", "default": 16}},
         mc_scan_nearby_entities),
        ("scan_nearby_entities", "扫描附近实体（mc_scan_nearby_entities 的无前缀别名，兼容 ego 侧调用名）",
         {"type_filter": {"type": "string", "enum": ["hostile", "player", "animal", "all"],
                           "description": "过滤类型", "default": "hostile"},
          "radius": {"type": "integer", "description": "扫描半径(格)", "default": 16}},
         mc_scan_nearby_entities),
        # 2026-09-17: ego 内部 L10 说的是 numen 原生工具名（实测 914 次 unknown tool：
        # scan_nearby_entities 635 / scan_blocks 101 / attack 69 / get_self_status 38 /
        # eat 37 / look_around 21 / task_status 11）——补上无前缀别名，指向同一批执行器。
        ("scan_blocks", "扫描附近方块（无前缀别名）",
         {"block_ids": {"type": "array", "items": {"type": "string"}, "description": "方块 id 列表"},
          "radius": {"type": "integer", "description": "半径(格)", "default": 32}},
         mc_scan_blocks),
        ("attack", "攻击近身敌对生物（无前缀别名；不传 entity_ids = 打身边所有敌对）",
         {"entity_ids": {"type": "array", "items": {"type": "integer"}}}, mc_attack),
        ("get_self_status", "查询自己身体状态（无前缀别名）", {}, mc_get_self_status),
        ("eat", "进食恢复饥饿/血量（无前缀别名；item 空=背包自己挑）",
         {"item": {"type": "string", "description": "食物名"}, "item_id": {"type": "string"}}, mc_eat),
        ("look_around", "环视周围地形（无前缀别名）",
         {"radius": {"type": "integer", "description": "半径(格)", "default": 8}}, mc_look_around),
        ("task_status", "查询后台任务状态（无前缀别名）",
         {"task_id": {"type": "string"}}, mc_task_status),
        ("interact_at", "右键交互指定坐标（无前缀别名）",
         {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}, mc_interact_at),
        ("mc_goto", "走到指定坐标（真实寻路，可搭方块绕障碍）",
         {"x": {"type": "integer", "description": "x"}, "y": {"type": "integer", "description": "y"},
          "z": {"type": "integer", "description": "z"}}, mc_goto),
        ("mc_follow", "跟随指定玩家（默认指挥官 EgoOwner）",
         {"target": {"type": "string", "description": "玩家名", "default": "EgoOwner"}}, mc_follow),
        ("mc_mine", "去挖指定方块（如 iron_ore/oak_log/stone）",
         {"block": {"type": "string", "description": "方块 ID"}}, mc_mine),
        ("mc_collect_items", "拾取身边掉落物", {}, mc_collect_items),
        ("mc_craft", "合成物品", {"recipe": {"type": "string", "description": "配方名"}}, mc_craft),
        ("mc_eat", "进食恢复饥饿/血量", {"item": {"type": "string", "description": "食物名"}}, mc_eat),
        ("mc_equip_item", "装备背包物品到主手", {"slot": {"type": "integer", "description": "槽位号"}}, mc_equip_item),
        ("mc_get_self_status", "查询自己身体状态（位置/血量/饥饿/手持）", {}, mc_get_self_status),
        ("mc_get_world_info", "查询世界信息（时间/天气/维度）", {}, mc_get_world_info),
        ("mc_look_around", "环视周围地形", {}, mc_look_around),
        ("mc_inspect_block", "查看指定坐标方块",
         {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}, mc_inspect_block),
        ("mc_task_status", "查询后台任务状态", {"task_id": {"type": "string"}}, mc_task_status),
        ("mc_task_stop", "叫停后台任务", {"task_id": {"type": "string"}}, mc_task_stop),
        ("mc_blueprint", "按蓝图建造", {"blueprint": {"type": "string", "description": "蓝图名"}}, mc_blueprint),
        ("mc_fish", "钓鱼", {}, mc_fish),
        ("mc_sleep", "睡觉", {}, mc_sleep),
        ("mc_drop_items", "丢弃物品",
         {"item": {"type": "string"}, "count": {"type": "integer", "default": 1}}, mc_drop_items),
        ("mc_transfer", "把物品转移给指定玩家",
         {"target": {"type": "string"}, "item": {"type": "string"}, "count": {"type": "integer", "default": 1}},
         mc_transfer),
        ("mc_interact_at", "右键交互指定坐标（开箱/开门/用工作台）",
         {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}, mc_interact_at),
        ("mc_locate_structure", "定位最近结构", {"structure": {"type": "string"}}, mc_locate_structure),
        ("mc_locate_biome", "定位最近生物群系", {"biome": {"type": "string"}}, mc_locate_biome),
        ("mc_scaffold_materials", "设置搭路材料（寻路时可放置的方块，解决 NO-PATH 卡死）",
         {"items": {"type": "array", "items": {"type": "string"},
                     "description": "材料列表，如 ['dirt','cobblestone']"}}, mc_scaffold_materials),
    ]

    count = 0
    for name, desc, props, executor in MC_TOOLS:
        tool = get_action_tool(name)
        if tool is None:
            required = [k for k, v in props.items()
                        if k in ("message", "recipe", "item", "target", "block", "structure", "biome")
                        ] if name != "mc_attack" else []
            # mc_goto/mc_inspect_block/mc_interact_at 需要 x,y,z
            if name in ("mc_goto", "mc_inspect_block", "mc_interact_at"):
                required = ["x", "y", "z"]
            tool = ActionTool(
                name=name,
                description=desc,
                category="minecraft_body",
                parameters={"type": "object", "properties": props, "required": required},
                executor=executor,
            )
            ACTION_TOOLS.append(tool)
        else:
            tool.executor = executor
        count += 1
    return count


# Auto-install on import
install_mc_body_bridge()

#!/usr/bin/env python3
"""SC2 body bridge — sc2_* action tools that command the ego x StarCraft II bridge.

Pattern (same as ego_mc_body_bridge / ego_tool_l7_bridge):
  - auto-install on import: append sc2_* ActionTools into ACTION_TOOLS + bind executors
  - imported by tl_ego_server.py so the P5 tool-calling pre-step can route
    SC2 requests straight to tools (fast path, skips the neural full pipeline)

Transport: append one JSON line to CMD_PATH. The SC2 bridge
(C:\\tools\\sc2win\\ego_sc2_bridge.py, Windows retail client) polls this file
every few game ticks and feeds the line into the same intent executor it uses
for /contemplate decisions. Queued commands survive bridge restarts; if the
bridge is not running the command simply waits.

Self-check: python ego_sc2_body_bridge.py --check
"""
import json
import os
import time
from pathlib import Path

CMD_PATH = Path(
    os.environ.get("EGO_SC2_CMD")
    or (r"C:\tools\sc2win\cmd.jsonl" if os.name == "nt" else str(Path.home() / "sc2" / "cmd.jsonl"))
)

# tool arg -> bridge UnitTypeId name
_BUILD_MAP = {
    "barracks": "BARRACKS",
    "factory": "FACTORY",
    "refinery": "REFINERY",
    "supply_depot": "SUPPLYDEPOT",
    "supply": "SUPPLYDEPOT",
    "depot": "SUPPLYDEPOT",
}
_TRAIN_MAP = {
    "marine": "MARINE",
    "tank": "SIEGETANK",
    "siege_tank": "SIEGETANK",
    "scv": "SCV",
    "worker": "SCV",
}


def _queue(kind: str, param: str | None = None, note: str = "") -> dict:
    try:
        CMD_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {"kind": kind, "param": param, "src": "ego_tool", "note": note, "ts": time.time()}
        with open(CMD_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"status": "ok", "queued": {k: rec[k] for k in ("kind", "param", "note")},
                "hint": "已下发, 桥会在下个游戏节拍执行"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def sc2_build(what: str = "barracks", **kw) -> dict:
    """建造人族建筑(在基地附近自动选址)。what: barracks/factory/refinery/supply_depot"""
    ut = _BUILD_MAP.get(str(what).strip().lower(), str(what).strip().upper())
    return _queue("build", ut, note=f"build {what}")


def sc2_train(what: str = "marine", **kw) -> dict:
    """训练单位(需要对应建筑已就绪: 机枪兵→兵营, 坦克→工厂, 农民→指挥中心)。what: marine/tank/scv"""
    ut = _TRAIN_MAP.get(str(what).strip().lower(), str(what).strip().upper())
    return _queue("train", ut, note=f"train {what}")


def sc2_attack(**kw) -> dict:
    """全军进攻: 所有机枪兵/坦克/掠夺者扑向最近的敌方单位/建筑/敌方基地"""
    return _queue("attack", None, note="attack")


def sc2_defend(**kw) -> dict:
    """全军回防: 所有部队撤回主基地"""
    return _queue("defend", None, note="defend")


def sc2_expand(**kw) -> dict:
    """开分矿: 在主基地附近合适矿点建第二座指挥中心"""
    return _queue("expand", None, note="expand")


def sc2_scout(**kw) -> dict:
    """侦察: 派一个农民去探测敌方基地位置"""
    return _queue("scout", None, note="scout")


def install_sc2_body_bridge() -> int:
    """把 SC2 身体工具注册进 ACTION_TOOLS + executor。返回注册数量。"""
    from action_tools import ACTION_TOOLS, ActionTool

    SC2_TOOLS = [
        ("sc2_build",
         "星际争霸2: 建造人族建筑(自动选址)。造兵营才能出机枪兵; 卡人口先造补给站。",
         {"what": {"type": "string", "enum": ["barracks", "factory", "refinery", "supply_depot"],
                   "description": "建筑类型", "default": "barracks"}},
         sc2_build),
        ("sc2_train",
         "星际争霸2: 训练单位。机枪兵需兵营, 坦克需工厂, 农民需指挥中心。",
         {"what": {"type": "string", "enum": ["marine", "tank", "scv"],
                   "description": "单位类型", "default": "marine"}},
         sc2_train),
        ("sc2_attack",
         "星际争霸2: 全军进攻最近的敌方目标",
         {}, sc2_attack),
        ("sc2_defend",
         "星际争霸2: 全军撤回主基地防守",
         {}, sc2_defend),
        ("sc2_expand",
         "星际争霸2: 开分矿(建第二座指挥中心)",
         {}, sc2_expand),
        ("sc2_scout",
         "星际争霸2: 派农民侦察敌方基地位置",
         {}, sc2_scout),
    ]
    existing = {t.name for t in ACTION_TOOLS}
    added = 0
    for name, desc, params, fn in SC2_TOOLS:
        if name in existing:
            continue
        ACTION_TOOLS.append(ActionTool(name=name, description=desc, parameters=params,
                                       category="sc2", executor=fn))
        added += 1
    return added


# auto-install on import (same pattern as the other body bridges)
try:
    _added = install_sc2_body_bridge()
    if _added:
        print(f"[sc2-body] registered {_added} sc2_* action tools (cmd channel: {CMD_PATH})", flush=True)
except Exception as _e:  # noqa: BLE001
    print(f"[sc2-body] install failed: {_e}", flush=True)


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        from action_tools import get_action_tool
        names = ["sc2_build", "sc2_train", "sc2_attack", "sc2_defend", "sc2_expand", "sc2_scout"]
        for n in names:
            t = get_action_tool(n)
            assert t is not None and t.executor is not None, f"tool {n} not bound"
        print(f"OK: {len(names)} sc2 tools registered and bound; cmd -> {CMD_PATH}")
        # dry-run one queue write + read back
        r = sc2_scout()
        assert r["status"] == "ok", r
        line = CMD_PATH.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(line)["kind"] == "scout", line
        print("OK: queue write/read-back verified:", line[:120])

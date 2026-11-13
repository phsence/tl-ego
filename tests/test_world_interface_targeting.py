#!/usr/bin/env python3
"""test_world_interface_targeting.py — world_* goals must name the LIVE body.

为什么要有它: world_act/world_verify 模板过去把 "Minecraft" 写死。2026-11-01 起 MC 栈
(25565/8765/3007) 全关，ego 仍按 MC 派发 world_act，执行器无世界可动，closeout 却照旧
stamp success (reason=subagent_comm_pid_not_alive, 沙箱零产物)。本测试钉住三件事:
  1. gmod 在线的文本里出现 8790，且不再出现 "Minecraft"/造方块指令
  2. minecraft 在线的文本里出现 8765，不出现 8790
  3. 两个桥都关时文本禁止声称世界动作，且服务端 success_criteria 有 body-specific 覆盖

跑法: python -B tests/test_world_interface_targeting.py   (断言式, 无框架)
"""
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tools_registry as TR  # noqa: E402

SERVER = ROOT / "tl_ego_server.py"


class _SD(dict):
    def __missing__(self, key):
        return "n/a"


def _desc_for(name):
    for n, _port, desc in TR._WORLD_BODIES:
        if n == name:
            return desc
    return TR._WORLD_NONE_DESC


def _render(goal_type, world_iface):
    return TR.TOOLS[goal_type]["prompt_template"].format_map(_SD(
        dominant="action", competition=0.9, concentration=0.2, stagnation=3, tick=100,
        external_world_summary="workspace: 8 files changed", world_iface=world_iface,
    ))


def test_gmod_text_targets_8790():
    for goal_type in ("world_act", "world_verify", "world_explore"):
        txt = _render(goal_type, _desc_for("gmod"))
        assert "8790" in txt, (goal_type, txt)
        assert "Minecraft" not in txt, (goal_type, txt)          # 旧硬编码必须消失
        assert "place blocks in GMod" in txt or goal_type != "world_act", txt


def test_minecraft_text_unchanged_in_spirit():
    txt = _render("world_act", _desc_for("minecraft"))
    assert "8765" in txt and "8790" not in txt, txt


def test_no_body_forbids_claiming():
    txt = _render("world_act", TR._WORLD_NONE_DESC)
    assert "unverifiable" in txt and "do NOT claim" in txt, txt


def test_probe_agrees_with_reality():
    name, desc = TR.world_interface()
    assert name in ("minecraft", "gmod", "none"), name
    assert desc and isinstance(desc, str)
    if name != "none":
        port = {"minecraft": 8765, "gmod": 8790}[name]
        socket.create_connection(("127.0.0.1", port), 1).close()   # 声称活着就必须真能连上


def test_server_criteria_are_body_specific():
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert 'success_by_type["world_act"]' in src
    assert "the GMod body actually changed" in src, "gmod 的 world_act 判据丢失"
    assert "no live world body (8765/8790 both closed)" in src, "双关判据丢失"
    # 蓝图菜单只允许在 MC 上出现（GMod 没有方块）
    assert '_pending_type == "world_act" and _wi_name == "minecraft"' in src
    assert "world_iface=_wi_desc" in src, "模板新占位符没喂进 format_map"


if __name__ == "__main__":
    for fn in (test_gmod_text_targets_8790, test_minecraft_text_unchanged_in_spirit,
               test_no_body_forbids_claiming, test_probe_agrees_with_reality,
               test_server_criteria_are_body_specific):
        fn()
        print(f"PASS {fn.__name__}")
    print("live probe ->", TR.world_interface()[0])

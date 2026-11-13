#!/usr/bin/env python3
"""test_dispatch_diversity_gate.py — 派发反单一化闸门 + 关键词边界匹配 (tl_ego_server)。

背景: grown 种类(need_*/custom_*)在派发顺序里"命中即 return", grown 占比从生命周期 5%
升到近 60 次里 51%。闸门 = l5_action 车道最近连续 >=3 次 grown 且 hint 同时命中静态表里的
研究/测量类 → 让位一次给静态类型; 否则维持原样(不夺新意图的常规优先级)。

踩雷警告: 派发历史**必须**读 .lae/l5_dispatch_requests.jsonl (真派发记录, 分 l5_action /
comm_action 两条车道); 读 .lae/l5_trigger_queue.jsonl 会把构成算成 100% grown (那只记待办
的 novel kind)。

跑法: python -B tests/test_dispatch_diversity_gate.py   (断言式, 无框架, 不碰网络)
"""
import json
import re
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "tl_ego_server.py"
src = SERVER.read_text(encoding="utf-8", errors="replace")

tmp = Path(tempfile.mkdtemp(prefix="dgate_"))
(tmp / ".lae").mkdir(parents=True, exist_ok=True)
REQ = tmp / ".lae" / "l5_dispatch_requests.jsonl"
QUEUE = tmp / ".lae" / "l5_trigger_queue.jsonl"


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |^class |\Z)", src, re.S | re.M)
    assert m, f"找不到 {name}"
    return m.group(0)


ns = {"json": json, "re": re, "WORKSPACE": tmp}
exec(re.search(r"^HINT_KEYWORD_MAP = \{.*?^\}", src, re.S | re.M).group(0), ns)
exec(re.search(r"^_DIVERSITY_ALT_TYPES = \(.*?^\)", src, re.S | re.M).group(0), ns)
exec(_extract("_kw_hit"), ns)
exec(_extract("_recent_dispatch_types"), ns)
exec(_extract("_diversity_gate"), ns)
kw_hit, gate, recent = ns["_kw_hit"], ns["_diversity_gate"], ns["_recent_dispatch_types"]


def write_reqs(entries):
    """entries: [(type, goalType), ...]"""
    with REQ.open("w", encoding="utf-8") as f:
        for i, (t, g) in enumerate(entries):
            f.write(json.dumps({"tick": i, "type": t, "goalType": g, "goalText": "x" * 50}) + "\n")


# ── 1) 边界匹配 (2026-10-16: 砍掉串中误配, 保住贪心后缀) ──
assert kw_hit("read", "already built the bridge") is False
assert kw_hit("block", "unblocked the path") is False
assert kw_hit("explor", "a short exploration pass") is True
assert kw_hit("calibrat", "recalibrated the sensor") is False   # 词中不算
assert kw_hit("calibrat", "calibrated the sensor") is True
assert kw_hit("comm", "commander says go") is True               # 词首真命中(词源问题不归匹配管)
assert kw_hit("self-repair", "do a self-repair pass") is True    # 连字符键照常

# ── 2) 历史来源: 只认 l5_action 车道, comm_action 不算 (否则闸门被通信车道刷没了) ──
write_reqs([("comm_action", "message_commander"), ("l5_action", "need_build"),
            ("comm_action", "message_commander"), ("l5_action", "need_comm")])
assert recent() == ["need_build", "need_comm"], recent()

# ── 3) 清一色 grown (>=3) + hint 命中研究/测量类 → 让位 ──
write_reqs([("l5_action", "need_build"), ("l5_action", "need_comm"), ("l5_action", "need_block")])
assert gate("need_comm", "a short exploration pass to break the loop") == "curiosity_explore"
assert gate("need_probe", "check stagnation trend over ticks") == "deep_probe_unstable"

# ── 4) 让位白名单: 只挑研究/测量类, rule_modification_repair 之类的职能不让位 ──
assert gate("need_comm", "the self-repair rule needs updating") == ""
assert gate("need_comm", "sharpen the repair rule") == ""

# ── 5) 最近派发里出现过非 grown (run 断掉) → 不让位 ──
write_reqs([("l5_action", "need_build"), ("l5_action", "deep_probe_unstable"), ("l5_action", "need_comm")])
assert gate("need_comm", "a short exploration pass") == ""

# ── 6) hint 本身就是静态类型 → 不归闸门管 ──
write_reqs([("l5_action", "need_build")] * 3)
assert gate("curiosity_explore", "explore something") == ""

# ── 7) hint 命不中研究/测量类 → 不让位 (避免把 grown 挤没) ──
assert gate("need_comm", "the bridge now runs 8 east from ego's feet") == ""

# ── 8) 记录文件缺失 → 不让位, 不抛异常 ──
REQ.unlink()
assert recent() == []
assert gate("need_build", "explore something") == ""

# ── 9) 阈值可调 ──
write_reqs([("l5_action", "need_build"), ("l5_action", "need_comm")])
assert gate("need_comm", "exploration", run_threshold=2) == "curiosity_explore"

# ── 10) 回归防护: 队列文件不是历史来源 (队列里有东西也不影响 recent) ──
REQ.unlink()                       # 记录文件缺席
QUEUE.write_text(json.dumps({"goal_type": "need_zzz"}) + "\n", encoding="utf-8")
assert recent() == [], "recent() 不该读 l5_trigger_queue.jsonl"

print("[test_dispatch_diversity_gate] OK （10 组断言全过）")
print("tmp:", tmp)

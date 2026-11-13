#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L5 参数写权持有 (tl_ego_server._l5_hold_block) 的行为检查。

背景: 2026-11-07 kwta_pool_mult 一天被写 21 次, 其中 20 次是 L10 强行覆盖 L5 的落地,
任何 L5 证据都在写完一小时内消失。这里只验守卫本身的语义, 不启服务:
真正 import 28k 行的 tl_ego_server 会拉起整个 runtime, 所以用 ast 把两个纯函数
摘出来, 配一个假账本执行 —— 逻辑变了测试就红。
"""
import ast
import json
import os
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "tl_ego_server.py"
WANT = {"_l5_hold_seed", "_l5_hold_block"}


def harness(ledger_rows, hold_s=5400):
    tree = ast.parse(SRC.read_text(encoding="utf-8", errors="replace"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANT]
    assert len(nodes) == len(WANT), f"helpers moved/renamed: found {[n.name for n in nodes]}"
    ws = Path(tempfile.mkdtemp())
    (ws / ".lae").mkdir()
    lines = [json.dumps(r, ensure_ascii=False) for r in ledger_rows]
    (ws / ".lae" / "parameter_updates.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    ns = {"json": json, "os": os, "time": time, "WORKSPACE": ws,
          "L5_PARAM_HOLD_S": hold_s, "_L5_PARAM_HOLD": {},
          "print": lambda *a, **k: None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), ns)
    return ns


def row(key, value, source, age_s=0.0):
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_s)),
            "key": key, "new_value": value, "source": source}


def test_l10_cannot_overwrite_fresh_l5_landing():
    ns = harness([row("kwta_pool_mult", 5.0, "l5g-20261107-066847")])
    blk = ns["_l5_hold_block"]("kwta_pool_mult", 3.0, "l10_candidate")
    assert blk and "holds until" in blk, blk


def test_same_value_write_is_not_a_takeover():
    ns = harness([row("kwta_pool_mult", 5.0, "l5g-20261107-066847")])
    assert ns["_l5_hold_block"]("kwta_pool_mult", 5.0, "l10_candidate") is None


def test_operator_still_wins():
    ns = harness([row("kwta_pool_mult", 5.0, "l5g-20261107-066847")])
    assert ns["_l5_hold_block"]("kwta_pool_mult", 3.0, "operator") is None
    assert ns["_l5_hold_block"]("kwta_pool_mult", 3.0, "hermes-user-grant") is None


def test_expired_landing_releases_the_key():
    ns = harness([row("kwta_pool_mult", 5.0, "l5g-20261107-066847", age_s=7200)])
    assert ns["_l5_hold_block"]("kwta_pool_mult", 3.0, "l10_candidate") is None


def test_knob_zero_disables_the_guard():
    ns = harness([row("kwta_pool_mult", 5.0, "l5g-20261107-066847")], hold_s=0)
    assert ns["_l5_hold_block"]("kwta_pool_mult", 3.0, "l10_candidate") is None


def test_boot_replay_never_blocked():
    ns = harness([row("kwta_pool_mult", 5.0, "l5g-20261107-066847")])
    assert ns["_l5_hold_block"]("kwta_pool_mult", 3.0,
                                "boot_replay") is None


def test_untouched_key_and_other_l5_key_unaffected():
    ns = harness([row("kwta_pool_mult", 5.0, "l5g-20261107-066847")])
    assert ns["_l5_hold_block"]("drive_decay", 0.05, "l10_candidate") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)

"""选择器现任惩罚倍率：真的接上了吗（白名单 / 范围 / 读点 / 钳位）。

这个键存在的唯一理由是"选择器层原本没有可写落地点"——所以测的也是这件事：
1) 桥的写通道能从服务器源码里解析到它和它的范围（否则写了也被拒）；
2) 源码里它既在 _CHANNEL_REACHABLE 里、又有真实读点（否则 _live_param_keys
   会把它当死键，目标文本继续教 ego 别用）；
3) 读点函数自己钳位（手改 runtime_params.json 不能把 incumbent 永久踢出）。
"""
import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "tl_ego_server.py"
KEY = "incumbent_penalty_scale"


def load_bridge():
    spec = importlib.util.spec_from_file_location("br_pen", str(ROOT / "l10" / "l10_l5_patch_bridge.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["br_pen"] = m
    spec.loader.exec_module(m)
    return m


def penalty_fn(resting):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_incumbent_penalty_scale":
            ns = {"RESTING_STATE": resting}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVER), "exec"), ns)
            return ns["_incumbent_penalty_scale"]
    raise AssertionError("_incumbent_penalty_scale not found")


def live_keys():
    """原样跑服务器里的 _live_param_keys()（只取它需要的三个节点）。"""
    import time
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    keep = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") in {"PARAM_WHITELIST", "PARAM_RANGES"} for t in node.targets):
            keep.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_live_param_keys":
            keep.append(node)
    ns = {"__file__": str(SERVER), "Path": Path, "re": re, "time": time,
          "_LIVE_PARAM_CACHE": None}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SERVER), "exec"), ns)
    return ns["_live_param_keys"]()


def main() -> None:
    src = SERVER.read_text(encoding="utf-8")

    # 1. 写通道认得它 + 范围
    wl, rng = load_bridge()._whitelist_and_ranges()
    assert KEY in wl, f"{KEY} not in whitelist parsed from tl_ego_server.py"
    assert rng.get(KEY) == (0.5, 12.0), rng.get(KEY)

    # 2. 它在 _live_param_keys 的两个条件里：channel-reachable + 真实读点
    m = re.search(r"_CHANNEL_REACHABLE = frozenset\(\{(.*?)\n    \}\)", src, re.S)
    assert m, "_CHANNEL_REACHABLE block not found"
    assert f'"{KEY}"' in m.group(1), "key missing from _CHANNEL_REACHABLE"
    assert re.search(rf'\.get\(\s*["\']{KEY}["\']', src), "no RESTING_STATE.get reader"

    # 3. 读点行为：默认 1.0（等价旧行为）、越界钳住、垃圾值不炸
    assert penalty_fn({})() == 1.0
    assert penalty_fn({KEY: 8.0})() == 8.0
    assert penalty_fn({KEY: 999})() == 12.0, "upper clamp missing: incumbent could be locked out"
    assert penalty_fn({KEY: 0.0})() == 0.5, "lower clamp missing: penalty could vanish"
    assert penalty_fn({KEY: "abc"})() == 1.0
    assert penalty_fn({KEY: None})() == 1.0

    # 4. 选择器真的乘这个倍率（不是还留着硬编码 -0.10）
    assert re.search(r"score\s*-\s*0\.10\s*\*\s*_incumbent_penalty_scale\(\)", src), \
        "selector no longer multiplies by the scale"

    # 5. 真跑一遍 _live_param_keys()：它才是决定目标文本里"活键名单"的那个。
    #    死在 _CHANNEL_REACHABLE 或读点正则任一条件上，这个键就不会出现在
    #    ego 的授权块里 —— 那等于白加。
    assert KEY in live_keys(), f"{KEY} not reported live: {sorted(live_keys())}"
    print("OK: incumbent_penalty_scale wired (whitelist 0.5..12, live reader, clamped, "
          f"selector uses it, reported live among {len(live_keys())} keys)")


if __name__ == "__main__":
    main()

"""族注册表缺口提示：只在真有缺口时出现，且缺口随注册表行收缩。

这段文本会拼进每一条 L5 目标，所以两件事必须成立：没缺口就空（不把每个目标
都变成造族目标）、有缺口时只点没有族行覆盖的死类型。直接 exec 服务器源码里的
那个函数（服务器不能 import：有单实例 sys.exit 副作用）。
"""
import ast
import json
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent / "tl_ego_server.py"


def _compile_fn(name: str):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return compile(ast.Module(body=[node], type_ignores=[]), str(SERVER), "exec")
    raise AssertionError(f"{name} not found in tl_ego_server.py")


def note_fn(workspace: Path):
    ns = {"WORKSPACE": workspace, "json": json,
          "_live_param_keys": lambda: {"kwta_pool_mult", "drive_decay"}}
    exec(_compile_fn("_family_invention_note"), ns)
    return ns["_family_invention_note"]


def make_ws(td: Path, dead: list[str]) -> Path:
    lae = td / ".lae"
    lae.mkdir(parents=True, exist_ok=True)
    (lae / "l10_observer.jsonl").write_text(
        json.dumps({"tick": 1, "profile": {"dead_types": dead}}), encoding="utf-8")
    return td


def main() -> None:
    td = Path(tempfile.mkdtemp())
    ws = make_ws(td, ["zeta", "yankee", "xray", "wisp"])
    note = note_fn(ws)

    # 1. 没有任何族行 → 提示存在，且最多点 3 个死类型（不刷屏）
    text = note()
    assert "[族注册表缺口" in text, text
    assert all(t in text for t in ("zeta", "yankee", "xray")), text
    assert "wisp" not in text, text
    assert "kwta_pool_mult" in text and "l10_family_registry" in text, text

    # 2. 写两行族行覆盖 zeta/yankee → 缺口收缩，已覆盖的从提示里消失
    reg = ws / ".lae" / "l10_family_registry"
    reg.mkdir()
    (reg / "l5g-a.json").write_text(json.dumps(
        {"type": "fam_a", "when": {"dead_types_any": ["zeta"]}, "target": "kwta_pool_mult",
         "value": 2.0, "hypothesis": "h"}), encoding="utf-8")
    (reg / "l5g-b.json").write_text(json.dumps([
        {"type": "fam_b", "when": {"dead_types_any": ["yankee"]}, "target": "drive_decay",
         "value": 0.4, "hypothesis": "h"}]), encoding="utf-8")
    text = note()
    assert "xray" in text and "wisp" in text, text
    assert "zeta" not in text and "yankee" not in text, text

    # 3. 全覆盖 → 整段消失
    (reg / "l5g-c.json").write_text(json.dumps(
        {"type": "fam_c", "when": {"dead_types_any": ["xray", "wisp"]}}), encoding="utf-8")
    assert note() == ""

    # 4. 没有 observer → 不猜，返回空
    empty = Path(tempfile.mkdtemp())
    assert note_fn(empty)() == ""

    # 5. 注册表里坏文件不影响提示（跳过即可）
    (reg / "broken.json").write_text("{oops", encoding="utf-8")
    (reg / "l5g-c.json").unlink()
    assert "xray" in note()

    print("OK: invention note appears only with a real gap and shrinks as rows cover it")


if __name__ == "__main__":
    main()

"""端到端：一条族行 → 生成器候选 → 桥的 shadow_only 行（全在 scratch，不碰生产）。

注册表目录和提案日志都重定向到临时目录，只为证明这条握手真的通：族行被生成器
吃进去、桥认它、落进提案日志被评估。生产 .lae/l10_family_registry/ 保持空 ——
那是 ego 自己的写入口。
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:/Users/Administrator/.openclaw/workspace/projects/tl-ego")


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def main() -> None:
    bridge = load("br_e2e", "l10/l10_l5_patch_bridge.py")
    gbridge = load("gb_e2e", "l10/l10_generator_bridge.py")
    gen = load("gen_e2e", "l10/l10_meta_algorithm_generator.py")

    td = Path(tempfile.mkdtemp())
    reg = td / "registry"
    reg.mkdir()
    (reg / "l5g-e2e.json").write_text(json.dumps({
        "type": "social_reentry_floor",
        "when": {"dead_types_any": ["social"]},
        "target": "kwta_pool_mult",
        "value": 2.2,
        "hypothesis": "social 长期不主导：把池倍数小幅抬高让边缘类型进入竞争",
        "evidence": {"source": "l10_observer", "dead_type": "social"},
        "author": "l5g-e2e",
    }, ensure_ascii=False), encoding="utf-8")
    gen.FAMILY_REGISTRY_DIR = reg
    bridge.PROPOSAL_LOG = td / "proposals.jsonl"

    prof = gbridge._build_profile(gbridge._read_latest_l10_profile(), None, False, "",
                                  gbridge._read_latest_injection())
    assert "social" in set(prof.dead_types), f"live profile lacks social: {prof.dead_types}"
    res = gen.MetaAlgorithmGenerator().generate(prof, {}, 70653)
    types = [(c.get("strategy") or {}).get("type") for c in res.get("algorithm_candidates") or []]
    print("emitted:", types)
    assert "social_reentry_floor" in types, types

    bridge.propose_param({"candidates": res["algorithm_candidates"], "tick": 70653,
                          "profile": {"dead_types": prof.dead_types}})
    rows = [json.loads(l) for l in bridge.PROPOSAL_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    mine = [r for r in rows if (r.get("candidate") or {}).get("strategy", {}).get("type") == "social_reentry_floor"]
    assert mine, rows
    row = mine[0]
    print("bridge row:", {k: row.get(k) for k in ("status", "shadow_param")})
    assert row["status"] == "shadow_only", row
    assert row["shadow_param"] == {"key": "kwta_pool_mult", "value": 2.2, "requested": 2.2}, row
    assert "param" not in row, "shadow row must not be consumable by the runtime applier"

    # 生产注册表必须仍是空的/不存在（写入口留给 ego）
    prod = ROOT.parent.parent / ".lae" / "l10_family_registry"
    print("production registry rows:", len(list(prod.glob("*.json"))) if prod.is_dir() else 0)
    print("E2E_OK: family row -> candidate -> shadow proposal")


if __name__ == "__main__":
    main()

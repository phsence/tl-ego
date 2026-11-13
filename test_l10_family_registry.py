"""族注册表：LLM（L5 执行器）写的族行，能不能安全地变成候选。

信任边界只有一处（registry_families / _iter_registry_rows），所以测的就是：
合法的进来且强制 shadow、条件不成立不产出、坏行全挡掉并留原因、
一目标一文件互不覆盖、注册表失控有上限、坏文件不连累整轮。
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

MOD = Path(__file__).resolve().parent / "l10" / "l10_meta_algorithm_generator.py"


def load():
    spec = importlib.util.spec_from_file_location("gen_reg_test", str(MOD))
    m = importlib.util.module_from_spec(spec)
    sys.modules["gen_reg_test"] = m
    spec.loader.exec_module(m)
    return m


GOOD = {
    "type": "curiosity_floor_probe",
    "pair": "deepening_existing",
    "when": {"dead_types_any": ["curiosity"], "dead_type_count_min": 10},
    "target": "curiosity_activation_boost",
    "value": 2.5,
    "hypothesis": "curiosity 长期死着，抬传播地板让它重新进竞争",
    "evidence": {"source": "l10_observer"},
    "author": "l5g-test",
}


def main() -> None:
    g = load()
    prof = g.BottleneckProfile(dead_type_count=16, dead_types=["curiosity", "social"])
    td = Path(tempfile.mkdtemp())
    reg = td / "registry"
    g.FAMILY_REGISTRY_DIR = reg

    # 1. 目录不存在（生产默认态）→ 零族，零报错
    assert g.registry_families(prof) == [] and g._REG_REJECTS == [], g._REG_REJECTS

    # 2. 一目标一文件，单行对象 → 一条策略，强制 shadow_only，附 provenance
    reg.mkdir()
    (reg / "l5g-1.json").write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    fams = g.registry_families(prof)
    assert len(fams) == 1, fams
    f = fams[0]
    assert f["type"] == "curiosity_floor_probe" and f["shadow_only"] is True, f
    assert f["shadow_param"] == {"key": "curiosity_activation_boost", "value": 2.5}, f
    assert f["provenance"] == {"source": "family_registry", "author": "l5g-test",
                               "registry_index": "l5g-1.json"}, f["provenance"]

    # 3. 条件不成立 → 不产出，原因写清楚（不是静默丢）
    cold = g.BottleneckProfile(dead_type_count=2, dead_types=["social"])
    assert g.registry_families(cold) == []
    assert "condition not met" in g._REG_REJECTS[0]["reason"], g._REG_REJECTS

    # 4. 一个文件装一组行：坏行全挡、好行照出来
    (reg / "l5g-1.json").unlink()
    bad = [
        {**GOOD, "type": "CuriosityBoost"},            # 非 snake_case
        {**GOOD, "type": "no_target_row", "target": ""},
        {**GOOD, "type": "bool_value", "value": True},  # bool 不是数字
        {**GOOD, "type": "str_value", "value": "2.5"},
        {**GOOD, "type": "no_hypothesis", "hypothesis": "  "},
        {**GOOD, "type": "bad_when", "when": {"dead_type": "curiosity"}},
        {**GOOD, "type": "nan_value", "value": float("nan")},
        "not an object",
    ]
    (reg / "batch.json").write_text(json.dumps([*bad, {**GOOD, "type": "still_valid"}],
                                               ensure_ascii=False), encoding="utf-8")
    fams = g.registry_families(prof)
    assert [f["type"] for f in fams] == ["still_valid"], fams
    assert len(g._REG_REJECTS) == len(bad), g._REG_REJECTS

    # 5. pair 不匹配是正常筛选，不算拒绝
    (reg / "batch.json").unlink()
    (reg / "osc.json").write_text(json.dumps({**GOOD, "pair": "stable_oscillation"}), encoding="utf-8")
    assert g.registry_families(prof, "deepening_existing") == [] and g._REG_REJECTS == []

    # 6. 一目标一文件互不覆盖：两个文件各一行 → 两族
    (reg / "osc.json").unlink()
    for i in range(2):
        (reg / f"l5g-{i}.json").write_text(json.dumps({**GOOD, "type": f"fam_{i}"}), encoding="utf-8")
    assert sorted(f["type"] for f in g.registry_families(prof)) == ["fam_0", "fam_1"]

    # 7. 注册表失控 → 一轮最多 8 条
    for i in range(2, 14):
        (reg / f"l5g-{i}.json").write_text(json.dumps({**GOOD, "type": f"fam_{i}"}), encoding="utf-8")
    assert len(g.registry_families(prof)) == 8

    # 8. 坏文件不连累整轮：一个坏 JSON + 一个标量文件被记账，好文件照常产出
    (reg / "broken.json").write_text("{oops", encoding="utf-8")
    (reg / "scalar.json").write_text('"just a string"', encoding="utf-8")
    fams = g.registry_families(prof)
    assert len(fams) == 8, fams
    reasons = " ".join(str(r["reason"]) for r in g._REG_REJECTS)
    assert "unreadable file" in reasons and "object or a list" in reasons, g._REG_REJECTS

    print("OK: registry rows emit only when valid+condition holds, always shadow_only, faults isolated")


if __name__ == "__main__":
    main()

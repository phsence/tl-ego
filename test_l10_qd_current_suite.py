"""qd_saturation 必须读**当前**套件，而不是写死的字面量。

冻结实证 2026-11-11: 默认前缀 "hidden_v6" 停在 2026-09-22 打满 1.0 的退役套件上,
saturated 恒 True -> apply_qd_pressure 把 deepening_existing 归零 -> 四个真实族
全部出不来。这个测试就是那次事故的最小复现 + 修复断言。
"""
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

MOD = Path(__file__).resolve().parent / "l10" / "l10_meta_algorithm_generator.py"


def load():
    spec = importlib.util.spec_from_file_location("gen_qd_test", str(MOD))
    m = importlib.util.module_from_spec(spec)
    sys.modules["gen_qd_test"] = m
    spec.loader.exec_module(m)
    return m


def make_db(path: Path, rows: list[tuple[str, float, str]]) -> None:
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE IF EXISTS archive")
    con.execute("CREATE TABLE archive (niche TEXT, fitness REAL, added_at TEXT)")
    con.executemany("INSERT INTO archive VALUES (?,?,?)", rows)
    con.commit()
    con.close()


def main() -> None:
    g = load()
    td = Path(tempfile.mkdtemp())
    db = td / "archive.sqlite"

    # 1. 退役套件打满 + 现役套件未打满 → 不得判饱和 (事故现场)
    make_db(db, [
        ("hidden_v6:kwta_threshold_expansion", 1.0, "2026-09-22 13:09:07"),
        ("hidden_v6:hp_types_rotation", 1.0, "2026-09-22 13:09:07"),
        ("hidden_v7:kwta_threshold_expansion", 0.8776, "2026-10-07 16:31:21"),
        ("hidden_v7:hp_types_rotation", 0.65, "2026-10-07 16:31:21"),
    ])
    g.QD_ARCHIVE_SQLITE = db
    s = g.qd_saturation()
    assert s["suite_prefix"] == "hidden_v7", s
    assert s["saturated"] is False and s["cells"] == 2, s

    pair = g.DissensusPair(s1_label="deepening_existing", s1_approach="a",
                           s2_label="expanding_candidate_pool", s2_approach="b",
                           score=1.0, rationale="r")
    kept = g.apply_qd_pressure([pair], s)
    assert [p.score for p in kept] == [1.0], kept  # 真实族不被归零

    # 2. 现役套件真打满 → 仍然判饱和，压力照旧生效
    make_db(db, [
        ("hidden_v7:kwta_threshold_expansion", 1.0, "2026-11-11 10:00:00"),
        ("hidden_v7:hp_types_rotation", 1.0, "2026-11-11 10:00:00"),
    ])
    s = g.qd_saturation()
    assert s["saturated"] is True and s["suite_prefix"] == "hidden_v7", s
    pair = g.DissensusPair(s1_label="deepening_existing", s1_approach="a",
                           s2_label="expanding_candidate_pool", s2_approach="b",
                           score=1.0, rationale="r")
    kept = g.apply_qd_pressure([pair], s)
    assert kept[0].score == 0.0, kept  # 饱和: 真实族被降权
    assert any(p.s1_label == "closed_self_reflection" for p in kept), kept

    # 3. 空档案 / 缺文件 → 未饱和，前缀为空，绝不抛
    make_db(db, [])
    s = g.qd_saturation()
    assert s == {"saturated": False, "cells": 0, "best": 0.0, "suite_prefix": ""}, s
    g.QD_ARCHIVE_SQLITE = td / "nope.sqlite"
    assert g.qd_saturation()["saturated"] is False

    # 4. 显式传前缀仍然照用（调用方覆盖）
    make_db(db, [("hidden_v9:x", 1.0, "2026-11-11"), ("hidden_v9:y", 1.0, "2026-11-11")])
    g.QD_ARCHIVE_SQLITE = db
    assert g.qd_saturation("hidden_v9")["saturated"] is True
    assert g.qd_saturation("hidden_v8")["saturated"] is False

    # 5. 真实 niche 带 QD 网格坐标前缀 → 裸套件名分组，格子不被坐标拆散
    make_db(db, [
        ("0|0|9|0|0|0|hidden_v8:a", 0.5, "2026-11-11 10:00:00"),
        ("1|2|9|0|0|0|hidden_v8:b", 1.0, "2026-11-11 10:05:00"),
        ("1|2|9|0|0|0|hidden_v8:c", 1.0, "2026-11-11 10:06:00"),
    ])
    g.QD_ARCHIVE_SQLITE = db
    s = g.qd_saturation()
    assert s["suite_prefix"] == "hidden_v8", s
    assert s["cells"] == 3 and s["saturated"] is True, s

    print("OK: qd_saturation follows the current suite; retired-suite 1.0 no longer freezes the loop")


if __name__ == "__main__":
    main()

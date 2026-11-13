import json
from pathlib import Path


def minecraft_lessons_note(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8")).get("lessons", [])
    rules = [str(row["rule"]).strip() for row in rows
             if isinstance(row, dict) and row.get("rule")
             and str(row.get("class", "")).startswith(("combat_", "move_", "starve", "death:"))]
    return ("[已验证的 Minecraft 行为教训；按当前情境采用，不相关时忽略]\n"
            + "\n".join("- " + rule for rule in rules[:5])) if rules else ""


def selftest():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "lessons.json"
    p.write_text(json.dumps({"lessons": [
        {"class": "death:husk", "rule": "被 Husk 击杀后先检查生命和武器"},
        {"class": "reject:build", "rule": "桥参数错误，不应变成策略教训"},
    ]}, ensure_ascii=False), encoding="utf-8")
    note = minecraft_lessons_note(p)
    assert "检查生命和武器" in note and "桥参数错误" not in note and "\\n" not in note
    print("minecraft lesson note self-check OK")


if __name__ == "__main__":
    selftest()

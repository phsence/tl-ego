#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lesson_consolidate.py — 经历→规则沉淀 (2026-10-14 重建)

数据源: memu.db memory_items (mc_activity + mc_lesson, 最近 3000 行)
分类: move_stall / killed_by:<mob> / kill:<mob> / starve / suffocate / drown /
      fall / burn / reject:<tool>
阈值: reject:* >= 20 (arg-format 噪音大多不注入), 其余 >= 3
产出: .lae/lessons.json (规则集, 供决策轮 standing_lessons_note 注入)
      + .lae/lessons.json.bak + .lae/lesson_audit.jsonl
规则来源: 一次 ego LLM batch 调用写 <=30 字中文规则; 失败 → 模板 (origin=template)

用法: python lesson_consolidate.py [--selftest]
被 Hermes cron 'mc-lesson-consolidate' (每30m) 经 lesson_consolidate_job.py 调起;
wrapper 看 stdout: "[lesson-consolidate] refreshed=N classes=..."。
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, r"C:\Users\Administrator\.openclaw\skills\neural_grid")
sys.path.insert(0, os.path.join(HERE, "..", ".."))

DB = os.path.join(HERE, "..", "..", "memory", "memu.db")
OUT = os.path.join(HERE, ".lae", "lessons.json")
AUDIT = os.path.join(HERE, ".lae", "lesson_audit.jsonl")

SCAN_ROWS = 3000
TH_OTHER = 3      # 非 reject 类阈值
TH_REJECT = 20    # reject 类阈值 (参数格式噪音, 只审计不注入)
MAX_CLASSES = 8

_DEATH_CAUSE = (
    ("starve", ("饿死", "饿得")),
    ("suffocate", ("窒息", "suffocat")),
    ("drown", ("淹死", "drown")),
    ("fall", ("摔死",)),
    ("burn", ("烧死",)),
)
_RE_MOB = re.compile(r"被\s*(.{1,30}?)\s*(?:击杀|杀死)")
_RE_KILL = re.compile(r"击杀\s*([^\s，。,.!！]{1,20})")
_RE_REJECT = re.compile(r"(?:使用工具|工具)\s*([A-Za-z_][A-Za-z0-9_]*)")
_RE_REJECT2 = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*但被拒绝")


def _mob(name):
    """'Witch using magic' -> 'witch' — 归一化, 不然一个杀手拆成两类。"""
    return re.split(r"\s+using\b", (name or "").strip(), 1)[0].strip().lower()


def classify(text):
    """一行 memu 文本 -> 结果类名, 无法归类 -> None。"""
    t = text or ""
    if "身体未动" in t:
        return "move_stall"
    m = _RE_MOB.search(t)
    if m:
        return "killed_by:" + (_mob(m.group(1)) or "unknown")
    for cls, keys in _DEATH_CAUSE:
        if any(k in t for k in keys):
            return cls
    if "击杀" in t:
        m = _RE_KILL.search(t)
        if m:
            return "kill:" + _mob(m.group(1))
    if "被拒绝" in t or "rejected" in t or "invalid arguments" in t:
        m = _RE_REJECT.search(t) or _RE_REJECT2.search(t)
        return "reject:" + (m.group(1) if m else "unknown")
    return None


_TPL = {
    "move_stall": "移动连续失败(身体未动)时换目标或路径, 不要原地重试",
    "starve": "保持饱食度, 饥饿时先找食物再长途行动",
    "suffocate": "被方块困住时先挖开头顶和身边再移动",
    "drown": "水下注意氧气, 尽快上浮换气",
    "fall": "高处移动前先搭路或找安全落点",
    "burn": "远离岩浆与火源, 先脱困再行动",
}


def _tpl(cls):
    if cls in _TPL:
        return _TPL[cls]
    if cls.startswith("killed_by:"):
        return "遇到 %s 先保持距离, 评估血量再决定打或撤" % cls.split(":", 1)[1]
    if cls.startswith("kill:"):
        return "对 %s 的有效打法可以复用" % cls.split(":", 1)[1]
    if cls.startswith("reject:"):
        return "调用 %s 前先检查参数格式" % cls.split(":", 1)[1]
    return "复盘同类失败, 避免重复"


def scan(db_path=None):
    p = os.path.abspath(db_path or DB)
    con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT summary FROM memory_items WHERE memory_type IN ('mc_activity','mc_lesson') "
            "ORDER BY ts DESC LIMIT ?", (SCAN_ROWS,)).fetchall()
    finally:
        con.close()
    counts, samples = {}, {}
    for (s,) in rows:
        cls = classify(s)
        if not cls:
            continue
        counts[cls] = counts.get(cls, 0) + 1
        samples.setdefault(cls, [])
        if len(samples[cls]) < 4:
            samples[cls].append((s or "").strip()[:120])
    return counts, samples


def over_threshold(counts):
    """超阈类, 排序键 (is_reject, -count) — 死亡/行为类永远排在 reject 噪音前。"""
    keep = {c: n for c, n in counts.items()
            if n >= (TH_REJECT if c.startswith("reject:") else TH_OTHER)}
    return sorted(keep.items(), key=lambda kv: (kv[0].startswith("reject:"), -kv[1]))[:MAX_CLASSES]


def _llm_rules(ranked, samples):
    """一次 batch LLM 调用 -> {class: rule}; 任何失败 -> {}。"""
    key = os.environ.get("EGO_LLM_API_KEY", "") or os.environ.get("STEPFUN_API_KEY", "")
    if not key:
        try:
            key = open(os.path.join(HERE, "ego_key.txt"), encoding="utf-8",
                       errors="replace").read().splitlines()[0].strip()
        except Exception:
            key = ""
    if not key:
        return {}
    try:
        from ego_llm_gate import llm_post_json
        blocks = ["%s (出现%d次): %s" % (cls, n, " / ".join(samples.get(cls, [])[:3]))
                  for cls, n in ranked]
        prompt = ("你是 TL-Ego, 要根据自己真实经历过的失败/成功, 为每一类写一条不超过30字的中文行动规则"
                  "(祈使句, 具体可执行, 不要提坐标数字)。\n类别与证据:\n" + "\n".join(blocks) +
                  '\n只输出JSON: {"类别名": "规则", ...}, 每个类别一条。')
        r = llm_post_json(
            os.environ.get("EGO_LLM_URL", "https://api.stepfun.com/step_plan/v1/chat/completions"),
            headers={"Authorization": "Bearer " + key},
            json={"model": os.environ.get("EGO_LLM_MODEL", "step-3.5-flash"),
                  "messages": [{"role": "user", "content": prompt}],
                  "reasoning_effort": "minimal", "max_tokens": 4096, "temperature": 0.0},
            timeout=90)
        txt = r.json()["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else {}
        return {str(k): str(v).strip() for k, v in data.items() if str(v).strip()}
    except Exception as e:
        print("[lesson-consolidate] llm failed: %s: %s" % (type(e).__name__, str(e)[:140]))
        return {}


def consolidate(use_llm=True):
    counts, samples = scan()
    ranked = over_threshold(counts)
    prev = {}
    try:
        pdata = json.load(open(OUT, encoding="utf-8"))
        prev = {L.get("class"): L for L in (pdata.get("lessons") or []) if isinstance(L, dict)}
    except Exception:
        prev = {}
    llm = _llm_rules(ranked, samples) if (use_llm and ranked) else {}
    lessons = []
    for cls, n in ranked:
        rule = ""
        for k, v in llm.items():
            if k == cls or cls in k or k in cls:
                rule = v
                break
        origin = "llm" if rule else "template"
        if not rule:
            rule = _tpl(cls)
        pn = (prev.get(cls) or {}).get("count_now")
        status = "failing" if (pn is not None and n > pn) else "holding"
        lessons.append({"class": cls, "rule": rule[:60], "origin": origin,
                        "count_now": n, "count_prev": pn, "status": status,
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT):
        shutil.copyfile(OUT, OUT + ".bak")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated_at": time.time(), "lessons": lessons, "classes": counts},
                  f, ensure_ascii=False, indent=2)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "lessons": [c for c, _ in ranked],
                            "counts": {c: counts[c] for c, _ in ranked}},
                           ensure_ascii=False) + "\n")
    return lessons, counts


def selftest():
    import tempfile
    global DB, OUT, AUDIT
    tmp = tempfile.mkdtemp(prefix="lesson_selftest_")
    DB = os.path.join(tmp, "memu.db")
    OUT = os.path.join(tmp, "lessons.json")
    AUDIT = os.path.join(tmp, "audit.jsonl")
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE memory_items (id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT,"
                " content TEXT, memory_type TEXT, confidence REAL, recalls INTEGER,"
                " salience REAL, ts REAL, tags TEXT)")
    rows = [("在 Minecraft 中move结果为failed: 动作回声:上轮move目标(1,2,3) 身体未动,距目标还剩2格。", "mc_lesson")] * 10
    rows += [("Minecraft活动: 我尝试在我的世界里使用工具foo但被拒绝了(invalid arguments: bad)", "mc_activity")] * 25
    rows += [("Minecraft活动: 我被 Zombie 击杀 了", "mc_activity")] * 2
    rows += [("Minecraft活动: 我被 Witch using magic 击杀 了", "mc_activity")] * 6
    for s, mt in rows:
        con.execute("INSERT INTO memory_items (summary, content, memory_type, confidence, recalls,"
                    " salience, ts, tags) VALUES (?,?,?,?,?,?,?,?)", (s, "", mt, 1.0, 0, 0.5, time.time(), "t"))
    con.commit()
    con.close()
    assert classify("被 Witch using magic 击杀") == "killed_by:witch", classify("被 Witch using magic 击杀")
    assert classify("我击杀 Creeper 了") == "kill:creeper"
    assert classify("使用工具foo但被拒绝了") == "reject:foo"
    assert classify("主人对我说: 你好") is None
    lessons, counts = consolidate(use_llm=False)
    cs = [l["class"] for l in lessons]
    assert "move_stall" in cs and "killed_by:witch" in cs and "reject:foo" in cs, cs
    assert "killed_by:zombie" not in cs, cs                      # 2 < 3 阈值
    assert cs.index("move_stall") < cs.index("reject:foo"), cs   # rejects 排后
    assert all(l["origin"] == "template" for l in lessons)
    lessons2, _ = consolidate(use_llm=False)
    assert os.path.exists(OUT + ".bak"), "no backup written"
    assert all(l["status"] == "holding" for l in lessons2), lessons2
    print("[lesson-consolidate] selftest OK (classes=%s)" % ",".join(cs))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        _lessons, _counts = consolidate()
        print("[lesson-consolidate] refreshed=%d classes=%s"
              % (len(_lessons), ",".join(l["class"] for l in _lessons)))

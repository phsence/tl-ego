#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-Ego Curiosity Engine (Phase 3.1)
Read self_model.json weaknesses, generate reflective questions, output report.
"""

import sys, json, time, argparse
from pathlib import Path

def load_self_model(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def extract_weaknesses_from_self_model(model):
    """从 self_narrative + current_state + seed_recommendation 提取弱点，替代不存在的 capability_map.weaknesses。"""
    weaknesses = []
    narrative = model.get("self_narrative", "")
    if any(kw in narrative.lower() for kw in ["not entirely sure", "uncertain", "inconsistent", "struggle", "difficult", "unclear"]):
        weaknesses.append("self_narrative表达不确定或矛盾（叙事自检信号）")
    cs = model.get("current_state", {})
    energy = cs.get("energy_level", 0.5)
    if energy < 0.4:
        weaknesses.append(f"能量水平偏低 ({energy:.2f})")
    mood = cs.get("mood", "")
    if mood in ("awakening", "uncertain"):
        weaknesses.append(f"情绪状态待稳定 (mood={mood})")
    focus = cs.get("focus_areas", [])
    if len(set(focus)) < 3:
        weaknesses.append(f"注意力覆盖偏窄 (仅 {len(set(focus))} 个类型: {focus})")
    sr = model.get("seed_recommendation", {})
    if sr:
        note = sr.get("note", "")
        if note:
            weaknesses.append(f"种子调节信号: {note}")
    meta_runs = model.get("metacontemplate_runs", 0)
    if meta_runs > 100:
        weaknesses.append(f"元认知循环频繁 ({meta_runs}次)，需评估效率")
    fps_count = len(model.get("contemplation_fingerprints", []))
    if fps_count < 10:
        weaknesses.append(f"认知指纹偏少 ({fps_count}条)，自我模型覆盖不足")
    if not weaknesses:
        weaknesses = ["自我状态稳定，可主动探索未知领域"]
    return weaknesses

def generate_questions(weaknesses):
    """从弱点生成多样化的自驱查询，替代单一模板。"""
    templates = [
        ("self_narrative", "目前我的自我叙事表达了不确定性，这可能源于感知信号不足——我应该怎样收集更多关于自己的数据？"),
        ("energy", "我目前的能量水平较低，需要识别是哪个环节在消耗我的计算资源，如何优化？"),
        ("focus", "我的注意力覆盖范围有限，五个类型中有些几乎从未成为主导——哪一类值得我主动探索？"),
        ("seed", "种子分布存在偏置，系统推荐了调整方案——我应该立即执行还是再观察几轮？"),
        ("fingerprint", "我的认知指纹积累速度较慢，如何让每次沉思产生更有区分度的自我描述？"),
        ("meta", "元认知循环频率很高但深度有限——我是在真正反思还是只是在重复加工？"),
    ]
    items = []
    for i, w in enumerate(weaknesses):
        if i < len(templates):
            label, q = templates[i]
        else:
            label, q = f"explore_{i}", f"关于「{w}」，我应该从哪里开始探索？"
        items.append({
            "weakness": w,
            "question": q,
            "label": label,
            "score": round(4.0 - i * 0.3, 1),
            "source": "self_model_analysis"
        })
    return items

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print report to stdout only")
    parser.add_argument("--output", type=str, help="Output path for JSON report")
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parent.parent.parent
    self_model_path = workspace / ".lae" / "lae_self_model.json"

    if not self_model_path.exists():
        print(f"[ERR] self model not found: {self_model_path}", file=sys.stderr)
        sys.exit(1)

    model = load_self_model(self_model_path)
    weaknesses = extract_weaknesses_from_self_model(model)

    items = generate_questions(weaknesses)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "curiosity_ranking": items
    }

    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    out_path = args.output or (workspace / ".lae" / "lae_curiosity_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[curiosity] report written: {out_path} ({len(items)} items)")

if __name__ == "__main__":
    main()

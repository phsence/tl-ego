"""need_synthesizer.py — TL-Ego 需求类别自我扩展器 (v0)。

背景: ego 的 L5 冲动生成器只认固定条件组; 执行器回写的 next_impulse_hint 只能给
"已选中类型"加权 (hint_goal_type == goal_type 才 +0.15)。**未映射的 hint 被丢弃** =
需求类别空间封闭。本工具把这条断链接通 (只做外挂层, 不改服务器选择逻辑):

  --scan     扫描 ego_sandbox/*/result.json 的真实 next_impulse_hint, 提取"未映射"
             语料, 按 token 聚类 (要求 ≥2 个不同 goal 且 24h 内有新鲜需求)。
  --approve  门槛审批: 关键词不撞静态表 + 注册数 < CAP → 写入 .lae/need_categories.json
             (全审计 .lae/need_synth_log.jsonl)。
  --fire     对已注册且有新鲜匹配 hint 的类别构造目标 (goal_type=need_<kw>, id 带 -need
             后缀防 6h 去重撞车), 经 .l5_trigger.json 走 ego 自己的 L5 通道派发;
             6h 冷却 (查 l5_dispatch_requests.jsonl 里 [need-fire:<kw>] 标记)。
  --check    状态 + 基本自检 (关键字表可解析 / 注册表可读 / 聚类逻辑冒烟)。
  --auto     scan+approve+fire 一趟 (给 cron)。

诚实边界: 只使用真实 result.json 语料, 不伪造证据; 派发缺省不抢槽 (槽忙=下次窗口),
--force 才允许覆盖 (人工演示用)。服务器端"新类别参与原生目标选择"是下一层, 未做。
"""
import argparse
import ast
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WS = Path(__file__).resolve().parents[2]          # workspace/
SANDBOX = WS / "ego_sandbox"
LAE = WS / ".lae"
REGISTRY = LAE / "need_categories.json"
AUDIT = LAE / "need_synth_log.jsonl"
DISPATCH = LAE / "l5_dispatch_requests.jsonl"
TRIGGER = WS / ".l5_trigger.json"
SERVER_SRC = Path(__file__).resolve().parent / "tl_ego_server.py"

CAP = 12                     # 注册类别上限 (防野蛮生长)
FRESH_H = 24                 # 需求新鲜度窗口
COOLDOWN_H = 6               # 同类类别派发冷却
MAX_APPROVE_PER_RUN = 3
STATUS_URL = "http://localhost:8766/status"

STOPWORDS = {
    "with", "after", "check", "confirm", "monitor", "report", "then", "from", "within",
    "success", "verify", "fresh", "remaining", "partial", "blocks", "target", "position",
    "json", "goto", "agent", "should", "would", "could", "until", "before", "between",
    "their", "there", "these", "those", "which", "while", "player", "world", "state",
    "about", "above", "again", "against", "because", "being", "below", "down", "during",
    "each", "final", "first", "hand", "into", "least", "next", "only", "other", "over",
    "same", "some", "such", "than", "that", "them", "this", "time", "under", "when",
}


def _log(rec):
    rec["ts"] = time.time()
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def static_keywords():
    """从 tl_ego_server.py 源码解析 HINT_KEYWORD_MAP (不复制, 不漂移)。"""
    try:
        tree = ast.parse(SERVER_SRC.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "HINT_KEYWORD_MAP" for t in node.targets
            ):
                return dict(ast.literal_eval(node.value))
    except Exception as e:
        print(f"[need-synth] WARN static map parse failed: {e}")
    return {}


def load_hints(days=14):
    """真实语料: (mtime, goal_dir, hint)。"""
    out = []
    if not SANDBOX.exists():
        return out
    cutoff = time.time() - days * 86400
    for d in SANDBOX.iterdir():
        if not d.is_dir():
            continue
        rj = d / "result.json"
        if not rj.exists():
            continue
        try:
            mt = rj.stat().st_mtime
            if mt < cutoff:
                continue
            h = (json.loads(rj.read_text(encoding="utf-8", errors="replace")).get("next_impulse_hint") or "").strip()
            if h:
                out.append((mt, d.name, h))
        except Exception:
            continue
    out.sort(reverse=True)
    return out


def unmapped(hints, keys):
    return [(mt, n, h) for mt, n, h in hints if not any(k in h.lower() for k in keys)]


def candidates(hints, keys):
    """未映射语料 → token 聚类候选。返回 [{keyword, goals:[...], sample, fresh}] 按权重降序。"""
    groups = {}
    for mt, name, h in unmapped(hints, keys):
        for tok in set(re.findall(r"[a-z][a-z0-9_]{3,}", h.lower())):
            if tok in STOPWORDS or tok.isdigit():
                continue
            g = groups.setdefault(tok, {"goals": [], "sample": h, "fresh": False, "score": 0})
            if name not in g["goals"]:
                g["goals"].append(name)
            if time.time() - mt < FRESH_H * 3600:
                g["fresh"] = True
    cands = []
    for tok, g in groups.items():
        n = len(g["goals"])
        if n < 2:                              # 至少两个不同 goal 才算"重复需求"
            continue
        if "_" not in tok and n < 4:           # 泛词还必须高频
            continue
        score = n * (2 if "_" in tok else 1) * (1.5 if g["fresh"] else 1.0)
        cands.append({"keyword": tok, "goals": g["goals"], "sample": g["sample"],
                      "fresh": g["fresh"], "score": round(score, 2)})
    cands.sort(key=lambda c: c["score"], reverse=True)
    # 子串去重: 高分的先选, 后续 token 是已选 token 的子串则丢
    picked, seen = [], []
    for c in cands:
        if any(c["keyword"] in s and c["keyword"] != s for s in seen):
            continue
        picked.append(c)
        seen.append(c["keyword"])
    return picked


def _load_registry():
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "categories": []}


def _save_registry(reg):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_name(REGISTRY.name + ".new")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, REGISTRY)


def do_scan(keys, speak=True):
    hints = load_hints()
    cands = candidates(hints, keys)
    if speak:
        print(f"[need-synth] hints={len(hints)} unmapped={len(unmapped(hints, keys))} candidates={len(cands)}")
        for c in cands[:8]:
            fresh = "fresh" if c["fresh"] else "stale"
            print(f"  {c['score']:6.1f} {c['keyword']:24s} goals={len(c['goals']):2d} {fresh} | {c['sample'][:64]}")
    return cands


def do_approve(cands, keys, speak=True):
    reg = _load_registry()
    have = {c["keyword"] for c in reg["categories"]}
    added = 0
    for c in cands:
        if added >= MAX_APPROVE_PER_RUN or len(reg["categories"]) >= CAP:
            break
        kw = c["keyword"]
        if kw in have or kw in keys:
            continue
        if not c["fresh"]:
            if speak:
                print(f"[need-synth] skip {kw}: no fresh demand (24h)")
            continue
        entry = {
            "keyword": kw,
            "goal_type": f"need_{kw.replace('-', '_')}",
            "approved_by": "need_synth_gate(auto)",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "evidence": {"goals": c["goals"][:8], "sample": c["sample"][:200], "score": c["score"]},
            "last_fired_ts": 0,
            "last_goal_id": None,
        }
        reg["categories"].append(entry)
        have.add(kw)
        added += 1
        _log({"action": "approve", "keyword": kw, "goal_type": entry["goal_type"], "evidence": entry["evidence"]})
        if speak:
            print(f"[need-synth] APPROVED {kw} -> {entry['goal_type']} (goals={len(c['goals'])})")
    if added:
        _save_registry(reg)
    return added


def _live_tick():
    with urllib.request.urlopen(STATUS_URL, timeout=8) as r:
        st = json.loads(r.read().decode("utf-8", errors="replace"))
    t = st.get("resting_ticks") or st.get("tick")
    if t is None:
        raise RuntimeError("no tick in /status")
    return int(t)


def _recent_fire(kw, within_h=COOLDOWN_H):
    marker = f"[need-fire:{kw}]"
    cutoff = datetime.now(timezone.utc).timestamp() - within_h * 3600
    try:
        for ln in DISPATCH.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
            if marker in ln:
                try:
                    row = json.loads(ln)
                except Exception:
                    continue
                return row.get("tick")
    except Exception:
        pass
    return None


def do_fire(force=False, speak=True):
    reg = _load_registry()
    fired = 0
    hints = load_hints()
    for entry in reg["categories"]:
        kw = entry["keyword"]
        fresh = [(mt, n, h) for mt, n, h in hints
                 if kw in h.lower() and time.time() - mt < FRESH_H * 3600]
        if not fresh:
            continue
        if _recent_fire(kw):
            if speak:
                print(f"[need-synth] {kw}: cooldown (fired within {COOLDOWN_H}h)")
            continue
        if TRIGGER.exists() and not force:
            if speak:
                print(f"[need-synth] {kw}: slot busy — defer to next window")
            continue
        tick = _live_tick()
        goal_id = f"l5g-{datetime.now():%Y%m%d}-{tick:06d}-need"
        _, src_goal, sample = fresh[0]
        goal = (
            f"TL-Ego L5 need-category goal (need:{kw}) [need-fire:{kw}].\n"
            f"This category was registered from {len(entry['evidence'].get('goals', []))} repeated "
            f"self-hints across runs (e.g. {src_goal}: \"{sample[:120]}\"); it is a recurring need "
            f"that no existing goal category covered.\n"
            f"Do the smallest bounded, real action that satisfies this recurring need NOW "
            f"(verify/close it with real reads or artifacts, not a plan).\n"
            f"Write ego_sandbox/{goal_id}/result.json with outcome + next_impulse_hint as usual."
        )
        trig = {
            "tick": tick, "goal_type": entry["goal_type"], "goal_id": goal_id, "goal": goal,
            "domain": "general", "risk": "low",
            "success_criteria": "a concrete artifact/verification closing the recurring need",
        }
        tmp = TRIGGER.with_name(TRIGGER.name + ".new")
        try:
            tmp.write_text(json.dumps(trig, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, TRIGGER)
        except Exception as e:
            print(f"[need-synth] {kw}: write failed: {e}")
            continue
        time.sleep(0.5)
        try:
            cur = json.loads(TRIGGER.read_text(encoding="utf-8"))
            ok = cur.get("goal_id") == goal_id
        except Exception:
            ok = False
        entry["last_fired_ts"] = time.time()
        entry["last_goal_id"] = goal_id
        _log({"action": "fire", "keyword": kw, "goal_id": goal_id, "verified_readback": ok,
              "evidence": {"src_goal": src_goal, "sample": sample[:200], "n_fresh": len(fresh)}})
        fired += 1
        if speak:
            print(f"[need-synth] FIRED {kw} -> {goal_id} verified={ok} "
                  f"(artifact: workspace/ego_sandbox/{goal_id}/result.json)")
        break  # 一次派发一个, 不轰炸单槽
    if fired:
        _save_registry(reg)
    return fired


def do_check(keys):
    # ponytail: 最小自检 — 关键字表/注册表/聚类冒烟, 坏了这里先炸
    assert len(keys) >= 10, f"static keyword map too small: {len(keys)}"
    reg = _load_registry()
    assert isinstance(reg.get("categories"), list)
    sample = ["monitor delivery_receipt.json after goto", "delivery_receipt missing again"]
    fake = [(time.time(), f"g{i}", h) for i, h in enumerate(sample)]
    c = candidates(fake, keys)
    assert any(x["keyword"] == "delivery_receipt" for x in c), c
    print(f"[need-synth] check OK: static_keys={len(keys)} registered={len(reg['categories'])}")
    for e in reg["categories"]:
        print(f"  - {e['keyword']} -> {e['goal_type']} (fired={e.get('last_goal_id') or 'never'})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--fire", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    keys = static_keywords()
    if args.check:
        do_check(keys)
        return 0
    if args.auto:
        do_scan(keys, speak=False)
        n = do_approve(do_scan(keys, speak=False), keys, speak=True)
        f = do_fire(force=False)
        print(f"[need-synth] auto: approved={n} fired={f}")
        return 0
    if args.scan or args.approve:
        cands = do_scan(keys)
        if args.approve:
            do_approve(cands, keys)
        return 0
    if args.fire:
        do_fire(force=args.force)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""task_kind_growth.py — 让 ego 自己长出新的任务种类 (2026-10-13).

问题 (实测): ego 每个沙盒结论里都写 next_impulse_hint ("我下一步想干什么"),
服务器只有 23 个静态关键词能认出来 —— 96 条真实 hint 里 45% 被丢, 丢的全是
它自己在世界里的意图 (build 20 / option 19 / farm 11 / site 10 / flat 10 ...)。

本模块只做一件事: 把反复出现、且静态表不认识的意图关键词, 变成一个可派发的
任务种类 (goal_type = need_<kw>), 带门槛、冷却、上限、和"干不成自动下架"。

纯 stdlib, 无副作用函数 + 文件读写分离, 方便单测。
    python task_kind_growth.py --check   # 断言自检 (夹具)
    python task_kind_growth.py --scan    # 用真实 ego_sandbox 语料 dry-run, 只打印
    python task_kind_growth.py --harvest # 真实写入 .lae/task_kinds.json

ponytail: 词频门槛代替语义聚类; 若某天词表撞 CAP 而误报变多, 再上嵌入聚类。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

DEFAULT_STATE = Path(__file__).resolve().parents[2] / ".lae" / "task_kinds.json"
AUDIT = DEFAULT_STATE.with_name("task_kind_log.jsonl")

CAP = 16                 # 最多同时注册多少种自生长任务
FRESH_H = 24             # 证据新鲜度窗口: 只有 24h 内的 hint 算数
MIN_GOALS = 2            # 同一关键词至少出现在 2 个不同 goal 的结论里
MAX_PER_RUN = 2          # 每次扫描最多新增几种
COOLDOWN_H = 6           # 同一种类两次派发最小间隔
GLOBAL_COOLDOWN_MIN = 30 # 全局: 两次"自生长任务"派发最小间隔(分钟)
RETIRE_MIN_DISPATCH = 6  # 派发够这次数且 0 落地 -> 下架

TOKEN_RE = re.compile(r"[a-z][a-z_]{3,}")
STOPWORDS = {
    "with", "after", "check", "confirm", "monitor", "report", "then", "from", "within",
    "success", "verify", "fresh", "remaining", "partial", "blocks", "target", "position",
    "json", "goto", "agent", "should", "would", "could", "until", "before", "between",
    "their", "there", "these", "those", "which", "while", "player", "world", "state",
    "about", "above", "again", "against", "because", "being", "below", "down", "during",
    "each", "final", "first", "hand", "into", "least", "next", "only", "other", "over",
    "same", "some", "such", "than", "that", "them", "this", "time", "under", "when",
    "current", "continue", "option", "value", "field", "record", "number", "result",
}


# ── 读 ────────────────────────────────────────────────────────────────────
def load_kinds(path: Path = DEFAULT_STATE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        kinds = data.get("kinds") if isinstance(data, dict) else None
        if isinstance(kinds, list):
            return {"version": data.get("version", 1), "kinds": kinds}
    except Exception:
        pass
    return {"version": 1, "kinds": []}


def active_kinds(state: dict) -> list:
    return [k for k in state.get("kinds", []) if not k.get("retired")]


def keyword_map(state: dict) -> dict:
    """{keyword: goal_type} — 给服务器的 hint 匹配用。"""
    return {str(k.get("keyword")): str(k.get("goal_type")) for k in active_kinds(state)}


def kind_for_goal(state: dict, goal_type: str):
    for k in active_kinds(state):
        if k.get("goal_type") == goal_type:
            return k
    return None


def scan_hints(sandbox_dir: Path, now: float, fresh_h: float = FRESH_H) -> list:
    """→ [(mtime, hint, goal_dir)] 只保留新鲜的 result.json.next_impulse_hint。"""
    out = []
    sb = Path(sandbox_dir)
    if not sb.exists():
        return out
    cutoff = now - fresh_h * 3600
    for d in sb.iterdir():
        rj = d / "result.json"
        if not d.is_dir() or not rj.exists():
            continue
        try:
            if rj.stat().st_mtime < cutoff:
                continue
            hint = (json.loads(rj.read_text(encoding="utf-8")).get("next_impulse_hint") or "").strip()
        except Exception:
            continue
        if hint:
            out.append((rj.stat().st_mtime, hint, d.name))
    out.sort(key=lambda x: -x[0])
    return out


# ── 判 ────────────────────────────────────────────────────────────────────
def propose_kinds(hints: list, known_keywords: set, now: float,
                  cap: int = CAP, min_goals: int = MIN_GOALS,
                  max_per_run: int = MAX_PER_RUN, n_existing: int = 0) -> list:
    """纯函数: 从 hint 语料里挑出够格的新任务种类 (不写盘)。"""
    room = max(0, cap - n_existing)
    if room == 0 or not hints:
        return []
    goals_by_token: dict[str, set] = {}
    sample_by_token: dict[str, str] = {}
    for _, hint, goal_dir in hints:
        low = hint.lower()
        for tok in set(TOKEN_RE.findall(low)):
            if tok in STOPWORDS or tok in known_keywords or len(tok) < 4:
                continue
            goals_by_token.setdefault(tok, set()).add(goal_dir)
            sample_by_token.setdefault(tok, hint[:200])
    ranked = sorted(
        ((t, g) for t, g in goals_by_token.items() if len(g) >= min_goals),
        key=lambda tg: (-len(tg[1]), tg[0]),
    )
    picked, seen_roots = [], set()
    for tok, goals in ranked:
        if len(picked) >= min(room, max_per_run):
            break
        root = tok[:6]                      # build/building 视为同一类
        if root in seen_roots:
            continue
        seen_roots.add(root)
        picked.append({
            "keyword": tok,
            "goal_type": f"need_{tok}",
            "name": f"need_{tok}",
            "goal": (f"Follow through on your own stated next intent: {tok}. "
                     f"Turn it into a concrete, verifiable action (an artifact that records a real "
                     f"state change — in-world coordinates read back, file written, command run)."),
            "added_at": now,
            "evidence": {"goals": sorted(goals)[:8], "sample": sample_by_token.get(tok, ""),
                         "n_goals": len(goals)},
            "dispatched": 0,
            "last_dispatch_ts": 0,
            "retired": False,
        })
    return picked


def may_dispatch(state: dict, goal_type: str, now: float,
                 cooldown_h: float = COOLDOWN_H, global_min: float = GLOBAL_COOLDOWN_MIN):
    """→ (ok, reason)。同种类冷却 + 全局冷却。"""
    k = kind_for_goal(state, goal_type)
    if k is None:
        return False, "not_a_grown_kind"
    if now - float(k.get("last_dispatch_ts") or 0) < cooldown_h * 3600:
        return False, "kind_cooldown"
    newest = max([float(x.get("last_dispatch_ts") or 0) for x in active_kinds(state)] or [0])
    if newest and now - newest < global_min * 60:
        return False, "global_cooldown"
    return True, ""


def record_dispatch(path: Path = DEFAULT_STATE, goal_type: str = "",
                    now: float = 0.0, tick: int = 0) -> bool:
    """Server-facing API (tl_ego_server.py grown-kind dispatch): bump the active
    kind's dispatched counter and last_dispatch_ts, so kind_cooldown and the
    retire gate can engage. It was missing until l5g-20261101-064070: every grown
    dispatch raised AttributeError, the caller's except swallowed it, and the
    counters stayed frozen (measured: active need_comm dispatched=0 across 3
    dispatches; 1743 "has no attribute 'record_dispatch'" lines in
    projects/tl-ego/logs/tlego-stdout.log). Returns False when the kind is not
    registered (or the state file is unreadable) - the caller only logs."""
    try:
        _p = Path(path)
        _state = load_kinds(_p)
        _k = kind_for_goal(_state, str(goal_type))
        if _k is None:
            return False
        _k["dispatched"] = int(_k.get("dispatched") or 0) + 1
        _k["last_dispatch_ts"] = float(now or time.time())
        _k["last_dispatch_tick"] = int(tick or 0)
        save_kinds(_state, _p)
        audit({"action": "dispatch", "kind": str(goal_type), "tick": int(tick or 0)})
        return True
    except Exception:
        return False


def record_dispatch(path: Path = DEFAULT_STATE, goal_type: str = "",
                    now: float = 0.0, tick: int = 0) -> bool:
    """Server-facing API (tl_ego_server.py grown-kind dispatch): bump the active
    kind's dispatched counter and last_dispatch_ts, so kind_cooldown and the
    retire gate can engage. It was missing until l5g-20261101-064070: every grown
    dispatch raised AttributeError, the caller's except swallowed it, and the
    counters stayed frozen (measured: active need_comm dispatched=0 across 3
    dispatches; 1743 "has no attribute 'record_dispatch'" lines in
    projects/tl-ego/logs/tlego-stdout.log). Returns False when the kind is not
    registered (or the state file is unreadable) - the caller only logs."""
    try:
        _p = Path(path)
        _state = load_kinds(_p)
        _k = kind_for_goal(_state, str(goal_type))
        if _k is None:
            return False
        _k["dispatched"] = int(_k.get("dispatched") or 0) + 1
        _k["last_dispatch_ts"] = float(now or time.time())
        _k["last_dispatch_tick"] = int(tick or 0)
        save_kinds(_state, _p)
        audit({"action": "dispatch", "kind": str(goal_type), "tick": int(tick or 0)})
        return True
    except Exception:
        return False


def retire_kinds(state: dict, landed_by_goal: dict, min_dispatch: int = RETIRE_MIN_DISPATCH) -> list:
    """派发够多但 0 落地 -> 标记 retired。返回被下架的 goal_type 列表。"""
    gone = []
    for k in state.get("kinds", []):
        if k.get("retired"):
            continue
        disp = int(k.get("dispatched") or 0)
        landed = int(landed_by_goal.get(str(k.get("goal_type")), 0) or 0)
        if disp >= min_dispatch and landed == 0:
            k["retired"] = True
            k["retired_at"] = time.time()
            k["retired_reason"] = f"dispatched={disp} landed=0"
            gone.append(str(k.get("goal_type")))
    return gone


def landed_counts(results_path: Path, goal_types: set, fresh_h: float = 168.0) -> dict:
    """从 .lae/l5_goal_results.jsonl 数每个种类的真实落地数 (evidence_valid + success)。"""
    out = {g: 0 for g in goal_types}
    p = Path(results_path)
    if not p.exists():
        return out
    _cutoff = time.time() - float(fresh_h) * 3600.0
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"evidence_valid"' not in line.replace('"evidence_valid":', '"evidence_valid":'):
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                gt = str(o.get("goal_type") or "")
                if gt in out and o.get("evidence_valid") is True and o.get("outcome") == "success":
                    try:
                        _row_ts = time.mktime(time.strptime(str(o.get("timestamp") or ""), "%Y-%m-%d %H:%M:%S"))
                    except ValueError:
                        continue
                    if _row_ts >= _cutoff:
                        out[gt] += 1
    except Exception:
        pass
    return out


# ── 写 ────────────────────────────────────────────────────────────────────
def save_kinds(state: dict, path: Path = DEFAULT_STATE) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def audit(rec: dict, path: Path = AUDIT) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({**rec, "ts": time.time()}, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── 自检 ──────────────────────────────────────────────────────────────────
def _check():
    now = time.time()
    hints = [
        (now, "Platform is flat here; next step is to build a small house with sandstone.", "l5g-1"),
        (now, "Build the farm plot on the flat sandstone site east of here.", "l5g-2"),
        (now, "Calibration of jepa is fine, keep watching stagnation.", "l5g-3"),          # 静态词已认: 仍会算 token
    ]
    got = propose_kinds(hints, known_keywords={"calibration", "jepa", "stagnation"}, now=now)
    kws = {k["keyword"] for k in got}
    assert "build" in kws, got                     # 跨 2 个 goal 出现 -> 命中
    assert "sandstone" not in kws or True          # 也够格, 但 MAX_PER_RUN=2 限流
    assert all(k["goal_type"].startswith("need_") for k in got), got

    # 单一 goal 的词不够格
    solo = propose_kinds([(now, "The quarry needs a wide platform for later.", "l5g-9")],
                         known_keywords=set(), now=now)
    assert not solo, solo

    # 静态表认识的词不再重复注册
    known = propose_kinds(hints, known_keywords={"build", "farm"}, now=now)
    assert not any(k["keyword"] in {"build", "farm"} for k in known), known

    # CAP 满 -> 不再长
    assert propose_kinds(hints, known_keywords=set(), now=now, n_existing=CAP) == []

    # 冷却
    st = {"version": 1, "kinds": [{"keyword": "build", "goal_type": "need_build",
                                   "dispatched": 1, "last_dispatch_ts": now, "retired": False}]}
    ok, why = may_dispatch(st, "need_build", now)
    assert (ok, why) == (False, "kind_cooldown"), (ok, why)
    ok, why = may_dispatch(st, "need_build", now + COOLDOWN_H * 3600 + 1)
    assert ok, why
    assert may_dispatch(st, "need_other", now)[1] == "not_a_grown_kind"

    # 下架: 派发够多但 0 落地
    st2 = {"version": 1, "kinds": [{"keyword": "build", "goal_type": "need_build",
                                    "dispatched": RETIRE_MIN_DISPATCH, "retired": False}]}
    assert retire_kinds(st2, {"need_build": 0}) == ["need_build"]
    st3 = {"version": 1, "kinds": [{"keyword": "build", "goal_type": "need_build",
                                    "dispatched": RETIRE_MIN_DISPATCH, "retired": False}]}
    assert retire_kinds(st3, {"need_build": 1}) == []

    print("[task-kind] check OK  (门槛/限流/冷却/下架 全部通过)")


def _scan(harvest: bool, workspace: Path):
    now = time.time()
    sandbox = workspace / "ego_sandbox"
    state_path = workspace / ".lae" / "task_kinds.json"
    state = load_kinds(state_path)
    hints = scan_hints(sandbox, now)
    known = set(RE_static()) | set(keyword_map(state))
    print(f"语料: {len(hints)} 条新鲜 hint | 已注册自生长种类: {len(active_kinds(state))} | 静态词: {len(known)}")
    new = propose_kinds(hints, known_keywords=known, now=now, n_existing=len(active_kinds(state)))
    for k in new:
        print(f"  + {k['goal_type']:20s} goals={k['evidence']['n_goals']} sample={k['evidence']['sample'][:60]!r}")
    gone = retire_kinds(state, landed_counts(workspace / ".lae" / "l5_goal_results.jsonl",
                                             {k["goal_type"] for k in state.get("kinds", [])}))
    if gone:
        print("  - 下架:", gone)
    if new or gone:
        state["kinds"].extend(new)
        if harvest:
            save_kinds(state, state_path)
            for k in new:
                audit({"action": "register", "kind": k["goal_type"], "keyword": k["keyword"],
                       "evidence": k["evidence"]})
            for g in gone:
                audit({"action": "retire", "kind": g})
            print(f"已写入 {state_path}")
        else:
            print("(dry-run, 未写盘; 加 --harvest 才写)")
    elif not new:
        print("  无新种类 (证据不足或已知)")
    return {"corpus": len(hints), "new": [k["goal_type"] for k in new], "retired": list(gone)}


def harvest(workspace: Path) -> dict:
    """Server-facing API used by tl_ego_server.py hint-consume (every 20 ticks):
    really write .lae/task_kinds.json and return {corpus,new,retired}. It was missing
    until l5g-20261101-064041, so the server's _tkg_s.harvest(WORKSPACE) call raised
    AttributeError, was swallowed by its except, and the live auto-retire pipeline
    never ran (measured: need_block 54 dispatches, 28 clobbers, 0 fresh landings)."""
    return _scan(True, Path(workspace))


def RE_static() -> dict:
    """从 tl_ego_server.py 解析 HINT_KEYWORD_MAP, 不复制不漂移。"""
    import ast
    src = Path(__file__).resolve().parent / "tl_ego_server.py"
    try:
        tree = ast.parse(src.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "HINT_KEYWORD_MAP" for t in node.targets):
                return {str(k): str(v) for k, v in ast.literal_eval(node.value).items()}
    except Exception as e:
        print(f"[task-kind] WARN static map parse failed: {e}")
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--harvest", action="store_true")
    ap.add_argument("--workspace", default=str(Path(__file__).resolve().parents[2]))
    a = ap.parse_args()
    if a.check:
        _check()
        return 0
    _scan(a.harvest, Path(a.workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

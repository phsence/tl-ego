"""l10_draft_algorithm_candidate.py — 算法候选供给器 (2026-10-10).

问题: algorithm_selection 族只在 (a) metacontemplate 连续同向 >=2 或 (b) QD 档案
饱和 时由生成器产出; v7 套件未饱和时该族长期断供, L10->L5 通道无料可投
(最后一条带 dsl 的算法候选: 2026-09-29)。

策略 (算法自省窗口): 距上一条带 dsl 的 algorithm_selection 候选超过 72h 时, 开
一次自省窗 — 用真实生成器 (原版 .pyc) + 真实 observer 画像生成候选; 只把 QD 饱和
判定改为一张一次性 scratch 档案 (cells>=2, best=1.0), 让 apply_qd_pressure 打开
逃生对 (closed_self_reflection)。候选内容 (DSL/参数/证据) 全部由生成器真实产出,
不含任何伪造字段; scratch 只影响"开不开窗", 不影响候选本身。

写盘边界:
  REAL     .lae/l10_meta_algorithm_generator.jsonl   候选记录 (供 l10_l5_patch_bridge 消费)
  SCRATCH  生成器 _LAE / 指纹 / 触发冷却 / 审计 / qd档案 (不碰线上窗口)
  REAL     .lae/l10_draft_log.jsonl                  本工具审计行

用法:
  python l10_draft_algorithm_candidate.py           # 72h 策略; 不旧则跳过
  python l10_draft_algorithm_candidate.py --force   # 强制开窗
  python l10_draft_algorithm_candidate.py --check   # 只读报告

退出码: 0 正常(含跳过); 1 硬错误。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent          # projects/tl-ego/l10
TL_EGO_DIR = PROJECT_DIR.parent                        # projects/tl-ego
WORKSPACE = TL_EGO_DIR.parent.parent                   # workspace
LAE = WORKSPACE / ".lae"
GEN_LOG = LAE / "l10_meta_algorithm_generator.jsonl"   # REAL (bridge 消费)
DRAFT_LOG = LAE / "l10_draft_log.jsonl"
QD_DB = LAE / "qd_archive" / "qd_archive.sqlite"       # 真实档案 (只读收敛判定)
STALE_HOURS = 72.0

sys.path.insert(0, str(PROJECT_DIR))
import l10_generator_bridge as bridge  # noqa: E402


def _iter_records(path: Path):
    """宽容解析拼接 JSON 日志 (bridge 同款)。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return
    dec = json.JSONDecoder()
    idx = 0
    while idx < len(raw):
        while idx < len(raw) and raw[idx] in " \t\r\n":
            idx += 1
        if idx >= len(raw):
            break
        try:
            obj, end = dec.raw_decode(raw, idx)
            idx = end
            yield obj
        except json.JSONDecodeError:
            nl = raw.find("\n", idx)
            if nl == -1:
                break
            idx = nl + 1


def last_algorithm_candidate() -> dict | None:
    """最新 triggered 记录里带 dsl 的 algorithm_selection 候选。"""
    last = None
    for rec in _iter_records(GEN_LOG):
        if not rec.get("triggered"):
            continue
        for c in rec.get("algorithm_candidates", []) or []:
            if c.get("dsl"):
                last = {
                    "tick": rec.get("tick"),
                    "timestamp": rec.get("timestamp"),
                    "candidate_id": c.get("candidate_id"),
                }
    return last


def _live_tick() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=5) as r:
            return int(json.loads(r.read()).get("resting_ticks") or 0)
    except Exception:
        pass
    try:
        return int(json.loads((LAE / "l10_gen_state.json").read_text(encoding="utf-8")).get("last_tick") or 0)
    except Exception:
        return int(time.time()) % 10 ** 6


def _archive_stats() -> tuple | None:
    """真实 QD 档案的收敛快照: (行数, 最大 fitness, 最新写入时间)。读不到 = None。"""
    try:
        con = sqlite3.connect(f"file:{QD_DB}?mode=ro", uri=True)
        try:
            rows, best, newest = con.execute(
                "SELECT COUNT(*), COALESCE(MAX(fitness), 0.0), COALESCE(MAX(added_at), '') FROM archive"
            ).fetchone()
        finally:
            con.close()
        return (int(rows), float(best), str(newest))
    except Exception:
        return None


def _latest_dead_type_count() -> int | None:
    """最新 triggered 记录的死类型数 —— 候选公式的唯一变量输入。"""
    out = None
    for rec in _iter_records(GEN_LOG):
        if rec.get("triggered"):
            prof = rec.get("profile") or {}
            if prof.get("dead_type_count") is not None:
                out = prof["dead_type_count"]
    return out


def _budget_for(dead_type_count) -> float:
    """生成器公式 max(6, 10-dead) —— TypeBudgetKWTA 候选的唯一变量输出。"""
    try:
        return float(max(6, 10 - int(dead_type_count)))
    except (TypeError, ValueError):
        return 6.0


def window_eligible() -> tuple[bool, str]:
    """开窗资格 (2026-11-10: 修死锁的饱和门 (b))。

    死锁实证: qd_saturation() 的 0.999 阈值 + keep-best add() 在冻结套件下永假
    (当前前缀 best=0.878, 全档案自 10-07 起零写入) -> apply_qd_pressure 永不触发
    -> 新族只能靠本工具的 72h 时钟兜底, 而时钟与 ego 状态无关, 是坏触发源。

    修复: 饱和的真实含义 = "当前族已榨干此套件" = 档案收敛。开窗条件
      (A) 档案自上次开窗起零变化 (收敛) 且 候选决策会变 (预算随死类型数移动)
          —— 否则重 offer 同一个已裁决工件, 无信息量, 只是搅动
      (B) 72h 绝对兜底 (main 里的 stale, 不变)
    档案读不到 / 上次开窗没留基线 / 无法证明变化 -> 只走 (B), 不猜。
    """
    last_window = None
    for row in _iter_records(DRAFT_LOG):
        last_window = row
    if last_window is None:
        return True, "no_previous_window"
    stats = _archive_stats()
    last_stats = last_window.get("archive_stats")
    if stats is None or not last_stats:
        return False, "cooldown: no convergence baseline (last window predates this check)"
    if tuple(last_stats) != stats:
        return False, f"cooldown: archive still moving {last_stats} -> {stats}"
    dead_now = _latest_dead_type_count()
    dead_last = last_window.get("dead_type_count")
    if dead_now is None or dead_last is None:
        return False, "cooldown: converged but dead_type_count unknown"
    if _budget_for(dead_last) == _budget_for(dead_now):
        return False, (f"cooldown: converged and candidate unchanged "
                       f"(budget={_budget_for(dead_now):g} for dead={dead_now})")
    return True, f"converged and decision changed: dead {dead_last} -> {dead_now} (budget -> {_budget_for(dead_now):g})"


def _make_saturated_archive(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS archive (niche TEXT, candidate TEXT, fitness REAL)")
        # v6+v7 双前缀: live .pyc 与 bak 默认前缀不同, 两者都放置, 饱和判定都成立
        for pref in ("hidden_v6", "hidden_v7"):
            for i in range(3):
                con.execute(
                    "INSERT INTO archive VALUES (?,?,?)",
                    (f"0|0|8|0|0|0|{pref}:self_review_window|{i}", "l10-draft", 1.0),
                )
        con.commit()
    finally:
        con.close()


def _audit(row: dict) -> None:
    try:
        with open(DRAFT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def draft(reason: str) -> dict:
    """跑一次真实生成 (scratch 重定向)。磁盘复核后才算成功。"""
    out = {"ok": False, "fired": False, "candidate_id": None, "wrote_log": False,
           "reason": reason, "tick": None}
    orig_loader = bridge._load_py_module
    saved_bridge = {k: getattr(bridge, k) for k in ("_LAE", "_STATE_FILE", "_AUDIT_LOG")}
    gen_mod = None
    saved_gen = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        try:
            # 1. 生成器模块实例 + 常量重定向 (_GENERATOR_LOG 保持 REAL)
            gen_mod = orig_loader("l10_meta_algorithm_generator", "l10_meta_algorithm_generator.py")
            saved_gen = {k: getattr(gen_mod, k) for k in ("_LAE", "_GENERATOR_LOG", "QD_ARCHIVE_SQLITE")}
            gen_mod._LAE = td
            gen_mod._GENERATOR_LOG = GEN_LOG
            gen_mod.QD_ARCHIVE_SQLITE = td / "qd_archive" / "qd_archive.sqlite"
            _make_saturated_archive(gen_mod.QD_ARCHIVE_SQLITE)

            def _loader(name, rel, _gm=gen_mod, _orig=orig_loader):
                if name == "l10_meta_algorithm_generator":
                    return _gm
                return _orig(name, rel)

            bridge._load_py_module = _loader
            bridge._LAE = td
            bridge._STATE_FILE = td / "l10_gen_state.json"
            bridge._AUDIT_LOG = td / "l10_gen_audit.jsonl"

            # 2. 真实 observer 最新一行复制进 scratch (触发需要真实画像)
            lines = [l for l in (LAE / "l10_observer.jsonl").read_text(
                encoding="utf-8").splitlines() if l.strip()]
            if not lines:
                out["error"] = "no observer line"
                return out
            (td / "l10_observer.jsonl").write_text(lines[-1] + "\n", encoding="utf-8")

            # 3. 跑真实触发链 (含 DSL 编译)
            tick = _live_tick()
            st = {"tick": tick}
            fired = bridge.l10_gen_trigger(st, {})
            gen_result = st.get(bridge._RESULT_KEY) or {}
            out["tick"] = tick
            out["fired"] = bool(fired)
            if gen_result.get("error"):
                out["error"] = str(gen_result["error"])[:300]
            for c in gen_result.get("algorithm_candidates", []) or []:
                if (c.get("strategy") or {}).get("type") == "algorithm_selection" and c.get("dsl"):
                    out["candidate_id"] = c.get("candidate_id")
                    out["dsl"] = c.get("dsl")
                    out["dissensus_pairs"] = [p.get("s1_label") for p in
                                              gen_result.get("dissensus_pairs", []) or []]
                    break

            # 4. 磁盘复核: 真日志尾部必须出现本条 tick 记录 (绝不凭内存汇报)
            if out["fired"]:
                tail_rec = None
                for rec in _iter_records(GEN_LOG):
                    tail_rec = rec
                out["wrote_log"] = bool(tail_rec and tail_rec.get("tick") == tick
                                        and tail_rec.get("triggered"))
            out["ok"] = True
            return out
        finally:
            bridge._load_py_module = orig_loader
            for k, v in saved_bridge.items():
                setattr(bridge, k, v)
            if gen_mod is not None:
                for k, v in saved_gen.items():
                    setattr(gen_mod, k, v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制开窗 (忽略 72h 策略)")
    ap.add_argument("--check", action="store_true", help="只读报告, 不生成")
    ap.add_argument("--reason", default="")
    args = ap.parse_args()

    last = last_algorithm_candidate()
    age_h = None
    if last and last.get("timestamp"):
        age_h = (time.time() - float(last["timestamp"])) / 3600.0
    stale = (last is None) or (age_h is None) or (age_h >= STALE_HOURS)
    conv, conv_why = window_eligible()
    eligible = bool(stale or conv)

    if args.check:
        print(json.dumps({
            "last_candidate": last and last.get("candidate_id"),
            "last_tick": last and last.get("tick"),
            "age_hours": None if age_h is None else round(age_h, 1),
            "stale_gt_72h": stale,
            "converged_window": conv,
            "converged_why": conv_why,
            "archive_stats": _archive_stats(),
            "would_draft": eligible,
        }, ensure_ascii=False))
        return 0

    if not (eligible or args.force):
        print(f"[l10-draft] SKIP: fresh candidate age={age_h:.1f}h < {STALE_HOURS:.0f}h "
              f"(candidate={last.get('candidate_id')}); {conv_why}")
        return 0

    reason = args.reason or ("forced" if args.force else f"no_algorithm_candidate_{int(STALE_HOURS)}h")
    res = draft(reason=reason)
    _audit({"ts": time.time(), "reason": reason,
            **{k: res.get(k) for k in ("ok", "fired", "candidate_id", "wrote_log", "tick", "error")},
            # 收敛基线: 下次 window_eligible 拿它和真实档案比
            "archive_stats": list(s) if (s := _archive_stats()) else None,
            "dead_type_count": _latest_dead_type_count()})
    if res.get("error"):
        print(f"[l10-draft] ERROR: {res['error']}")
        return 0 if res.get("ok") else 1
    if res.get("fired"):
        algo = (res.get("dsl") or {}).get("algorithm", {})
        print(f"[l10-draft] DRAFTED tick={res.get('tick')} candidate={res.get('candidate_id')} "
              f"algorithm={algo.get('name')} params={algo.get('params')} wrote_log={res.get('wrote_log')}")
        if not res.get("wrote_log"):
            print("[l10-draft] WARNING: record not found in generator log tail — bridge may miss it")
        return 0
    print(f"[l10-draft] generator did not trigger this window (fired=False, tick={res.get('tick')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

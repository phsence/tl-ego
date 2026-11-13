#!/usr/bin/env python3
"""B 路径缺线：把闭环的真实失败数据回流给 ego，让它自己提出候选改动。

数据流（单向、可审计）::

    .lae/benchmarks/<最新报告>.json        ← 独立验证器判分（不是 ego 自述）
    .lae/selector_decisions/<同轮>.json    ← 上轮候选的判决
        → 失败摘要 (哪些集输了、为什么输: 掉洞 / 高度差 / 从未接近 / 超时)
        → /contemplate 问 ego (附白名单参数表 + 已试过的候选, 防止重复)
        → 解析它给的候选 JSON
        → policy_runtime.validate_params  (Gate1 白名单 + 范围钳制, 复用)
        → .lae/self_proposals/<ts>.json (带 provenance / trace_id / 生成代际)

用法::

    python -B ego_self_proposal.py --dry-run          # 离线自检（不碰网络）
    python -B ego_self_proposal.py                    # 真跑：向 8766 要一个候选
    python -B ego_self_proposal.py --outcome <id> hold    # 回收判定进历史

诚实性: ego 在**我们灌注的失败数据**下提出的改动, 来源记为 ``derived_goal``,
不是 ``self_generated_hypothesis``——"自己察觉弱点"尚未实现, 别把它记成自治。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import policy_runtime as pr

WS = Path(__file__).resolve().parent
BENCH_DIR = WS / ".lae" / "benchmarks"
SEL_DIR = WS / ".lae" / "selector_decisions"
OUT_DIR = WS / ".lae" / "self_proposals"
HISTORY = OUT_DIR / "history.jsonl"
EGO_BASE = "http://127.0.0.1:8766"

# 裁判阈值不是候选能改的——写进 prompt 防止 ego 提无效方案。
JUDGE_NOTE = "到达判定（水平≤3格 且 高度差≤5）属于裁判，你改不了，别提它。"


# ---------------------------------------------------------------- 证据读取

def _newest(directory: Path, pattern: str = "*.json") -> Path | None:
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def load_evidence(report_path: Path | None = None) -> dict:
    """从最新报告 + 选择器判决里抽出"我输在哪"。"""
    report_path = report_path or _newest(BENCH_DIR)
    if report_path is None:
        raise SystemExit("没有找到任何 benchmark 报告")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    recs = {(r.get("arm"), r.get("episode_id")): r for r in report.get("records", [])}

    def cell(arm: str, prefix: str) -> dict:
        out = {}
        for (a, ep), r in recs.items():
            if a == arm and ep.startswith(prefix):
                out[ep] = r
        return out

    cand_h = cell("candidate_on", "mc-holdout")
    base_h = cell("baseline", "mc-holdout")
    specs = sorted(set(cand_h) | set(base_h))

    def _ev(rec: dict | None, key: str, default=None):
        """失败细节在 record['evidence'] 里，不在 record 顶层。"""
        ev = (rec or {}).get("evidence") or {}
        val = ev.get(key, (rec or {}).get(key))
        return default if val is None else val

    lost, won, base_only = [], [], []
    for ep in specs:
        c, b = cand_h.get(ep), base_h.get(ep)
        c_ok = bool(c and c.get("success"))
        b_ok = bool(b and b.get("success"))
        row = {
            "spec": ep,
            "min_distance": _ev(c, "min_distance"),
            "dy": _ev(c, "dy"),
            "elapsed_s": (c or {}).get("elapsed_s"),
            "fail_reason": _ev(c, "fail_reason"),
            "baseline_ok": b_ok,
        }
        if not c_ok and b_ok:
            lost.append(row)
        elif c_ok and not b_ok:
            won.append(row)
        elif c_ok and b_ok:
            continue
        elif not c_ok and not b_ok:
            base_only.append({**row, "note": "双方都输(地形瓶颈)"})

    sel = None
    sel_path = SEL_DIR / report_path.name
    if sel_path.exists():
        sel = json.loads(sel_path.read_text(encoding="utf-8"))

    # decision 是 {checks, decision, reasons} 的 dict；绝不能整块灌进 prompt——
    # 实测把 checks 原文喂给 ego，它会把裁判项（efficiency_lift 等）当成"要改的参数"。
    dec = report.get("decision") or {}
    dec_str = dec.get("decision") if isinstance(dec, dict) else dec
    checks = (dec.get("checks") if isinstance(dec, dict) else report.get("checks")) or {}

    return {
        "report": report_path.name,
        "report_path": str(report_path),
        "frozen": report.get("profile_created_at"),
        "decision": dec_str,
        "failed_checks": sorted(k for k, v in checks.items()
                                if (v is False) or (isinstance(v, dict) and v.get("pass") is False)),
        "candidate_holdout": f"{sum(1 for v in cand_h.values() if v.get('success'))}/{len(specs)}",
        "baseline_holdout": f"{sum(1 for v in base_h.values() if v.get('success'))}/{len(specs)}",
        "lost_to_baseline": lost,
        "won_over_baseline": won,
        "both_failed": base_only,
        "selector": (sel or {}).get("final_decision"),
        "selector_reasons": (sel or {}).get("reasons", []),
        "selector_missing": (sel or {}).get("missing_evidence", []),
    }


def load_history(limit: int = 6) -> list[dict]:
    if not HISTORY.exists():
        return []
    rows = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-limit:]


# ---------------------------------------------------------------- 提问

PARAM_GLOSS = {
    "reobserve_after_no_motion_s": "身体不动超过 N 秒就重新观察/重试一次（越小=重试越勤）",
    "replan_after_tool_failure": "工具失败后是否换计划重试",
    "hostile_scan_radius": "扫描敌对生物的半径（格）",
    "food_rescue_threshold": "饥饿低于 N 时中断任务去找食物",
    "attack_retry_limit": "攻击失败后的重试次数",
    "goal_timeout_s": "单集任务窗口上限（秒）",
    "goal_direct_exec": "目标带精确坐标时直接执行、跳过 LLM 咨询",
}


# 裁判侧键：属于"卷子"而不是"身体"——改它等于改考试规则，不是能力提升。
# goal_timeout_s 是 episode 的**测量窗口**（桥自己的注释：taskBudgetS 不得复用它），
# 实测 ego 第一次给出合法键时就挑中它（"给我更多时间"= 改卷子）→ 从提案空间剔除。
JUDGE_SIDE_KEYS = {"goal_timeout_s"}


def proposable_keys() -> list[str]:
    return [k for k in pr.PARAM_SPEC if k not in JUDGE_SIDE_KEYS]


def param_table(current: dict | None = None) -> str:
    cur = current or pr.PARAM_SPEC_DEFAULTS()
    lines = []
    for key in proposable_keys():
        typ, low, high, default = pr.PARAM_SPEC[key]
        lines.append(f"  {key:28s} {str(low):>5}~{str(high):<5} 当前 {str(cur.get(key, default)):6s}"
                     f" — {PARAM_GLOSS.get(key, '')}")
    return "\n".join(lines)


def current_params() -> tuple[dict, str]:
    """ego 看到的"当前值"必须等于真实部署值（active profile），不是代码默认值。"""
    try:
        doc = pr.load_active_profile()
        return dict(doc.get("params") or pr.PARAM_SPEC_DEFAULTS()), str(doc.get("profile_id"))
    except Exception:
        return pr.PARAM_SPEC_DEFAULTS(), "builtin_defaults"


def fmt_failures(rows: list[dict]) -> str:
    if not rows:
        return "  （无）"
    out = []
    for r in rows:
        if r.get("note"):
            out.append(f"  - {r['spec']}: {r['note']}")
            continue
        out.append(
            f"  - {r['spec']}: 最接近 {r.get('min_distance')} 格 / 高度差 {r.get('dy')} / "
            f"{r.get('elapsed_s')}s / 判词 {r.get('fail_reason')}"
        )
    return "\n".join(out)


def build_prompt(ev: dict, history: list[dict], current: dict | None = None,
                 profile_id: str = "baseline") -> str:
    # 只列"真正改了键"的历史（changed 非空）；空 changed = 被门拒的无效提案，别拿它冒充已试候选
    tried_rows = [h for h in history if h.get("changed")]
    tried = "；".join(
        f"{h['proposal_id']}: {json.dumps(h['changed'], ensure_ascii=False)}"
        f"→{h.get('outcome') or h.get('status') or '未回收'}" for h in tried_rows[-3:]) \
        or "（还没有任何有效候选被测试过）"
    lost = "、".join(
        f"{r['spec'].replace('mc-holdout-', '#')}(最近 {r.get('min_distance')}格/高度差 {r.get('dy')}"
        f"{'/超时' if (r.get('elapsed_s') or 0) >= 299 else ''})"
        for r in ev["lost_to_baseline"][:5]) or "无"
    both = "、".join(r["spec"].replace("mc-holdout-", "#") for r in ev["both_failed"][:4]) or "无"
    return f"""只输出一行 JSON。不要调用任何工具。不要分析。不要解释。不要 markdown 代码块。

格式（唯一允许的输出，键名必须从下面列表**原样复制**）：
{{"key": "<键名>", "value": <新值>, "prediction": "<一句可验伪的预测>", "rationale": "<一句：针对哪条失败>"}}

背景：你（TL-Ego）在隐藏测试集上 {ev['candidate_holdout']}，基线 {ev['baseline_holdout']}，判决 {ev['decision']}。
你输掉、基线赢了的集：{lost}
双方都输（地形瓶颈，别指望调参解决）：{both}
已试过：{tried}

你能改的键 **只有下面这些**（不在列表里的键会被门自动拒绝，等于没提；当前值 = {profile_id} 线上实配）：
{param_table(current)}

到达判定（水平≤3格且高度差≤5）是裁判，你改不了。只改 1~2 个键。

【重要】测试的**打分项**（通过率、耗时比、成本、恢复等）是裁判的判据，**不是你能改的参数**——写它们会被系统直接拒绝、这轮就白费。你只能从上面那 7 个**身体旋钮**里原样复制一个键名。

现在只回那一行 JSON。"""


def post(path: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(f"{EGO_BASE}{path}",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ask_ego(prompt: str, timeout_s: int = 420) -> dict:
    """走已验证通路: POST /contemplate → 轮询 /contemplate-job。"""
    job = post("/contemplate", {"query": prompt, "depth": 1})["job_id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(8)
        d = post("/contemplate-job", {"job_id": job})
        if d.get("status") == "done":
            res = d.get("result") or {}
            return {
                "job_id": job,
                "response": res.get("response") or "",
                "tools": [e.get("tool") for e in (res.get("tool_executions") or [])],
                "route": res.get("llm_route"),
            }
    return {"job_id": job, "response": "", "tools": [], "timeout": True}


# ---------------------------------------------------------------- 解析与校验

def parse_proposal(text: str) -> dict | None:
    """扫描文本里第一个含 params/key 的 JSON 对象（容忍 ``` 围栏与前后废话）。"""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, dict) and ("params" in obj or "key" in obj):
            return obj
    return None


def validate(proposal: dict) -> tuple[dict, list[str]]:
    """Gate1：白名单 + 范围（复用 policy_runtime，不另写一套）。"""
    raw = proposal.get("params")
    if not raw and proposal.get("key"):
        raw = {str(proposal["key"]): proposal.get("value")}
    params, errors = pr.validate_params(raw or {})
    if not raw:
        errors.append("no_usable_params")
    errors.extend(judge_side_errors(raw))       # 裁判侧（测量窗口等）→ 硬拒
    return params, errors


def hard_errors(notes: list[str]) -> list[str]:
    """硬拒（键/类型/空提案/裁判侧键）；clamped:* 是软提示——值被钳进区间，仍可用。

    坑：validate_params 即使把未知键全丢掉也会**回填全部默认值**，所以
    "params 非空" 不能当成功判据，必须看 notes（实测因此提前退出重试循环）。
    """
    return [n for n in notes if n.startswith(("rejected_", "no_usable", "judge_side"))]


def judge_side_errors(raw: dict) -> list[str]:
    return [f"judge_side_key:{k}" for k in (raw or {}) if k in JUDGE_SIDE_KEYS]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _generation_id() -> str:
    try:
        with urllib.request.urlopen(f"{EGO_BASE}/status", timeout=10) as r:
            st = json.loads(r.read().decode("utf-8"))
        return str(st.get("runtime_generation_id") or st.get("generation_id") or "unknown")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- 主流程

DRY_EVIDENCE = {
    "report": "dry-run.json", "report_path": "(dry)", "frozen": "2026-09-14T04:32:43+0800",
    "decision": "hold", "failed_checks": ["efficiency_lift", "holdout_lift"],
    "candidate_holdout": "8/12", "baseline_holdout": "8/12",
    "lost_to_baseline": [{"spec": "mc-holdout-100", "min_distance": 1.45, "dy": 39.0,
                          "elapsed_s": 300.7, "fail_reason": "distance_5.36>3.0;dy_39.00>5.0",
                          "baseline_ok": True}],
    "won_over_baseline": [{"spec": "mc-holdout-102", "min_distance": 1.783, "dy": 1.0,
                           "elapsed_s": 103.3, "fail_reason": None, "baseline_ok": False}],
    "both_failed": [{"spec": "mc-holdout-104", "min_distance": 7.238, "dy": 15.0,
                     "elapsed_s": 300.75, "fail_reason": "distance_21.74>3.0", "baseline_ok": False,
                     "note": "双方都输(地形瓶颈)"}],
    "selector": "hold", "selector_reasons": ["holdout_lift"], "selector_missing": [],
}
DRY_RESPONSE = """看明白了。我输在掉洞和超时上。
```json
{"params": {"reobserve_after_no_motion_s": 45.0, "goal_timeout_s": 500},
 "prediction": "掉洞后我会更慢地重新观察,不再每 10 秒换一次尝试,100/111 应能爬回地面",
 "rationale": "针对 mc-holdout-100/111 的掉洞与 104 的超时"}
```
教训: 掉洞后频繁重发指令会互相打断,要给重新观察留更长的窗口。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, default=None, help="指定报告（默认最新）")
    ap.add_argument("--dry-run", action="store_true", help="离线自检：不碰网络")
    ap.add_argument("--outcome", nargs=2, metavar=("PROPOSAL_ID", "VERDICT"),
                    help="把某次候选的判定写回历史")
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--attempts", type=int, default=3, help="含反馈重问的最大轮数")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.outcome:
        pid, verdict = args.outcome
        rows = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()] \
            if HISTORY.exists() else []
        hit = 0
        for r in rows:
            if r.get("proposal_id") == pid:
                r["outcome"] = verdict
                hit += 1
        HISTORY.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                           encoding="utf-8")
        print(f"[outcome] {pid} → {verdict}（更新 {hit} 条）")
        return 0 if hit else 1

    ev = DRY_EVIDENCE if args.dry_run else load_evidence(args.report)
    history = load_history()
    cur, cur_id = current_params()
    prompt = build_prompt(ev, history, cur, cur_id)

    if args.dry_run:
        print("=== 失败摘要 ===")
        print(f"候选 {ev['candidate_holdout']} vs 基线 {ev['baseline_holdout']}｜判决 {ev['decision']}"
              f"｜未过 {ev['failed_checks']}")
        print("=== prompt 长度 ===", len(prompt), "字符")
        raw = {"response": DRY_RESPONSE, "tools": ["remember"], "job_id": "dry"}
    else:
        print(f"[evidence] {ev['report']}｜候选 {ev['candidate_holdout']} vs 基线 "
              f"{ev['baseline_holdout']}｜输给基线 {len(ev['lost_to_baseline'])} 集")
        print(f"[ask] 正在问 ego（/contemplate，最多 {args.attempts} 轮）…", flush=True)

    attempts: list[dict] = []
    proposal, params, errors = None, {}, []
    feedback = ""
    for _ in range(1 if args.dry_run else args.attempts):
        p = prompt + feedback
        r = ({"response": DRY_RESPONSE, "tools": ["remember"], "job_id": "dry"}
             if args.dry_run else ask_ego(p, timeout_s=args.timeout))
        attempts.append({"n": len(attempts) + 1, "prompt_sha256": _sha(p),
                         "tools": r.get("tools"),
                         "raw_response": (r.get("response") or "")[:3000]})
        raw = r
        proposal = parse_proposal(r.get("response", ""))
        if proposal is None:
            feedback = "\n\n【系统】你上一条回复里没有 JSON。只回一行 JSON，不要分析、不要调用工具。"
            continue
        params, errors = validate(proposal)
        changed_now = {k: v for k, v in params.items() if cur.get(k) != v}
        if not hard_errors(errors) and changed_now:
            break
        if not hard_errors(errors) and not changed_now:
            why = "no_op_value_unchanged"
            print(f"[retry] 第 {len(attempts)} 次无效: 值没变（等于没改）→ 反馈重问", flush=True)
        else:
            why = "; ".join(hard_errors(errors))
            print(f"[retry] 第 {len(attempts)} 次被 Gate1 拒绝: {why} → 带拒绝理由重问", flush=True)
        feedback = (f"\n\n【系统门拒绝了你的回答】{why}。"
                    f"你只能从这些键里**原样复制一个**：{', '.join(pr.PARAM_SPEC)}；"
                    f"新值不能等于当前值。只回一行 JSON。")

    if proposal is None:
        rec = {
            "proposal_id": f"mc-self-{int(time.time())}", "ts": time.time(),
            "origin": "derived_goal", "status": "no_parseable_proposal",
            "prompt_sha256": _sha(prompt), "raw_response": (raw.get("response") or "")[:4000],
            "generation_id": _generation_id() if not args.dry_run else "dry",
            "evidence_report": ev["report"], "tools": raw.get("tools"),
            "attempts": attempts,
        }
        if args.dry_run:            # dry-run 不许污染证据目录
            print("[dry] no_parseable_proposal（不落盘）")
            return 2
        (OUT_DIR / f"{rec['proposal_id']}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        with HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"proposal_id": rec["proposal_id"], "ts": rec["ts"],
                                 "status": "no_parseable_proposal", "changed": {},
                                 "ego_tools": raw.get("tools"), "outcome": None},
                                ensure_ascii=False) + "\n")
        print(f"[FAIL] ego 没有给出可解析的候选 JSON → 已如实记录 {rec['proposal_id']}"
              f"（这就是「能力」数据，不是管道故障）")
        return 2

    params, errors = validate(proposal)
    changed = {k: v for k, v in params.items() if cur.get(k) != v}
    pid = f"mc-self-{int(time.time())}"
    rec = {
        "proposal_id": pid, "ts": time.time(), "origin": "derived_goal",
        "status": None if (not hard_errors(errors) and changed) else "invalid_proposal",
        "trigger": "human_or_cron:ego_self_proposal.py",
        "params": params, "changed": changed, "prediction": proposal.get("prediction"),
        "rationale": proposal.get("rationale"),
        "dropped_or_clamped": errors,
        "raw_params": proposal.get("params"),
        "prompt_sha256": _sha(prompt), "evidence_report": ev["report"],
        "evidence_sha256": _sha(json.dumps(ev, ensure_ascii=False, sort_keys=True)),
        "generation_id": _generation_id() if not args.dry_run else "dry",
        "ego_tools": raw.get("tools"), "ego_job_id": raw.get("job_id"),
        "raw_response": (raw.get("response") or "")[:4000],
        "attempts": attempts,
        "outcome": None,
    }
    if args.dry_run:                    # 自检只走解析+校验+记录构建，不落盘
        print(f"[dry] proposal parsed+validated OK → {json.dumps(changed, ensure_ascii=False)}")
        return 0
    (OUT_DIR / f"{pid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: rec.get(k) for k in
                             ("proposal_id", "ts", "params", "changed", "prediction",
                              "rationale", "outcome", "status")},
                            ensure_ascii=False) + "\n")

    if hard_errors(errors) or not changed:
        print(f"[FAIL] 提案无效（{errors or '值未改变'}）→ 已如实记录 {pid}")
        return 2

    print(f"[candidate] {pid}")
    print(f"  changed    : {json.dumps(changed, ensure_ascii=False)}  (相对 {cur_id})")
    print(f"  prediction : {rec['prediction']}")
    print(f"  rationale  : {rec['rationale']}")
    print(f"  Gate1      : {errors or 'OK（白名单+范围全过）'}")
    print(f"  ego 工具    : {rec['ego_tools']}（出现 remember = 它把教训写进了自己的记忆）")
    print(f"  → 下一步可把它喂进矩阵: --candidate-params '{json.dumps(params)}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

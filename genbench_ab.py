"""genbench_ab.py - A/B 验收 mini genbench (3 题), 配合 _run_governance_check L9432

3 题 = 1 T1 (算术) + 2 T2 (规划), 跑约 20s, 返 task_success in [0.0, 1.0]

设计原则:
- 题库固定 (可重复, A/B 才公平)
- 不依赖 LLM 自评 (机械判分)
- v0 只测 baseline_score (apply 前快照), v1 加 Δscore 比对 + 自动回滚
"""
from __future__ import annotations
import json, re, sys, time, urllib.request
from pathlib import Path
from typing import Optional

EGO_URL = "http://127.0.0.1:8766"
AB_LOG = Path(__file__).resolve().parent / ".lae" / "l9" / "ab_validation.jsonl"


def ask_ego(query: str, timeout_s: float = 30.0) -> dict:
    payload = json.dumps({"query": query}).encode("utf-8")
    try:
        req = urllib.request.Request(EGO_URL + "/contemplate", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"error": f"submit failed: {exc}"}
    job_id = body.get("job_id")
    if not job_id:
        return {"error": f"no job_id: {body}"}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        poll = json.dumps({"job_id": job_id}).encode("utf-8")
        req2 = urllib.request.Request(EGO_URL + "/contemplate-job", data=poll,
                                      headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req2, timeout=10) as r2:
                job = json.loads(r2.read().decode("utf-8"))
        except Exception as exc:
            return {"error": f"poll failed: {exc}"}
        status = job.get("status")
        if status == "done":
            return {"job_id": job_id, "status": "done", "result": job.get("result")}
        if status == "error":
            return {"error": job.get("error", "unknown error")}
        time.sleep(1.0)
    return {"error": "timeout", "job_id": job_id}


def _text(result) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("response", "answer", "output", "content", "text", "result"):
            if key in result:
                v = result[key]
                if isinstance(v, str):
                    return v
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def _find_number(text: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


# ── 固定题库 (3 题, 简洁可重复) ──────────────────────────────────

T1_ARITH = {
    "id": "ab-arith-01",
    "query": "计算 11+22+33 的结果。只回答数字。",
    "expected": 66.0,
    "kind": "arithmetic",
}

T2_PLAN_01 = {
    "id": "ab-plan-01",
    "query": "出门发现车坏了请给出 3 个最高优先级动作。用 1. 2. 3. 编号。",
    "min_numbered": 2,
    "must_contain_any": ["修", "联", "公", "租", "步", "打", "车", "朋"],
    "kind": "planning",
}

T2_PLAN_02 = {
    "id": "ab-plan-02",
    "query": "家里 Wi-Fi 突然断了，给出 3 个排查动作。用 1. 2. 3. 编号。",
    "min_numbered": 2,
    "must_contain_any": ["重", "路", "光", "电", "打", "联", "信"],
    "kind": "planning",
}

FIXED_TASKS = [T1_ARITH, T2_PLAN_01, T2_PLAN_02]


def _score_arith(task: dict, result: dict) -> bool:
    text = _text(result.get("result"))
    num = _find_number(text)
    return num == task["expected"]


def _score_plan(task: dict, result: dict) -> bool:
    text = _text(result.get("result"))
    numbered = re.findall(r"^\s*\d+[\.\)]", text, re.MULTILINE)
    steps = len(numbered)
    return steps >= task["min_numbered"] and any(kw in text for kw in task["must_contain_any"])


def _score_task(task: dict, result: dict) -> tuple[bool, str]:
    if "error" in result:
        return False, f"error: {result['error'][:60]}"
    if task["kind"] == "arithmetic":
        ok = _score_arith(task, result)
    elif task["kind"] == "planning":
        ok = _score_plan(task, result)
    else:
        ok = False
    return ok, ("ok" if ok else "fail")


# v8.3 A/B v1.1: baseline 持久化 + 报警
LAST_BASELINE_PATH = Path(__file__).resolve().parent / ".lae" / "l9" / "last_baseline.json"
ALERT_THRESHOLD = 0.15  # baseline 跌超过 0.15 = 能力可能退化
DEGRADED_ALERTS_PATH = Path(__file__).resolve().parent / ".lae" / "l9" / "degraded_alerts.jsonl"
ROLLBACK_THRESHOLD = 0.30  # delta <= -ROLLBACK_THRESHOLD = rollback (stricter than degraded alert)
ROLLED_BACK_PATH = Path(__file__).resolve().parent / ".lae" / "l9" / "rolled_back.jsonl"


def load_last_baseline() -> Optional[float]:
    """Load most recent baseline_score from .lae/l9/last_baseline.json."""
    try:
        if LAST_BASELINE_PATH.exists():
            data = json.loads(LAST_BASELINE_PATH.read_text(encoding="utf-8"))
            score = data.get("baseline_score")
            return float(score) if score is not None else None
    except Exception:
        pass
    return None


def save_last_baseline(score: float, proposal_id: str = "") -> None:
    """Persist current baseline_score to .lae/l9/last_baseline.json (overwrite, not append)."""
    try:
        LAST_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "baseline_score": float(score),
            "proposal_id": proposal_id,
        }
        # Atomic write: write to .tmp, then rename
        tmp = LAST_BASELINE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(LAST_BASELINE_PATH)
    except Exception as exc:
        print("[save_last_baseline] failed: " + str(exc), file=sys.stderr)


def write_degraded_alert(current: float, previous: float, delta: float, proposal_id: str = "", extra: dict = None) -> dict:
    """Append a degraded alert to .lae/l9/degraded_alerts.jsonl. Returns the alert entry.

    Called when score_alert() returns "degraded" (delta <= -ALERT_THRESHOLD).
    The alert log is append-only, designed for external monitoring/alerting to consume.
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "level": "degraded",
        "current_score": round(float(current), 6),
        "previous_score": round(float(previous), 6),
        "delta": round(float(delta), 6),
        "threshold": ALERT_THRESHOLD,
        "proposal_id": proposal_id,
        "extra": extra or {},
    }
    try:
        DEGRADED_ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEGRADED_ALERTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print("[write_degraded_alert] failed: " + str(exc), file=sys.stderr)
    return entry


def mark_rolled_back(proposal_id: str, current: float, previous: float, delta: float, reason: str = "", extra: dict = None) -> dict:
    """Append a rollback event to .lae/l9/rolled_back.jsonl.

    Called when alert=degraded AND delta <= -ROLLBACK_THRESHOLD.
    Caller should also call _L7.proposer.update_status(id, "rolled_back") to flip status.
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "level": "rolled_back",
        "proposal_id": proposal_id,
        "current_score": round(float(current), 6),
        "previous_score": round(float(previous), 6),
        "delta": round(float(delta), 6),
        "rollback_threshold": ROLLBACK_THRESHOLD,
        "reason": reason,
        "extra": extra or {},
    }
    try:
        ROLLED_BACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(ROLLED_BACK_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print("[mark_rolled_back] failed: " + str(exc), file=sys.stderr)
    return entry



def score_alert(current: float, previous: Optional[float]) -> tuple[float, str]:
    """Compare current baseline vs previous. Return (delta, alert_msg).

    delta = current - previous  (负数表示能力下降)
    alert_msg:
      "first_run"  - 没有 previous
      "stable"     - 差 < 阈值
      "improved"   - 涨 >= 阈值
      "degraded"   - 跌 >= 阈值 (报警)
    """
    if previous is None:
        return 0.0, "first_run"
    delta = round(current - previous, 6)
    if delta >= ALERT_THRESHOLD:
        return delta, "improved"
    elif delta <= -ALERT_THRESHOLD:
        return delta, "degraded"
    else:
        return delta, "stable"


def ab_validate(proposal_id: Optional[str] = None, note: str = "") -> dict:
    """Run 3-task mini genbench, return task_success in [0.0, 1.0].

    Logs to .lae/l9/ab_validation.jsonl with:
      ts, proposal_id, note, task_success, per_task: [{id, ok, reason}]
    """
    results = []
    for task in FIXED_TASKS:
        r = ask_ego(task["query"], timeout_s=40.0)
        ok, reason = _score_task(task, r)
        results.append({"id": task["id"], "ok": ok, "reason": reason})

    n_ok = sum(1 for r in results if r["ok"])
    score = round(n_ok / len(FIXED_TASKS), 6)

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "proposal_id": proposal_id,
        "note": note,
        "task_success": score,
        "per_task": results,
        "n_passed": n_ok,
        "n_total": len(FIXED_TASKS),
    }

    # Append to ab_validation.jsonl (append mode, never overwrite)
    try:
        AB_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AB_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[ab_validate] log write failed: {exc}", file=sys.stderr)

    return entry


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else None
    note = sys.argv[2] if len(sys.argv) > 2 else "manual"
    entry = ab_validate(proposal_id=pid, note=note)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print(f"\nLogged to: {AB_LOG}")

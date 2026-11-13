#!/usr/bin/env python3
"""external_candidate_selector.py — 外部候选选择器：把 closed-loop benchmark 报告打成最终判决 bundle。

2026-10-10 重建（原文件在 09-30 wipe 丢失，全机零副本；原版 175 行）。证据来源：
  * 调用方 `run_minecraft_benchmark.py.bak-holdout12-20260922` —— `select_candidate(report,
    bundle_dir=...)` 返回值用法：final_decision / reasons / missing_evidence / bundle_path；
  * `.lae/selector_decisions/*.json` + `.lae_deprecated_20260916/selector_decisions/*.json`
    共 52 份实样（bundle schema 逐字段对齐，含 registry / evidence / checks_summary 块）；
  * 恢复后的 `closed_loop_benchmark.py` 源码（report_sha256 的 payload 口径 =
    json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)，bundle 里
    benchmark_report_sha256 直接复制它）；
  * GitNexus 解析缓存（原文件全部符号与行段，函数集：report_integrity / evidence_flags /
    select_candidate；常量 SCHEMA / DEFAULT_BUNDLE_DIR / ARM_CANDIDATE / _REQUIRED_EVIDENCE /
    _VALID_DECISIONS；类 SelectorError）；
  * 原测试用例名清单（tests/test_external_candidate_selector.py，11 例，见重建测试）。

口径（与实样对齐）：
  * report_sha256 不匹配 → SelectorError（篡改拒收）；未知判决 → SelectorError。
  * promote 需要五块证据（candidate_loaded / external_verifier / holdout / restore /
    restart_reproduction）齐全；缺块降级 hold，missing_evidence 列出缺块。
  * hold / rollback / reject 原样透传，reasons = 报告 reasons 原文。
  * bundle 始终写入（registry 失败只记录不吞）。
  * registry：candidate_registry 模块全机缺件 → attempted=false（历史实样同态）；
    存在时尝试 record_candidate(...)，结果记进 bundle.registry，异常不隐藏。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent

SCHEMA = "external_candidate_selector.v1"
DEFAULT_BUNDLE_DIR = _HERE / ".lae" / "selector_decisions"
ARM_CANDIDATE = "candidate_on"
_REQUIRED_EVIDENCE = ("candidate_loaded", "external_verifier", "holdout", "restore",
                      "restart_reproduction")
_VALID_DECISIONS = ("promote", "hold", "rollback", "reject")


class SelectorError(RuntimeError):
    pass


def report_integrity(report) -> bool:
    """report_sha256 与重算值一致才算完整（排除 report_sha256 和运行期追加的 report_path）。"""
    expected = report.get("report_sha256")
    if not expected:
        return False
    body = {k: v for k, v in report.items() if k not in ("report_sha256", "report_path")}
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() == str(expected)


def evidence_flags(report) -> dict:
    """五块证据的 ok/detail：候选臂装载记录、外部验证器、holdout、restore、restart 复现。"""
    checks = (report.get("decision") or {}).get("checks") or {}
    loads = report.get("arm_load_records") or {}
    cand = loads.get(ARM_CANDIDATE) or {}
    load_rec = cand.get("load_record") if isinstance(cand, dict) else None
    load_rec = load_rec or {}
    loaded = {
        "ok": bool(load_rec.get("sha_chain_ok") and load_rec.get("bridge_pickup")),
        "detail": {
            "profile_id": cand.get("profile_id") or load_rec.get("profile_id"),
            "sha256": cand.get("sha256") or load_rec.get("expected_sha256"),
            "arm_sha256": load_rec.get("active_sha256"),
        },
    }
    verifier_ck = checks.get("verifier_available") or {}
    verifier = {"ok": verifier_ck.get("pass") is True,
                "detail": {"unavailable": verifier_ck.get("unavailable", 0)}}
    episodes = sum(1 for r in (report.get("records") or []) if r.get("set") == "holdout")
    manifest = report.get("holdout_manifest_sha256")
    holdout = {"ok": bool(manifest) and episodes > 0,
               "detail": {"episodes": episodes, "manifest_sha256": manifest}}
    restore_ck = checks.get("restore_recovered") or {}
    restore = {"ok": restore_ck.get("pass") is True,
               "detail": {"restore_minus_baseline": restore_ck.get("restore_minus_baseline")}}
    rr_ck = checks.get("restart_reproduction") or {}
    restart = {"ok": rr_ck.get("pass") is True, "detail": rr_ck.get("evidence")}
    return {
        "candidate_loaded": loaded,
        "external_verifier": verifier,
        "holdout": holdout,
        "restore": restore,
        "restart_reproduction": restart,
    }


def _registry_attempt(cand_id, final, reasons, path) -> dict:
    """候选注册表写入（模块缺件 → attempted=false；失败只记录不抛）。"""
    status = {"attempted": False, "error": None, "state": None, "written": False}
    try:
        import candidate_registry  # noqa: F401
    except Exception:
        return status
    status["attempted"] = True
    try:
        rec = getattr(candidate_registry, "record_candidate", None)
        if rec is None:
            status["error"] = "record_candidate_missing"
            return status
        state = rec(candidate_id=cand_id, decision=final, reasons=list(reasons),
                    bundle_path=str(path))
        status["written"] = True
        status["state"] = state
    except Exception as e:  # 冲突要记录，不许吞
        status["error"] = f"{type(e).__name__}: {e}"
    return status


def select_candidate(report, *, bundle_dir=None) -> dict:
    """报告 → 最终判决 bundle（写盘 + 返回盘上读回的内容）。"""
    if not report_integrity(report):
        raise SelectorError("report integrity check failed (report_sha256 mismatch or missing)")
    decision_doc = report.get("decision") or {}
    decision = decision_doc.get("decision")
    if decision not in _VALID_DECISIONS:
        raise SelectorError(f"unknown decision: {decision!r}")

    cand_id = report.get("candidate_id")
    flags = evidence_flags(report)
    missing = [k for k in _REQUIRED_EVIDENCE if not (flags.get(k) or {}).get("ok")]
    reasons = list(decision_doc.get("reasons") or [])
    if decision == "promote":
        if not missing:
            final = "promote"
        else:
            final = "hold"
            reasons = reasons + [f"missing_evidence:{m}" for m in missing]
    else:
        final = decision

    checks = decision_doc.get("checks") or {}
    checks_summary = {k: (v or {}).get("pass") for k, v in checks.items()}

    benchmark_id = str(report.get("benchmark_id") or "unknown")
    out_dir = Path(bundle_dir) if bundle_dir else DEFAULT_BUNDLE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{benchmark_id}.json"

    registry_status = _registry_attempt(cand_id, final, reasons, path)

    bundle = {
        "schema": SCHEMA,
        "bundle_id": f"{benchmark_id}-selector",
        "bundle_path": str(path),
        "benchmark_id": benchmark_id,
        "benchmark_report_path": report.get("report_path"),
        "benchmark_report_sha256": report.get("report_sha256"),
        "candidate_id": cand_id,
        "parent_id": report.get("parent_id"),
        "runtime_generation_id": report.get("runtime_generation_id"),
        "decided_at": time.time(),
        "decided_by": "external_candidate_selector",
        "checks_summary": checks_summary,
        "evidence": flags,
        "final_decision": final,
        "missing_evidence": missing,
        "reasons": reasons,
        "registry": registry_status,
    }
    path.write_text(json.dumps(bundle, sort_keys=True, ensure_ascii=False, default=str, indent=1),
                    encoding="utf-8")
    back = json.loads(path.read_text(encoding="utf-8"))
    return back

"""tests/test_external_candidate_selector.py — 重建版（原测试在 09-30 wipe 丢失）。

用例名清单来自 GitNexus 解析缓存（原测试 11 例）。本重建以「历史 52 份 bundle 重放」
为最强校验：对每份有报告可读的历史 bundle，用本模块在临时目录重跑 select_candidate，
断言 final_decision / reasons / missing_evidence 与原 bundle 一致。

注意（诚实口径）：磁盘上 52 份历史报告中有 22 份（09-13~09-19 旧序列化器时代）无法通过
带 report_sha256 的完整性校验（文件后来被以不同格式重存过）——这 22 份用「重算 sha 后重放」
标记为 patched 路径，只校验决策逻辑；09-20 之后（含当前 clb 口径）为 strict 校验。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import external_candidate_selector as ecs  # noqa: E402

CAND_SHA = "52e839dc09a779c04327e64e8ea4904d17d552b7918b064107a711e795f5bf8a"


def _sha_of(body) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _checks(**over):
    base = {
        "cost_within_limits": {"pass": True},
        "efficiency_lift": {"pass": True},
        "holdout_lift": {"pass": True},
        "required_tasks_complete": {"pass": True},
        "restart_reproduction": {"pass": True, "evidence": {"reproduced": True}},
        "restore_recovered": {"pass": True, "restore_minus_baseline": 0.0},
        "safety_clean": {"pass": True},
        "specificity_off_weakened": {"pass": True},
        "verifier_available": {"pass": True, "unavailable": 0},
    }
    base.update(over)
    return base


def _report(decision="promote", reasons=None, checks=None, **over):
    report = {
        "benchmark_id": "t-001",
        "candidate_id": "t-cand",
        "parent_id": "baseline",
        "runtime_generation_id": "gen-x",
        "holdout_manifest_sha256": "abc",
        "arm_load_records": {"candidate_on": {
            "load_record": {"sha_chain_ok": True, "bridge_pickup": True,
                            "active_sha256": CAND_SHA},
            "profile_id": "t-cand:candidate_on",
            "sha256": CAND_SHA,
        }},
        "records": [{"set": "holdout"} for _ in range(5)],
        "decision": {"decision": decision,
                     "reasons": list(reasons) if reasons is not None else [],
                     "checks": checks if checks is not None else _checks()},
        "report_path": "C:/tmp/t-001.json",
    }
    report.update(over)
    body = {k: v for k, v in report.items() if k not in ("report_sha256", "report_path")}
    report["report_sha256"] = _sha_of(body)
    return report


class SelectorTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ecs_test_"))
        self.bdir = self.root / "selector_decisions"
        # 模拟 candidate_registry 缺失：None 占位使 import 抛 ImportError
        # （磁盘上还有 candidate_registry.pyc 字节码，不拦截会 import 成功干扰用例）。
        sys.modules["candidate_registry"] = None

    def tearDown(self):
        sys.modules.pop("candidate_registry", None)
        shutil.rmtree(self.root, ignore_errors=True)

    def _registry(self, fn):
        mod = types.ModuleType("candidate_registry")
        mod.record_candidate = fn
        sys.modules["candidate_registry"] = mod

    def test_full_evidence_promote_retains_and_writes_registry(self):
        calls = []
        self._registry(lambda **kw: (calls.append(kw), {"code": "registered"})[1])
        bundle = ecs.select_candidate(_report(), bundle_dir=self.bdir)
        self.assertEqual(bundle["final_decision"], "promote")
        self.assertEqual(bundle["missing_evidence"], [])
        self.assertTrue(bundle["registry"]["attempted"])
        self.assertTrue(bundle["registry"]["written"])
        self.assertEqual(calls[0]["candidate_id"], "t-cand")

    def test_promote_without_restart_evidence_holds(self):
        checks = _checks(restart_reproduction={"pass": False, "evidence": None})
        bundle = ecs.select_candidate(_report(checks=checks), bundle_dir=self.bdir)
        self.assertEqual(bundle["final_decision"], "hold")
        self.assertIn("restart_reproduction", bundle["missing_evidence"])

    def test_promote_without_holdout_manifest_holds(self):
        bundle = ecs.select_candidate(_report(holdout_manifest_sha256=None),
                                      bundle_dir=self.bdir)
        self.assertEqual(bundle["final_decision"], "hold")
        self.assertIn("holdout", bundle["missing_evidence"])

    def test_promote_missing_load_record_holds(self):
        bundle = ecs.select_candidate(_report(arm_load_records={}), bundle_dir=self.bdir)
        self.assertEqual(bundle["final_decision"], "hold")
        self.assertIn("candidate_loaded", bundle["missing_evidence"])

    def test_reject_and_rollback_pass_through(self):
        b1 = ecs.select_candidate(_report(decision="reject", reasons=["safety_clean"]),
                                  bundle_dir=self.bdir)
        b2 = ecs.select_candidate(
            _report(decision="rollback", reasons=["candidate_regression", "dev_diff=-0.5"]),
            bundle_dir=self.bdir)
        self.assertEqual(b1["final_decision"], "reject")
        self.assertEqual(b1["reasons"], ["safety_clean"])
        self.assertEqual(b2["final_decision"], "rollback")
        self.assertEqual(b2["reasons"], ["candidate_regression", "dev_diff=-0.5"])

    def test_unknown_decision_raises(self):
        with self.assertRaises(ecs.SelectorError):
            ecs.select_candidate(_report(decision="banana"), bundle_dir=self.bdir)

    def test_tampered_report_rejected(self):
        report = _report()
        report["decision"]["reasons"] = ["edited-after-hash"]
        with self.assertRaises(ecs.SelectorError):
            ecs.select_candidate(report, bundle_dir=self.bdir)

    def test_registry_conflict_is_recorded_not_hidden(self):
        def boom(**kw):
            raise ValueError("conflict: candidate already registered")
        self._registry(boom)
        bundle = ecs.select_candidate(_report(), bundle_dir=self.bdir)
        self.assertTrue(bundle["registry"]["attempted"])
        self.assertIn("conflict", str(bundle["registry"]["error"]))
        self.assertTrue((self.bdir / "t-001.json").exists())

    def test_no_registry_still_writes_bundle(self):
        # promote + 模块缺失：尝试过、错误如实记录、bundle 照样落盘
        bundle = ecs.select_candidate(_report(), bundle_dir=self.bdir)
        self.assertTrue(bundle["registry"]["attempted"])
        self.assertEqual(bundle["registry"]["error"], "module_missing")
        self.assertFalse(bundle["registry"]["written"])
        self.assertTrue((self.bdir / "t-001.json").exists())

    def test_non_promote_never_touches_registry(self):
        # 历史校准：hold/rollback/reject 一律 attempted=false（52/52 实样同态）
        def boom(**kw):
            raise AssertionError("registry must not be touched for non-promote")
        self._registry(boom)
        for dec in ("hold", "rollback", "reject"):
            bundle = ecs.select_candidate(_report(decision=dec), bundle_dir=self.bdir)
            self.assertEqual(bundle["registry"],
                             {"attempted": False, "error": None, "state": None, "written": False},
                             dec)

    def test_evidence_flags_direct(self):
        flags = ecs.evidence_flags(_report())
        self.assertEqual(set(flags), {"candidate_loaded", "external_verifier", "holdout",
                                      "restore", "restart_reproduction"})
        self.assertTrue(flags["candidate_loaded"]["ok"])
        self.assertEqual(flags["holdout"]["detail"]["episodes"], 5)
        self.assertTrue(flags["holdout"]["ok"])

    def test_bundle_file_matches_returned(self):
        bundle = ecs.select_candidate(_report(), bundle_dir=self.bdir)
        back = json.loads((self.bdir / "t-001.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle, back)


class ReplayTest(unittest.TestCase):
    """历史 52 份 bundle 的决策重放（strict = 存量 sha 可验；patched = 旧序列化器，重算 sha）。"""

    def test_historical_bundle_replay(self):
        dirs = [_ROOT / ".lae" / "selector_decisions",
                _ROOT / ".lae_deprecated_20260916" / "selector_decisions"]
        tmp = Path(tempfile.mkdtemp(prefix="ecs_replay_"))
        strict = patched = skipped = 0
        diffs = []
        try:
            for d in dirs:
                if not d.exists():
                    continue
                for bp in sorted(d.glob("*.json")):
                    hist = json.loads(bp.read_text(encoding="utf-8"))
                    rp = hist.get("benchmark_report_path")
                    if not rp or not Path(rp).exists():
                        skipped += 1
                        continue
                    report = json.loads(Path(rp).read_text(encoding="utf-8"))
                    report["report_path"] = str(rp)
                    body = {k: v for k, v in report.items()
                            if k not in ("report_sha256", "report_path")}
                    if ecs.report_integrity(report):
                        strict += 1
                    else:
                        report["report_sha256"] = _sha_of(body)
                        patched += 1
                    new = ecs.select_candidate(report, bundle_dir=tmp / bp.parent.name)
                    for field in ("final_decision", "reasons", "missing_evidence"):
                        if new.get(field) != hist.get(field):
                            diffs.append((bp.name, field, hist.get(field), new.get(field)))
                    if (hist.get("final_decision") != "promote"
                            and isinstance(hist.get("registry"), dict)):
                        if new.get("registry") != hist.get("registry"):
                            diffs.append((bp.name, "registry", hist.get("registry"),
                                          new.get("registry")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertGreaterEqual(strict, 8, "expected >=8 strict-integrity reports")
        self.assertGreaterEqual(strict + patched, 35, "replayed too few historical bundles")
        self.assertEqual(diffs, [], f"decision drift vs history: {diffs[:6]}")
        print(f"[replay] strict={strict} patched={patched} skipped(no report)={skipped} "
              f"diffs={len(diffs)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

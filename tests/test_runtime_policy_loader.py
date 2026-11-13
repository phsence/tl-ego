"""tests/test_runtime_policy_loader.py — 重建版（原用例名来自 GitNexus 解析缓存）。

覆盖：load 记录读写往返、回读验证（ok / 检测文件调包 / 检测进程重启 / 无记录）、
rollback_to_parent 恢复父本并回读、canary 确定性、restart storm 阈值。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import policy_runtime as pr          # noqa: E402
import runtime_policy_loader as rtl  # noqa: E402


class TestDeploymentLedger(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rtl_test_"))
        self.pdir = self.root / "policy_profiles"
        self.pdir.mkdir()
        self.ddir = self.root / "deployment"
        self.baseline = pr.write_profile({}, profile_id="baseline",
                                         base_dir=self.pdir, write=True)
        self.cand = pr.write_profile({"goal_timeout_s": 41}, profile_id="cand",
                                     base_dir=self.pdir, write=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _load_cand(self, sha=None, gen="gen-test"):
        return rtl.record_load("cand", sha256=sha or self.cand["sha256"],
                               runtime_generation_id=gen, pid=4242,
                               extra={"arm": "test"}, base_dir=self.ddir)

    def test_record_and_read_roundtrip(self):
        rec = self._load_cand()
        recs = rtl.read_load_records(self.ddir)
        self.assertEqual(recs[-1], rec)
        self.assertEqual(rec["profile_id"], "cand")
        self.assertEqual(rec["pid"], 4242)
        self.assertEqual(rec["extra"], {"arm": "test"})

    def test_verify_ok(self):
        live_gen = rtl.fetch_generation()
        self._load_cand(gen=live_gen)
        pr.activate("cand", activated_by="test", base_dir=self.pdir)
        out = rtl.verify_load_readback(base_dir=self.ddir)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reason"], "ok")

    def test_verify_detects_file_swap(self):
        self._load_cand(sha="0" * 64)
        pr.activate("cand", activated_by="test", base_dir=self.pdir)
        out = rtl.verify_load_readback(base_dir=self.ddir)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "sha_mismatch")

    def test_verify_detects_restart(self):
        self._load_cand(gen="gen-OLD")
        pr.activate("cand", activated_by="test", base_dir=self.pdir)
        out = rtl.verify_load_readback(base_dir=self.ddir)
        if rtl.fetch_generation() == "gen-OLD":
            self.skipTest("live generation unavailable; cannot distinguish")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "generation_changed")

    def test_verify_no_records(self):
        out = rtl.verify_load_readback(base_dir=self.root / "empty_deploy")
        self.assertEqual(out, {"ok": False, "reason": "no_records"})

    def test_rollback_restores_parent_and_reads_back(self):
        pr.activate("cand", activated_by="test", base_dir=self.pdir)
        self._load_cand()
        rb = rtl.rollback_to_parent("baseline", reason="test_restore", base_dir=self.ddir)
        self.assertTrue(rb["readback"]["matches_parent"], rb["readback"])
        self.assertEqual(rb["readback"]["active_sha256"], self.baseline["sha256"])
        recs = rtl.read_rollback_records(self.ddir)
        self.assertEqual(recs[-1]["from_profile"], "cand")
        self.assertEqual(recs[-1]["to_profile"], "baseline")
        self.assertEqual(recs[-1]["reason"], "test_restore")

    def test_canary_deterministic_and_bounded(self):
        self.assertEqual(rtl.canary_selected("cand-x", 0.5),
                         rtl.canary_selected("cand-x", 0.5))
        self.assertFalse(any(rtl.canary_selected(f"c{i}", 0.0) for i in range(50)))
        self.assertTrue(all(rtl.canary_selected(f"c{i}", 1.0) for i in range(50)))
        frac = sum(rtl.canary_selected(f"id-{i}", 0.5) for i in range(400)) / 400
        self.assertTrue(0.3 < frac < 0.7, frac)

    def test_restart_storm_blocks_at_threshold(self):
        now = 1_000_000.0
        rtl.note_restart({"ts": now - 5000.0, "kind": "old"}, base_dir=self.ddir)
        rtl.note_restart({"ts": now - 10.0}, base_dir=self.ddir)
        out = rtl.restart_storm(now=now, window_s=1800.0, threshold=3, base_dir=self.ddir)
        self.assertEqual(out["recent"], 1)
        self.assertFalse(out["storm"])
        rtl.note_restart({"ts": now - 8.0}, base_dir=self.ddir)
        rtl.note_restart({"ts": now - 6.0}, base_dir=self.ddir)
        out = rtl.restart_storm(now=now, window_s=1800.0, threshold=3, base_dir=self.ddir)
        self.assertEqual(out["recent"], 3)
        self.assertTrue(out["storm"])

    def test_restart_allowed_roundtrip(self):
        now = 2_000_000.0
        self.assertTrue(rtl.restart_allowed(now=now, base_dir=self.ddir))
        for i in range(4):
            rtl.note_restart({"ts": now - i}, base_dir=self.ddir)
        self.assertFalse(rtl.restart_allowed(now=now, base_dir=self.ddir))


if __name__ == "__main__":
    unittest.main(verbosity=2)

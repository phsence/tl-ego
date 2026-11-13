"""Runnable check for the 2026-10-17 fixes (no frameworks, no fixtures).

1. criticality regulator must actually tick when no `_proprio_calibration` record exists
   (it used to early-return every tick -> ticks=0, regime=unknown forever).
2. the phi entropy proxy must never be published as phi_total.

    py -3.12 test_phi_and_criticality_20261017.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


class _StubModule:
    """Minimal stand-in for tl_ego_server as seen by criticality_regulator."""

    def __init__(self, proprio, resting=None):
        self._proprio = proprio
        self.RESTING_STATE = resting if resting is not None else {}
        self.SELF_MODEL_EVOLUTION = {"parameters": {}}
        self.PERTURBATION_SCALE = 0.1

    def _get_proprio_status(self):
        return self._proprio


def test_criticality_ticks_without_calibration():
    import criticality_regulator as cr_mod

    cr_mod._criticality_regulator_instance = None          # fresh singleton
    mod = _StubModule({"ticks": 54, "avg_error": 0.52, "last_error": 0.0})
    for i in range(8):
        cr_mod._criticality_tick(mod, tick=i)
    cr = cr_mod.get_instance()
    assert cr.ticks == 8, f"regulator froze: ticks={cr.ticks} (old bug = 0)"
    st = cr_mod._get_criticality_status()
    assert st.get("error_source") == "proprio_avg_error", st.get("error_source")
    assert st.get("regime") in ("critical", "ordered", "chaotic", "transitional"), st


def test_criticality_prefers_calibration_record():
    import criticality_regulator as cr_mod

    cr_mod._criticality_regulator_instance = None
    mod = _StubModule(
        {"ticks": 54, "avg_error": 0.52},
        resting={"_proprio_calibration": {"original": "action", "gated": "integrator"}},
    )
    cr_mod._criticality_tick(mod, tick=1)
    # /status reads _get_criticality_status(), which is where the provenance is published
    st = cr_mod._get_criticality_status()
    assert st.get("error_source") == "proprio_calibration", st.get("error_source")


def test_no_phi_published_without_trajectory():
    """Static guard: the <10-trajectory branch must not assign phi_total."""
    src = (HERE / "tl_ego_server.py").read_text(encoding="utf-8", errors="replace")
    start = src.index("if len(traj) < 10:")
    end = src.index("1. Build type transition matrix", start)
    branch = src[start:end]
    assert 'IIT_STATE["phi_proxy_entropy_bits"] = round(H, 4)' in branch, "proxy not kept separately"
    assert 'phi_valid"] = False' in branch, "phi_valid flag missing"
    assert 'phi_total"] = round(H' not in branch, "proxy is still published as phi_total"
    assert 'phi_history"].append' not in branch, "proxy is still polluting phi_history"
    # and the real path must clear the flag again
    real = src[src.index("5. Update state", src.index("1. Build type transition matrix")):]
    assert 'IIT_STATE["phi_valid"] = True' in real, "real path never clears phi_valid"


def test_status_exposes_new_fields():
    src = (HERE / "tl_ego_server.py").read_text(encoding="utf-8", errors="replace")
    for key in ("phi_valid", "phi_unavailable_reason", "phi_proxy_entropy_bits", "proxy_skips"):
        assert f'"{key}"' in src, f"{key} missing from server"
    assert "error_source" in (HERE / "criticality_regulator.py").read_text(encoding="utf-8")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")

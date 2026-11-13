"""Recovered shim built 2026-10-15: the real source for this module was deleted
in the 10-14/15 tree sweep. The compiled twin (l10_gate_policy_bridge.pyc, byte-identical to the
last live source) sits next to this file: run it as a script, or re-export it on
import. Replace this file with real source when recovered.
"""
import os, runpy

_PYC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l10_gate_policy_bridge.pyc")
if __name__ == "__main__":
    runpy.run_path(_PYC, run_name="__main__")
else:
    _g = runpy.run_path(_PYC)
    globals().update({k: v for k, v in _g.items() if not k.startswith("__")})

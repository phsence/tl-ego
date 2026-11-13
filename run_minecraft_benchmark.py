"""Recovered shim built 2026-10-15: the real source for this module was deleted
in the 10-14/15 tree sweep. The compiled twin (run_minecraft_benchmark.pyc, byte-identical to the
last live source) sits next to this file: run it as a script, or re-export it on
import. Replace this file with real source when recovered.
"""
import os, runpy

_HERE = os.path.dirname(os.path.abspath(__file__))
_PYC = os.path.join(_HERE, "run_minecraft_benchmark.pyc")


def full_pool_argv(argv):
    """Default --holdout-limit to the WHOLE frozen pool (2026-10-30).

    The pyc's argparse default is 12 while the frozen holdout pool holds 100, so a
    plain run silently scores 12% of the frozen evidence and the gate's MDE sits at
    +0.38 instead of ~+0.09. Explicit --holdout-limit always wins.
    """
    argv = list(argv)
    if "--holdout-limit" in argv:
        return argv
    import sys
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import capability_suites.minecraft.tasks as _mc
    return argv + ["--holdout-limit", str(len(_mc.holdout_specs()))]


if __name__ == "__main__":
    import sys
    sys.argv = full_pool_argv(sys.argv)
    runpy.run_path(_PYC, run_name="__main__")
else:
    _g = runpy.run_path(_PYC)
    globals().update({k: v for k, v in _g.items() if not k.startswith("__")})

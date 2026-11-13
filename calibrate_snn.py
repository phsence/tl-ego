"""Calibrate snn_layer's NOISE_SIGMA / DRIVE_FRACTION. Run after any geometry change.

    python calibrate_snn.py            # sweep and print
    python calibrate_snn.py --apply    # also write the chosen pair into snn_layer.py

Why this exists: the layer shipped with noise std 0.012 against V_TH=1.0, which
can never fire, and full-weight drive, which is contagion (measured 0% or
70-86%/ms, never sparse). Both numbers are physical-model constants, so they get
measured rather than guessed. Kept in-repo so the next person re-runs the sweep
instead of re-inventing values.

Anchors from the 2026-09-30 run (n=20000, 60000 edges, cuda):
    sigma  drive  rate %/ms
    0.30   0.10   0.83      <- chosen (sparse, sub-threshold drive stays sub-threshold)
    0.50   0.10   4.07
"""
import sys

import torch

TAU_M, V_TH, STEPS, READ_LAST = 20.0, 1.0, 600, 100
LEAK = 0.8
TARGET_LO, TARGET_HI = 0.3, 5.0  # %/ms


def run(n, edges, sigma, drive_fraction, seed=0, steps=STEPS):
    dev = "cuda"
    t = torch
    torch.manual_seed(seed)
    idx_s = t.randint(0, n, (edges,), device=dev)
    idx_d = t.randint(0, n, (edges,), device=dev)
    w = t.rand(edges, device=dev)
    w = w / (w.mean() + 1e-6)
    decay = pow(2.718281828, -1.0 / TAU_M)
    v = t.zeros(n, device=dev)
    pre = t.zeros(n, device=dev)
    post = t.zeros(n, device=dev)
    last = t.zeros(n, device=dev)
    ltp_m = ltd_m = 0.0
    for step in range(steps):
        drive = t.zeros(n, device=dev)
        if step > 0 and drive_fraction > 0:
            drive.index_add_(0, idx_d, w * (pre[idx_s] > 0).float()) * drive_fraction
        v = v * decay * LEAK + drive + t.randn(n, device=dev) * sigma
        fired = v > V_TH
        v = t.where(fired, t.full_like(v, V_RESET), v)
        pre = pre * 0.95 + fired.float()
        post = t.zeros(n, device=dev)
        post.index_add_(0, idx_d, fired[idx_s].float())
        if step >= steps - READ_LAST:
            last += fired.float()
        if step == steps - 1:
            ltp = 0.01 * pre[idx_s] * post[idx_d]
            ltd = 0.012 * pre[idx_d] * post[idx_s]
            ltp_m, ltd_m = float(ltp.sum()), float(ltd.sum())
    return int(last.sum()), float(last.mean() / READ_LAST), ltp_m, ltd_m


def main():
    sizes = ((20000, 60000), (200000, 400000))
    print("decay*leak=%.4f  V_TH=%.1f  band=%.1f-%.1f %%/ms"
          % (pow(2.718281828, -1.0 / TAU_M) * LEAK, V_TH, TARGET_LO, TARGET_HI))
    best = None
    for sigma in (0.20, 0.30, 0.40, 0.50):
        for n, edges in sizes:
            f, r, a, mi = run(n, edges, sigma, 0.10)
            pct = r * 100
            ok = TARGET_LO <= pct <= TARGET_HI
            if ok and best is None:
                best = (sigma, 0.10, n, edges, pct)
            print("n=%-7d edges=%-7d sigma=%.2f drive=0.10  fired=%-9d rate=%7.3f%%/ms "
                  "ltp=%-9.1f net=%-8.1f%s"
                  % (n, edges, sigma, f, pct, a, a - mi, "  <-- in band" if ok else ""))
    if best is None:
        print("\nNO setting landed in band. Widen the sigma grid or reconsider "
              "V_TH/LEAK before shipping a number.")
        return 1
    print("\nchosen: NOISE_SIGMA=%.2f DRIVE_FRACTION=%.2f (measured %.3f%%/ms on "
          "n=%d edges=%d)" % (best[0], best[1], best[4], best[2], best[3]))
    if "--apply" in sys.argv:
        import re
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snn_layer.py")
        src = open(p, encoding="utf-8").read()
        for name, val in (("NOISE_SIGMA", best[0]), ("DRIVE_FRACTION", best[1])):
            src, n = re.subn(r"^%s = [\d.]+$" % name, "%s = %.2f" % (name, val),
                             src, count=1, flags=re.M)
            assert n == 1, "failed to patch %s" % name
        open(p, "w", encoding="utf-8", newline="").write(src)
        print("written into snn_layer.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

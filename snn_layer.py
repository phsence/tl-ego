"""snn_layer.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py imports this module guarded by
  EGO_GPU_SNN == "1"  (and only then sets _SNN_LAYER; the caller logs "snn_layer
  unavailable ... gated by EGO_GPU_SNN" otherwise). Call site:
      _r = _SNN_LAYER.snn_step(triples, neuron_ids, write_back_callback)
      _r["fired_last"]; _r.get("ltp"); _r.get("ltd"); _r.get("ms")
  and snn_status() for the /status snn block. triples = sampled sparse edges
  (src, dst, weight); write_back(callback) pushes STDP deltas back.
Judgement (not measured): the original drive protocol / layer geometry is unrecoverable.
Implemented as a real LIF + pair-based STDP step on GPU (cuda) over the triple list, with
an explicit no-drive simplification: membrane noise drives the net, spiking rates are read
from the last 100 ms. Torch is imported lazily so a torch-less host simply disables it.
"""

STEPS = 600          # 1 ms steps -> 600 ms integration window
READ_LAST = 100      # read firing rates from the last 100 ms
TAU_M = 20.0
V_TH = 1.0
V_RESET = 0.0
STDP_A_PLUS = 0.01
STDP_A_MINUS = 0.012
# 2026-09-30 (measured, see scripts/calibrate_snn.py): the old 0.08*0.15=0.012 std
# can never reach V_TH=1.0 -> the layer emitted zeros forever. NOISE_SIGMA is the
# std of the per-step membrane noise; steady-state std is 1.54x that. 0.30 measured
# 0.83%/ms sparse firing, 0.50 -> 4.07%/ms. Exposed as a knob, not hardcoded, because
# this is a physical model: re-run the sweep if the graph's degree distribution moves.
NOISE_SIGMA = 0.30
# 2026-09-30: DRIVE_FRACTION caps what one pre-synaptic spike delivers. The original
# drove every synapse at full normalised weight (w/mean == 1 == V_TH), which is
# contagion: measured either 0 spikes or 70-86%/ms synchronisation, never sparse.
# 0.10 keeps sub-threshold drive sub-threshold so only summed input can fire.
DRIVE_FRACTION = 0.10


def _neuron_index(nid):
    """Neuron ids are prefixed strings ('n-action-0353068'), not ints.
    int('n-action-...') raised for every triple and the except-continue hid it
    behind neurons=0. Keep the raw key's suffix so different types stay distinct."""
    if isinstance(nid, int):
        return nid
    s = str(nid)
    if s.lstrip("-").isdigit():
        return int(s)
    tail = s.rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    raise ValueError("unmappable neuron id: %r" % (nid,))


def _torch():
    import torch  # noqa: F401  (lazy: ImportError disables the layer upstream)
    return torch


def available():
    try:
        return bool(_torch().cuda.is_available())
    except Exception:
        return False


def snn_status():
    try:
        t = _torch()
    except Exception as e:
        return {"available": False, "reason": "torch missing: %s" % e}
    return {"available": bool(t.cuda.is_available()),
            "device": "cuda" if t.cuda.is_available() else "cpu",
            "steps": STEPS, "read_last": READ_LAST,
            "reason": "" if t.cuda.is_available() else "no cuda device"}


def snn_step(triples, neuron_ids=None, write_back=None, steps=STEPS):
    """One LIF+STDP pass over the sampled edges. Returns None when no GPU (caller skips)."""
    if not available():
        return None
    t = _torch()
    if not triples:
        return {"fired_last": 0.0, "ltp": 0.0, "ltd": 0.0, "ms": 0.0, "neurons": 0}
    import time
    t0 = time.perf_counter()
    dev = "cuda"
    idx = {}
    src_l, dst_l, w_l = [], [], []
    skipped = 0
    for tr in triples:
        try:
            s, d, w = _neuron_index(tr[0]), _neuron_index(tr[1]), float(tr[2])
        except Exception:
            skipped += 1
            continue
        for n in (s, d):
            if n not in idx:
                idx[n] = len(idx)
        src_l.append(idx[s]); dst_l.append(idx[d]); w_l.append(abs(w))
    n = len(idx)
    if n == 0:
        return {"fired_last": 0, "fired_rate": 0.0, "ltp": 0.0, "ltd": 0.0,
                "ms": (time.perf_counter() - t0) * 1000.0,
                "neurons": 0, "edges": 0, "skipped_ids": skipped}
    src = t.tensor(src_l, dtype=t.long, device=dev)
    dst = t.tensor(dst_l, dtype=t.long, device=dev)
    w = t.tensor(w_l, dtype=t.float32, device=dev)
    w = w / (w.mean() + 1e-6)
    v = t.zeros(n, device=dev)
    pre_tr = t.zeros(n, device=dev)
    post_tr = t.zeros(n, device=dev)
    last_fires = t.zeros(n, device=dev)
    decay = pow(2.718281828, -1.0 / TAU_M)
    # 2026-09-30: these are only assigned inside the loop when write_back is not None
    # (and only on the last step). Init BEFORE the loop, not after — an init after
    # the loop silently overwrites the measured values with 0.0.
    ltp_m = ltd_m = 0.0
    for step in range(int(steps)):
        noise = t.randn(n, device=dev) * NOISE_SIGMA
        drive = t.zeros(n, device=dev)
        if step > 0:
            drive.index_add_(0, dst, w * (pre_tr[src] > 0).float()) * DRIVE_FRACTION
        v = v * decay * (1 - 0.2) + drive + noise
        fired = v > V_TH
        v = t.where(fired, t.full_like(v, V_RESET), v)
        pre_tr = pre_tr * 0.95 + fired.float()
        post_tr = t.zeros_like(post_tr)
        post_tr.index_add_(0, dst, fired[src].float())
        if step >= int(steps) - READ_LAST:
            last_fires += fired.float()
        if write_back is not None and step == int(steps) - 1:
            ltp = STDP_A_PLUS * pre_tr[src] * post_tr[dst]
            ltd = STDP_A_MINUS * pre_tr[dst] * post_tr[src]
            try:
                write_back(src_l, dst_l, (ltp - ltd).detach().cpu().tolist())
            except Exception:
                pass
            ltp_m, ltd_m = float(ltp.sum()), float(ltd.sum())
    t.cuda.synchronize()
    # 2026-09-30: fired_last = spike COUNT over the read window (the name and the server's
    # accumulator both read as a count); fired_rate keeps the mean-rate view.
    return {"fired_last": int(last_fires.sum()),
            "fired_rate": float(last_fires.mean() / READ_LAST),
            "ltp": ltp_m, "ltd": ltd_m, "ms": (time.perf_counter() - t0) * 1000.0,
            "neurons": n, "edges": len(src_l), "skipped_ids": skipped}


if __name__ == "__main__":
    st = snn_status()
    print("snn_status:", st)
    # id mapping is independent of cuda — always assert it
    assert _neuron_index("n-action-0353068") == 353068
    assert _neuron_index("12345") == 12345
    assert _neuron_index(999) == 999
    for bad in ("x", "n-action-", ""):
        try:
            _neuron_index(bad)
            raise AssertionError("should not map: %r" % (bad,))
        except ValueError:
            pass
    if not st["available"]:
        assert snn_step([(1, 2, 0.5)], [1, 2]) is None
        assert st["available"] is False
        print("snn_layer selfcheck OK (no cuda -> gate keeps it dormant, as designed)")
    else:
        seen = []
        r = snn_step([(1, 2, 0.5), (2, 3, 0.5), (3, 1, 0.5)], [1, 2, 3],
                     write_back=lambda s, d, dw: seen.append((s, d, dw)), steps=80)
        assert r and r["neurons"] == 3 and r["edges"] == 3, r
        assert seen and len(seen[0][2]) == 3
        assert snn_step([], [1])["neurons"] == 0
        assert snn_step([("x", "y")], None)["neurons"] == 0
        # the real server shape: prefixed string ids
        rs = snn_step([("n-action-1", "n-doubt-2", 0.5), ("n-doubt-2", "n-action-1", 0.5)],
                      None, steps=80)
        assert rs["neurons"] == 2 and rs["edges"] == 2 and rs["skipped_ids"] == 0, rs
        # firing must be SPARSE, not synchronised (the 2026-09-30 contagion bug).
        # 4000 distinct srcs + 500 dsts = 4000 neurons; pre-2026-09-30 this hit
        # 70-86%/ms, and the old id path never got here at all (neurons=0).
        big = snn_step([("n-action-%d" % i, "n-doubt-%d" % ((i * 7) % 500), 0.5)
                        for i in range(4000)], None, steps=200)
        rate = big["fired_rate"] * 100
        assert big["neurons"] == 4000 and big["edges"] == 4000, big
        assert 0.0 <= rate <= 15.0, "firing rate %.2f%%/ms out of band (synchronised?)" % rate
        # the writer MUST receive real STDP magnitudes. A 0.0 here once hid for a
        # whole day behind an init placed after the loop.
        w_seen = []
        wb = snn_step([("n-action-%d" % i, "n-doubt-%d" % ((i * 7) % 500), 0.5)
                       for i in range(4000)], None, steps=200,
                      write_back=lambda s, d, dw: w_seen.append(dw))
        assert wb["ltp"] > 0 and wb["ltd"] > 0, "STDP magnitudes are zero: %r" % (wb,)
        assert w_seen and any(v != 0.0 for row in w_seen for v in row), "write_back got all zeros"
        print("snn_layer selfcheck OK | sparse %d neurons, %.3f%%/ms, ltp=%.1f ltd=%.1f"
              % (big["neurons"], rate, wb["ltp"], wb["ltd"]))
        print("   sample:", {k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()})

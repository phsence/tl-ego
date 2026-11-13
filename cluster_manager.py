"""cluster_manager.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance (call sites in tl_ego_server.py; nothing else invented):
  2504/2515  get_cluster_mgr().fit(aligned_codes, neuron_ids, tick=...)   # refit after codec bg-train
  2579/2599  get_cluster_mgr().fit(aligned_codes, neuron_ids, tick=...)   # P4 fit on Pulse-Codec latents
  17649      get_cluster_mgr().log_activation(seeds, tick)                # P8 hotspot tracking
  23096      get_cluster_mgr()._fitted                                   # gate before log_activation
  23299      get_cluster_stats()                                          # /status["cluster"]
  24911/24914 _fitted + log_activation                                    # P8, telemetry-only
Last live /status reported  cluster = {"enabled": false}  (fit never succeeded on 10M-mode),
so an unfitted manager must report exactly {"enabled": false}.

k-means is numpy-only (no sklearn on this box).
# ponytail: 20k-row subsample cap + 20 iters; raise when cluster quality actually matters.
"""

from __future__ import annotations

import time

import numpy as np

K = 64
SUBSAMPLE_CAP = 20_000
ITERS = 20

_MGR = None


class ClusterManager:
    def __init__(self, k=K, seed=0):
        self.k = int(k)
        self._fitted = False
        self.centroids = None          # (k, dim)
        self._neuron_ids = None        # (n,) ids aligned with the fit matrix rows
        self._assign = None            # (n,) cluster index per fitted neuron
        self._activations = {}         # cluster idx -> count (bounded, hotspot tracking)
        self.last_fit_tick = None
        self.last_fit_ts = None
        self.fitted_neurons = 0
        self._rng = np.random.default_rng(seed)

    # ── fit ────────────────────────────────────────────────────────────────
    def fit(self, matrix, neuron_ids=None, tick=None):
        x = np.asarray(matrix, dtype=np.float32)
        if x.ndim != 2 or x.shape[0] < self.k or x.shape[1] < 1:
            return {"fitted": False, "reason": f"shape {x.shape}"}
        ids = list(neuron_ids) if neuron_ids is not None else list(range(x.shape[0]))
        if len(ids) != x.shape[0]:
            return {"fitted": False, "reason": f"ids {len(ids)} != rows {x.shape[0]}"}
        if x.shape[0] > SUBSAMPLE_CAP:
            idx = np.sort(self._rng.choice(x.shape[0], SUBSAMPLE_CAP, replace=False))
        else:
            idx = np.arange(x.shape[0])
        xs = x[idx]
        # k-means++-lite: random init from the sample, then Lloyd iterations
        cent = xs[self._rng.choice(xs.shape[0], self.k, replace=False)].copy()
        assign = np.zeros(xs.shape[0], dtype=np.int32)
        for _ in range(ITERS):
            # blocked distance to keep peak memory bounded (10M-mode safety)
            assign = np.empty(xs.shape[0], dtype=np.int32)
            for s in range(0, xs.shape[0], 4096):
                blk = xs[s:s + 4096]
                dd = (blk ** 2).sum(axis=1)[:, None] - 2 * blk @ cent.T + (cent ** 2).sum(axis=1)[None, :]
                assign[s:s + 4096] = dd.argmin(axis=1)
            moved = 0
            for c in range(self.k):
                m = assign == c
                if m.any():
                    new = xs[m].mean(axis=0)
                    moved += int(not np.allclose(new, cent[c], atol=1e-5))
                    cent[c] = new
            if not moved:
                break
        self.centroids = cent
        self._neuron_ids = ids
        self._assign = assign
        self._fitted = True
        self.fitted_neurons = int(x.shape[0])
        self.last_fit_tick = tick
        self.last_fit_ts = time.time()
        return {"fitted": True, "k": self.k, "neurons": self.fitted_neurons, "sampled": int(xs.shape[0])}

    # ── P8 hotspot tracking (telemetry only, must never raise) ─────────────
    def log_activation(self, seeds, tick=None):
        if not self._fitted or self._assign is None or not seeds:
            return 0
        pos = getattr(self, "_id_pos", None)
        if pos is None:
            pos = {nid: i for i, nid in enumerate(self._neuron_ids or [])}
            self._id_pos = pos
        n = 0
        for s in seeds:
            i = pos.get(s)
            if i is None:
                continue
            c = int(self._assign[i])
            if i >= self._assign.shape[0]:
                continue
            self._activations[c] = self._activations.get(c, 0) + 1
            n += 1
            if len(self._activations) > 4 * self.k:      # bounded
                for kk in sorted(self._activations, key=self._activations.get)[:self.k]:
                    self._activations.pop(kk, None)
        return n


def get_cluster_mgr():
    global _MGR
    if _MGR is None:
        _MGR = ClusterManager()
    return _MGR


def get_cluster_stats():
    m = _MGR
    if m is None or not m._fitted:
        return {"enabled": False}          # matches the last live /status
    hot = sorted(m._activations.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "enabled": True,
        "k": m.k,
        "neurons": m.fitted_neurons,
        "last_fit_tick": m.last_fit_tick,
        "hotspots": [{"cluster": int(c), "activations": int(n)} for c, n in hot],
    }


if __name__ == "__main__":  # self-check: unfitted shape must match the live /status
    assert get_cluster_stats() == {"enabled": False}
    rng = np.random.default_rng(1)
    x = np.vstack([rng.normal(0, .1, (200, 8)), rng.normal(5, .1, (200, 8))]).astype(np.float32)
    res = get_cluster_mgr().fit(x, list(range(400)), tick=7)
    assert res["fitted"], res
    st = get_cluster_stats()
    assert st["enabled"] and st["k"] == K
    # two well-separated blobs must land in >=2 clusters, and activation logging must be id-addressable
    assert len(set(get_cluster_mgr()._assign.tolist())) >= 2
    assert get_cluster_mgr().log_activation([0, 1, 2], tick=8) == 3
    print("cluster_manager self-check OK", st)

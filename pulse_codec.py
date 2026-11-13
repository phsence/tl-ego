"""
P3 脉冲压缩编解码 — Pulse Codec
================================
每个 neuron type 独立训练微型编解码器（PCA → 32d），
将 190K × 1024 float32 嵌入压缩到 ~32d per neuron = 32× 存储缩减。

集成点:
  - neural_grid.py  init_grid() → fit + encode → swap _embeddings_matrix
  - tl_ego_server.py   init()    → NEURON_EMBED 从 decoded 重建
  - /status            暴露 codec 统计
  - standalone train   独立训练脚本，用于首次压缩

架构:
  PulseCodec
  ├── per-type PCA encoder (1024d → 32d)
  ├── codec_matrix       (N_neurons, 32)   ← 压缩后嵌入矩阵
  ├── decoder_matrix     (32, 1024)        ← 投影恢复矩阵（per-type mean + components）
  └── type_map           neuron_id → type → codec_idx

Storage format (.lae/pulse_codec.pkl):
  {
    "version": 1,
    "n_neurons": N,
    "n_components": 32,
    "neuron_ids": [...],
    "type_map": {neuron_id: type_str},
    "codec_meta": {type_str: {"mean": [...], "components": [[...], ...], "variance_explained": float}},
    "codes": np.ndarray (N, 32),   # float32
  }
"""

import threading
import time
import pickle
import json
import numpy as np
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

def _find_workspace() -> Path:
    """Locate the workspace root: the directory that contains .lae/.

    Walk up from __file__ location, preferring the directory whose .lae/
    contains pulse_codec.pkl (if present).
    """
    here = Path(__file__).resolve().parent
    candidates = []
    for p in [here] + list(here.parents):
        lae = p / ".lae"
        if lae.is_dir():
            has_pkl = (lae / "pulse_codec.pkl").exists()
            candidates.append((p, has_pkl, len(str(p))))
    if not candidates:
        return Path.cwd()
    # prefer candidate with pulse_codec.pkl, then shortest path
    candidates.sort(key=lambda x: (-x[1], x[2]))
    return candidates[0][0]

_WORKSPACE = _find_workspace()
_CODEC_PKL_PATH = _WORKSPACE / ".lae" / "pulse_codec.pkl"

# ─── 配置 ────────────────────────────────────────────────────────────
N_COMPONENTS = 64          # PCA 目标维度
# 2026-09-30 (修复 64vs32 播放错): 磁盘上的拟合产物
# (~/.lae/pulse_codec.pkl: 190000 neurons x 64 codes, 19 types, components(64,1024))
# 是活着的代码写的，而本文件是从 09-11 备份恢复的旧版(写着 32) -> 以产物为准。
# 换拟合产物时必须同步这个值。
MIN_TYPE_SAMPLES = 50      # 最少样本数才训练 codec
MIN_NEURONS_FOR_CODEC = 1000  # 最少神经元数量才启用 codec

# ─── 统计 ────────────────────────────────────────────────────────────
_stats_lock = threading.Lock()
_codec_stats = {
    "enabled": False,
    "n_neurons_original": 0,
    "n_neurons_compressed": 0,
    "n_types_trained": 0,
    "n_types_skipped": 0,
    "compression_ratio": 0.0,
    "original_size_mb": 0.0,
    "compressed_size_mb": 0.0,
    "total_variance_explained": 0.0,
    "last_train_tick": 0,
    "last_encode_ts": 0,
    "total_encodes": 0,
    "total_decodes": 0,
}


def set_workspace(workspace_path):
    global _WORKSPACE, _CODEC_PKL_PATH
    _WORKSPACE = Path(workspace_path)
    _CODEC_PKL_PATH = _WORKSPACE / ".lae" / "pulse_codec.pkl"


def get_codec_path() -> Path:
    return _CODEC_PKL_PATH


class PulseCodec:
    """
    Per-type PCA pulse codec.
    1024-d float32 embedding → 32-d float32 code (32× compression).
    """

    def __init__(self):
        self._lock = threading.Lock()

        # Per-type codec data: type_str → {"mean", "components", "variance_explained"}
        self._type_codecs: Dict[str, dict] = {}

        # neuron_id → type_str mapping
        self._type_map: Dict[str, str] = {}

        # The compressed codes matrix (N_neurons, 32)
        self._codes: Optional[np.ndarray] = None

        # neuron_ids in same order as _codes rows
        self._neuron_ids: Optional[List[str]] = None

        # Whether codec has been fitted
        self._fitted: bool = False

        # Decoder cache: type_str → decoder_matrix (32, 1024) precomputed
        self._decoder_cache: Dict[str, np.ndarray] = {}

    # ────────────────────────────────────────────────────────────────
    # Training
    # ────────────────────────────────────────────────────────────────

    def fit(self, embeddings: np.ndarray, neuron_ids: List[str],
            neuron_types: Dict[str, str]) -> dict:
        """
        Train per-type PCA codecs and compress all embeddings.

        Args:
            embeddings:  (N, 1024) float32 numpy array
            neuron_ids:  list of N neuron ID strings
            neuron_types: dict neuron_id → type_str

        Returns:
            stats dict
        """
        if len(neuron_ids) < MIN_NEURONS_FOR_CODEC:
            return {"enabled": False, "reason": f"too few neurons ({len(neuron_ids)} < {MIN_NEURONS_FOR_CODEC})"}

        # 2026-08-25 fix: NEURON_TYPE values may be role_scores dicts (P3).
        # Normalize to primary-role strings so type grouping keys stay hashable.
        neuron_types = {nid: _primary_role_str(v) for nid, v in neuron_types.items()}

        n_orig = embeddings.nbytes / (1024 * 1024)  # bytes → MB
        n_neurons, n_dims = embeddings.shape

        with self._lock:
            self._neuron_ids = list(neuron_ids)
            self._type_map = dict(neuron_types)

            # Group embeddings by neuron type
            type_groups: Dict[str, List[int]] = defaultdict(list)
            for i, nid in enumerate(neuron_ids):
                t = neuron_types.get(nid, "unknown")
                type_groups[t].append(i)

            # Train per-type PCA
            codec_list = []
            type_assign = np.empty(n_neurons, dtype=np.int32)
            types_trained = 0
            types_skipped = 0
            total_var_explained = 0.0
            n_with_codec = 0

            for type_str, indices in type_groups.items():
                if len(indices) < MIN_TYPE_SAMPLES:
                    types_skipped += 1
                    type_assign[indices] = -1
                    continue

                type_embs = embeddings[indices]  # (n_type, 1024)
                mean = type_embs.mean(axis=0)    # (1024,)
                centered = type_embs - mean

                # SVD-based PCA (no sklearn dependency)
                try:
                    u, s, vt = np.linalg.svd(centered, full_matrices=False)
                    components = vt[:N_COMPONENTS]  # (32, 1024)
                    var_explained = float(s[:N_COMPONENTS].sum() / s.sum()) if s.sum() > 0 else 0.0
                except np.linalg.LinAlgError:
                    types_skipped += 1
                    type_assign[indices] = -1
                    continue

                self._type_codecs[type_str] = {
                    "mean": mean.astype(np.float32),
                    "components": components.astype(np.float32),
                    "variance_explained": var_explained,
                    "n_samples": len(indices),
                }
                codec_list.append((type_str, len(indices), var_explained))
                type_assign[indices] = types_trained
                types_trained += 1
                total_var_explained += var_explained * len(indices)
                n_with_codec += len(indices)

            if types_trained == 0:
                self._fitted = False
                return {"enabled": False, "reason": "no type had enough samples"}

            # Compute global codes for all neurons
            codes = np.empty((n_neurons, N_COMPONENTS), dtype=np.float32)

            for type_str, codec_data in self._type_codecs.items():
                indices = type_groups[type_str]
                type_embs = embeddings[indices]
                mean = codec_data["mean"]
                components = codec_data["components"]  # (32, 1024)
                centered = type_embs - mean
                # Project to latent space: (n_type, 1024) @ (1024, 32) = (n_type, 32)
                codes[indices] = centered @ components.T

            # Fill neurons without codec with zeros
            no_codec_mask = type_assign == -1
            if no_codec_mask.any():
                codes[no_codec_mask] = 0.0

            self._codes = codes
            self._fitted = True

            # Build decoder cache (type_str → (32, 1024) matrix)
            # NOTE: components is (32, 1024), decode_single does code @ decoder
            # so decoder must be (32, 1024), NOT (1024, 32)
            for type_str, codec_data in self._type_codecs.items():
                self._decoder_cache[type_str] = (
                    codec_data["components"]  # (32, 1024)
                )

            avg_var = total_var_explained / n_with_codec if n_with_codec > 0 else 0.0
            comp_size_mb = codes.nbytes / (1024 * 1024)
            comp_ratio = n_orig / comp_size_mb if comp_size_mb > 0 else 0.0

            with _stats_lock:
                _codec_stats["enabled"] = True
                _codec_stats["n_neurons_original"] = n_neurons
                _codec_stats["n_neurons_compressed"] = n_neurons
                _codec_stats["n_types_trained"] = types_trained
                _codec_stats["n_types_skipped"] = types_skipped
                _codec_stats["compression_ratio"] = round(comp_ratio, 1)
                _codec_stats["original_size_mb"] = round(n_orig, 1)
                _codec_stats["compressed_size_mb"] = round(comp_size_mb, 1)
                _codec_stats["total_variance_explained"] = round(avg_var, 4)
                _codec_stats["last_train_tick"] = int(time.time())

            print(f"[pulse_codec] Fitted {types_trained} type codecs, "
                  f"{n_neurons} neurons → {N_COMPONENTS}d, "
                  f"{n_orig:.0f}MB → {comp_size_mb:.1f}MB ({comp_ratio:.1f}×)",
                  flush=True)
            for t, cnt, var in codec_list:
                print(f"  [{t}] {cnt} neurons, var_explained={var:.3f}", flush=True)

            return {
                "enabled": True,
                "n_neurons": n_neurons,
                "n_types_trained": types_trained,
                "n_types_skipped": types_skipped,
                "n_components": N_COMPONENTS,
                "original_size_mb": round(n_orig, 1),
                "compressed_size_mb": round(comp_size_mb, 1),
                "compression_ratio": round(comp_ratio, 1),
                "avg_variance_explained": round(avg_var, 4),
            }

    # ────────────────────────────────────────────────────────────────
    # Encode / Decode
    # ────────────────────────────────────────────────────────────────

    def encode(self, embeddings: np.ndarray, neuron_ids: List[str],
               neuron_types: Dict[str, str]) -> np.ndarray:
        """
        Compress (N, 1024) embeddings to (N, 32) codes using trained codecs.
        If embeddings are all-zero, return cached codes (read-only).
        If embeddings have data, re-encode and UPDATE the cached _codes in-place.
        """
        if not self._fitted:
            # 2026-09-01 fix: read-only fallback is not an encode — do not
            # inflate total_encodes (stats-only, no behavior change).
            with _stats_lock:
                _codec_stats["last_encode_ts"] = time.time()
            return self._codes_for_ids(neuron_ids) if self._codes is not None else np.zeros((len(neuron_ids), N_COMPONENTS), dtype=np.float32)

        # Detect if caller passed actual data (non-zero embeddings)
        _has_data = bool(np.any(embeddings)) if embeddings.size > 0 else False
        if not _has_data and self._codes is not None:
            # Read-only: return cached codes
            with _stats_lock:
                # 2026-09-01 fix: cache reads are not encodes — count them
                # separately so total_encodes reflects real re-encodes only.
                _codec_stats["read_cache_hits"] = _codec_stats.get("read_cache_hits", 0) + len(neuron_ids)
                _codec_stats["last_encode_ts"] = time.time()
            return self._codes_for_ids(neuron_ids)

        # Re-encode from actual embeddings (STDP-modified)
        n = len(neuron_ids)
        codes = np.zeros((n, N_COMPONENTS), dtype=np.float32)
        nid_to_idx = {nid: i for i, nid in enumerate(self._neuron_ids)} if self._neuron_ids else {}
        for i, nid in enumerate(neuron_ids):
            t = neuron_types.get(nid, "unknown")
            # P3 migration: NEURON_TYPE values are now role_scores dicts, not
            # strings. `dict in dict` raises TypeError: unhashable type.
            if isinstance(t, dict):
                t = max(t, key=t.get) if t else "unknown"
            if t in self._type_codecs:
                cd = self._type_codecs[t]
                centered = embeddings[i] - cd["mean"]
                codes[i] = centered @ cd["components"].T
                # Update cached _codes in-place if this neuron exists in the global map
                if nid in nid_to_idx and self._codes is not None:
                    self._codes[nid_to_idx[nid]] = codes[i]
        with _stats_lock:
            _codec_stats["total_encodes"] += n
            _codec_stats["last_encode_ts"] = time.time()
        return codes

    def decode(self, neuron_ids: List[str]) -> np.ndarray:
        """
        Reconstruct (N, 1024) embeddings from (N, 32) codes.
        Uses per-type decoder matrix.
        """
        if self._codes is None or self._neuron_ids is None:
            raise RuntimeError("PulseCodec not fitted")

        nid_to_idx = {nid: i for i, nid in enumerate(self._neuron_ids)}
        missing = [nid for nid in neuron_ids if nid not in nid_to_idx]
        if missing:
            raise KeyError(f"{len(missing)} neuron IDs not in codec vocabulary")

        idxs = np.array([nid_to_idx[nid] for nid in neuron_ids])
        codes = self._codes[idxs]  # (N, 32)

        # Decode per neuron
        result = np.empty((len(neuron_ids), 1024), dtype=np.float32)
        for i, nid in enumerate(neuron_ids):
            t = self._type_map.get(nid, "unknown")
            cd = self._type_codecs.get(t)
            if cd is not None:
                # (32,) @ (32, 1024) + (1024,) = (1024,)
                components = cd.get("components")
                if components is not None:
                    result[i] = codes[i] @ components + cd["mean"]
                else:
                    result[i] = 0.0
            else:
                result[i] = 0.0

        with _stats_lock:
            _codec_stats["total_decodes"] += len(neuron_ids)
        return result

    def decode_single(self, neuron_id: str) -> Optional[np.ndarray]:
        """Decode a single neuron's embedding from its code."""
        if self._codes is None or self._neuron_ids is None:
            return None
        try:
            idx = self._neuron_ids.index(neuron_id)
        except ValueError:
            return None
        t = self._type_map.get(neuron_id, "unknown")
        cd = self._type_codecs.get(t)
        if cd is None:
            return None
        code = self._codes[idx]  # (32,)
        vec = code @ self._decoder_cache[t] + cd["mean"]  # (32,) @ (32, 1024) = (1024,)
        with _stats_lock:
            _codec_stats["total_decodes"] += 1
        return vec.astype(np.float32)

    def _codes_for_ids(self, neuron_ids: List[str]) -> np.ndarray:
        """Return pre-computed codes for given neuron IDs."""
        if self._codes is None or self._neuron_ids is None:
            raise RuntimeError("PulseCodec not fitted")
        nid_to_idx = {nid: i for i, nid in enumerate(self._neuron_ids)}
        idxs = [nid_to_idx.get(nid) for nid in neuron_ids]
        result = np.zeros((len(neuron_ids), N_COMPONENTS), dtype=np.float32)
        for j, idx in enumerate(idxs):
            if idx is not None:
                result[j] = self._codes[idx]
        return result

    # ────────────────────────────────────────────────────────────────
    # Persistence
    # ────────────────────────────────────────────────────────────────

    def save(self, path: Optional[Path] = None):
        """Save codec to pickle."""
        p = path or _CODEC_PKL_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".pkl.tmp")
        state = {
            "version": 1,
            "n_neurons": len(self._neuron_ids) if self._neuron_ids else 0,
            "n_components": N_COMPONENTS,
            "neuron_ids": self._neuron_ids,
            "type_map": self._type_map,
            "codec_meta": {
                t: {k: (v.tolist() if isinstance(v, np.ndarray) else v)
                    for k, v in cd.items()}
                for t, cd in self._type_codecs.items()
            },
            "codes": self._codes,
            "fitted": self._fitted,
            "decoder_cache": {t: m.tolist() for t, m in self._decoder_cache.items()},
        }
        try:
            with open(tmp, "wb") as f:
                pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(p)
            print(f"[pulse_codec] Saved to {p} ({p.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
        except Exception as e:
            print(f"[pulse_codec] Save error: {e}", flush=True)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "PulseCodec":
        """Load codec from pickle."""
        p = path or _CODEC_PKL_PATH
        if not p.exists():
            raise FileNotFoundError(f"Codec file not found: {p}")
        codec = cls()
        with open(p, "rb") as f:
            state = pickle.load(f)
        codec._neuron_ids = state["neuron_ids"]
        codec._type_map = state["type_map"]
        codec._fitted = state.get("fitted", False)
        codec._codes = state["codes"]
        codec._type_codecs = {}
        for t, cd in state.get("codec_meta", {}).items():
            codec._type_codecs[t] = {
                "mean": np.array(cd["mean"], dtype=np.float32),
                "components": np.array(cd["components"], dtype=np.float32),
                "variance_explained": cd["variance_explained"],
                "n_samples": cd.get("n_samples", 0),
            }
        codec._decoder_cache = {
            t: np.array(m, dtype=np.float32)
            for t, m in state.get("decoder_cache", {}).items()
        }
        print(f"[pulse_codec] Loaded from {p}: {state['n_neurons']} neurons, "
              f"{len(codec._type_codecs)} types, fitted={codec._fitted}", flush=True)
        # Rebuild runtime stats from loaded state
        _n = state.get("n_neurons", 0)
        _types = len(codec._type_codecs)
        _n_comp = state.get("n_components", N_COMPONENTS)
        if _n > 0 and _n_comp > 0:
            _orig_mb = _n * 1024 * 4 / 1024 / 1024
            _comp_mb = _n * _n_comp * 4 / 1024 / 1024
            _ratio = 1024 / _n_comp if _n_comp > 0 else 0
            _vars = [cd.get("variance_explained", 0) for cd in codec._type_codecs.values()]
            _avg_var = sum(_vars) / len(_vars) if _vars else 0
        else:
            _orig_mb = _comp_mb = _ratio = _avg_var = 0
        with _stats_lock:
            _codec_stats.update({
                "enabled": True,
                "n_neurons_original": _n,
                "n_neurons_compressed": _n,
                "n_types_trained": _types,
                "n_types_skipped": 0,
                "compression_ratio": round(_ratio, 1),
                "original_size_mb": round(_orig_mb, 1),
                "compressed_size_mb": round(_comp_mb, 1),
                "total_variance_explained": round(_avg_var, 4),
                "last_train_tick": 0,
                "last_retrain_tick": 0,
                "total_encodes": 0,
                "total_decodes": 0,
            })
        return codec

    # ────────────────────────────────────────────────────────────────
    # Stats
    # ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        with _stats_lock:
            return dict(_codec_stats)

    def is_fitted(self) -> bool:
        return self._fitted

    def get_compressed_codes(self) -> Optional[np.ndarray]:
        """Return the (N, 32) codes matrix for direct replacement of _embeddings_matrix."""
        return self._codes

    # ────────────────────────────────────────────────────────────────
    # P6: Cold Neuron Store API
    # ────────────────────────────────────────────────────────────────

    def get_compressed_code(self, neuron_id: str) -> Optional[np.ndarray]:
        """Return the 32d code for a single neuron. Used by ColdNeuronStore."""
        if self._codes is None or self._neuron_ids is None:
            return None
        try:
            idx = self._neuron_ids.index(neuron_id)
            return self._codes[idx].copy()  # (32,) float32
        except ValueError:
            return None

    def decode_single(self, code: np.ndarray, neuron_type: str) -> Optional[np.ndarray]:
        """
        Decode a single 32d code to 1024d embedding using per-type decoder.
        Uses components (32, 1024) directly to avoid decoder_cache shape issues.
        """
        cd = self._type_codecs.get(neuron_type)
        if cd is None:
            return None
        components = cd.get("components")  # (32, 1024)
        if components is None:
            return None
        # code (32,) @ components (32, 1024) = (1024,)
        vec = np.asarray(code, dtype=np.float32) @ components + cd["mean"]
        with _stats_lock:
            _codec_stats["total_decodes"] += 1
        return vec.astype(np.float32)

    def inject_code(self, neuron_id: str, code: np.ndarray):
        """
        Replace a single neuron's 32d code in the codes matrix.
        Used by ColdNeuronStore.flush_dirty_to_codec().
        """
        if self._codes is None or self._neuron_ids is None:
            return
        try:
            idx = self._neuron_ids.index(neuron_id)
            self._codes[idx] = np.asarray(code, dtype=np.float32).ravel()
        except ValueError:
            pass

    def get_nid_to_code_idx(self) -> dict:
        """Return {neuron_id: row_index_in_codes_matrix} lookup dict."""
        if self._neuron_ids is None:
            return {}
        return {nid: i for i, nid in enumerate(self._neuron_ids)}

    # ────────────────────────────────────────────────────────────────
    # P5: Latent Codec Retrain Hook
    # ────────────────────────────────────────────────────────────────

    def should_retrain(self, tick: int, neuron_types: Optional[Dict[str, str]] = None,
                       retrain_interval: int = 1000,
                       min_variance: float = 0.05,
                       max_variance: float = 0.60) -> Tuple[bool, str]:
        """Check whether codec retrain is warranted.

        Returns (should_retrain, reason). Non-blocking — no heavy work here.
        """
        if not self._fitted:
            return False, "not_fitted"
        with _stats_lock:
            last = _codec_stats.get("last_retrain_tick", 0)
            avg_var = _codec_stats.get("total_variance_explained", 0)
            n_types = _codec_stats.get("n_types_trained", 0)
        # Condition A: interval elapsed
        if tick - last < retrain_interval:
            return False, f"interval_not_elapsed ({tick-last}/{retrain_interval} ticks)"
        # Condition B: variance too low (codec degrading)
        if avg_var < min_variance:
            return True, f"low_variance ({avg_var:.4f} < {min_variance})"
        # Condition C: variance too high (codec undertrained / new types)
        if avg_var > max_variance and n_types > 1:
            return True, f"high_variance ({avg_var:.4f} > {max_variance})"
        # Condition D: new types appeared
        # 2026-08-25 fix: NEURON_TYPE values may be role_scores dicts (P3);
        # normalize to primary-role strings before hashing.
        if neuron_types:
            known = {_primary_role_str(v) for v in self._type_map.values()} if self._type_map else set()
            current = {_primary_role_str(v) for v in neuron_types.values()}
            unseen = current - known
            if unseen:
                return True, f"new_types: {unseen}"
        return False, "ok"

    def mark_retrained(self, tick: int) -> None:
        """Record the tick a retrain was triggered (2026-09-01 fix).

        should_retrain() reads last_retrain_tick, which nothing ever wrote —
        the interval gate always compared against 0.
        """
        with _stats_lock:
            _codec_stats["last_retrain_tick"] = int(tick)

    def retrain_async(self, embeddings_matrix, neuron_ids, neuron_types,
                      pkl_path: Optional[Path] = None) -> Optional[threading.Thread]:
        """Trigger async retrain in background thread. Returns thread or None."""
        p = pkl_path or _CODEC_PKL_PATH
        def _do_retrain():
            try:
                print(f"[pulse_codec] retrain triggered ({len(neuron_ids)} neurons)", flush=True)
                new_codec = PulseCodec()
                new_codec.fit(embeddings_matrix, neuron_ids, neuron_types)
                new_codec.save(p)
                # Atomically replace singleton
                with _singleton_lock:
                    global _codec_singleton
                    _codec_singleton = new_codec
                print(f"[pulse_codec] retrain complete: {new_codec.get_stats()['compression_ratio']:.1f}×", flush=True)
            except Exception as e:
                print(f"[pulse_codec] retrain error: {e}", flush=True)
        t = threading.Thread(target=_do_retrain, daemon=True)
        t.start()
        return t


def _primary_role_str(value):
    """P3 compat (fix 2026-08-25): NEURON_TYPE values changed from str to
    role_scores dicts. Return a hashable primary-role string for either
    legacy str values or dict values; anything else -> 'unknown'."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        try:
            return max(value, key=value.get)
        except Exception:
            return "unknown"
    return "unknown"


# ─── Module-level singleton ────────────────────────────────────────
_codec_singleton: Optional[PulseCodec] = None
_singleton_lock = threading.Lock()


def get_codec() -> PulseCodec:
    global _codec_singleton
    if _codec_singleton is None:
        with _singleton_lock:
            if _codec_singleton is None:
                _codec_singleton = PulseCodec()
                # Auto-load from disk if pickle exists
                _p = _CODEC_PKL_PATH
                if _p.exists():
                    try:
                        _codec_singleton = PulseCodec.load(_p)
                    except Exception as _e:
                        print(f"[pulse_codec] load skipped: {_e}")
    return _codec_singleton


def set_global_instance(codec: PulseCodec):
    global _codec_singleton
    _codec_singleton = codec


def get_stats() -> dict:
    codec = _codec_singleton
    if codec is None:
        return {"enabled": False}
    return codec.get_stats()

"""
EdgeStore v2 — Two-Tier Edge Storage (P1 风暴计划)
====================================================
Active tier  : in-memory dict, recently-accessed edges (~20%)
Cold tier    : serialized to .lae/edges_cold.pkl, infrequent edges (~80%)
Access path  : Active -> miss -> lazy load from Cold -> promote to Active
Downgrade    : edges idle >7 days are swept from Active -> Cold
Persistence  : cold tier written to pickle; active tier persists via save_checkpoint->SQLite
"""

import heapq
import json
import os
import pickle
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Default workspace root (overridden by set_workspace() before first use)
_WORKSPACE = Path(__file__).resolve().parent
_COLD_PKL_PATH = _WORKSPACE / ".lae" / "edges_cold.pkl"

# Active-tier config
ACTIVE_MAX_ENTRIES = 10000          # max source neurons kept in active tier (was 40000; 2026-08-12 memory: active tier is the real memory anchor — EDGE_ACTIVE mirror is capped at 20000, so 40000 kept dead edge lists alive in _active forever)
COLD_IDLE_DAYS = 7.0               # edges idle this long are candidates for cold downgrade
COLD_IDLE_SECS = COLD_IDLE_DAYS * 86400.0

# Cold-tier memory cache: keep recently-accessed cold sources in memory to avoid
# repeated pickle deserialization. Bounded LRU.
COLD_MEM_CACHE_MAX = 2000          # max cold source entries in memory cache

# ─── Stats (separate lock — never held simultaneously with self._lock) ───
_stats_lock = threading.Lock()
_edge_store_v2_stats = {
    "active_count": 0,
    "cold_count": 0,
    "total_count": 0,
    "cold_hits": 0,
    "cold_misses": 0,
    "cold_promotions": 0,           # cold -> active (lazy load)
    "active_downgrades": 0,         # active -> cold (sweep / cap eviction)
    "last_sweep_tick": 0,
    "cold_sqlite_recovered": 0,     # cold sources recovered from SQLite on fallback
}
# Reference to the server-level EDGE_ACTIVE dict for authoritative active count.
# Set by tl_ego_server.py during init so get_stats() reflects the runtime cache.
_EDGE_ACTIVE_REF = None  # Optional[Dict[str, List]]


def set_workspace(workspace_path):
    """Override default workspace (called by tl_ego_server.py during init)."""
    global _WORKSPACE, _COLD_PKL_PATH
    _WORKSPACE = Path(workspace_path)
    _COLD_PKL_PATH = _WORKSPACE / ".lae" / "edges_cold.pkl"


def get_cold_path() -> Path:
    return _COLD_PKL_PATH


class EdgeStoreV2:
    """
    Two-tier edge storage: Active (in-memory) + Cold (pickle on disk).

    Thread-safe: all public methods acquire self._lock.
    Stats counters use _stats_lock (separate) to avoid deadlock when
    get_stats() is called from inside a self._lock section.
    """

    def __init__(self, workspace: Optional[Path] = None):
        self._lock = threading.RLock()

        ws = workspace or _WORKSPACE
        self._cold_path: Path = ws / ".lae" / "edges_cold.pkl"
        self._cold_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Active tier ────────────────────────────────────────────────
        # OrderedDict used as LRU: most recently accessed at end.
        # Value: list of (dst, weight) sorted by weight desc.
        self._active: "OrderedDict[str, List[Tuple[str, float]]]" = OrderedDict()

        # ── Cold tier ──────────────────────────────────────────────────
        # Loaded from pickle on first access; None until then.
        self._cold_loaded: bool = False
        self._cold: Dict[str, List[Tuple[str, float]]] = {}
        # In-memory LRU cache for recently-accessed cold sources (avoids
        # repeated pickle deserialization). Bounded by COLD_MEM_CACHE_MAX.
        self._cold_mem_cache: "OrderedDict[str, List[Tuple[str, float]]]" = OrderedDict()
        # Per-source last-active timestamp (epoch seconds) for sweep scheduling
        self._src_last_active: Dict[str, float] = {}
        # A cold pickle may belong to an older neuron-ID family. Keep it out of
        # the current graph without overwriting the original artifact.
        self._compatible_id_prefixes: Optional[Tuple[str, ...]] = None
        self._incompatible_cold_sources: int = 0
        self._incompatible_cold_edges: int = 0
        self._cold_quarantined: bool = False
        self._legacy_cold_path: Optional[Path] = None
        # 2026-08-07: reuse a single read-only SQLite connection instead of
        # reconnecting per lookup. Connecting opens the 34.6GB graph file each
        # time (0.2-2s); a tick touching thousands of cold-tier sources would
        # otherwise spend 15-20min in get_edges(). All callers hold self._lock.
        self._sqlite_conn = None
        self._sqlite_conn_path: Optional[Path] = None

    # ────────────────────────────────────────────────────────────────
    # Tier management helpers (must hold self._lock)
    # ────────────────────────────────────────────────────────────────

    def _load_cold(self, sqlite_fallback: bool = True) -> Dict[str, List[Tuple[str, float]]]:
        """Deserialize cold tier from pickle.

        Returns cold dict (shared ref).

        SQLite fallback: if pickle is empty / missing / all-empty, try to
        recover cold-edge candidates from the Neural Grid SQLite database
        (grid_edges.sqlite).  This covers the case where the pickle was
        corrupted or overwritten but the authoritative edge data still lives
        in SQLite.
        """
        if self._cold_loaded:
            return self._cold
        if self._cold_path.exists() and self._cold_path.stat().st_size > 0:
            try:
                with open(self._cold_path, "rb") as f:
                    data = pickle.load(f)
                if isinstance(data, dict):
                    self._cold = data
                else:
                    self._cold = {}
            except Exception:
                self._cold = {}
        self._cold_loaded = True

        self._filter_incompatible_cold_locked()

        # ── SQLite fallback: DISABLED for memory (2026-08-12) ──
        # Loading every cold source from the 34.6GB graph into self._cold pinned
        # ~27M edges (~4GB RAM): `SELECT src FROM edges GROUP BY src` + per-src
        # fetch on a 99069-source cold set. get_edges() already queries SQLite
        # per-src (step 2, connection reused) with SQLite taking precedence over
        # the cold dict, so the fallback dict is pure redundancy. Cold access
        # falls through to the SQLite query path; nothing is lost.
        if sqlite_fallback and not any(self._cold.values()):
            print(
                "[edge_store_v2] sqlite fallback disabled (memory): cold tier "
                "stays unloaded; SQLite per-src queries cover cold access",
                flush=True,
            )
        return self._cold

    def set_compatible_id_prefixes(self, prefixes) -> None:
        """Restrict cold edges to the neuron-ID families used by this server."""
        normalized = tuple(sorted({str(prefix) for prefix in prefixes if prefix}))
        with self._lock:
            self._compatible_id_prefixes = normalized or None
            print(f"[edge_store_v2] set_compatible_id_prefixes: {normalized or 'None'}", flush=True)
            if self._cold_loaded:
                self._filter_incompatible_cold_locked()

    def _filter_incompatible_cold_locked(self) -> None:
        """Quarantine incompatible cold edges in memory without rewriting the pickle."""
        if not self._compatible_id_prefixes or not self._cold:
            return
        # DEBUG: 打印兼容性信息
        prefixes = self._compatible_id_prefixes
        sample_sources = list(self._cold.keys())[:5]
        print(f"[edge_store_v2] compat check: prefixes={prefixes}, sample_sources={sample_sources}", flush=True)
        prefixes = self._compatible_id_prefixes
        compatible = {}
        removed_sources = 0
        removed_edges = 0
        for src, edges in self._cold.items():
            if not src.startswith(prefixes):
                removed_sources += 1
                removed_edges += len(edges)
                continue
            valid_edges = [(dst, weight) for dst, weight in edges if dst.startswith(prefixes)]
            removed_edges += len(edges) - len(valid_edges)
            if valid_edges:
                compatible[src] = valid_edges
            else:
                removed_sources += 1
        if removed_sources:
            # Source-level incompatibility: some neuron-ID prefixes don't match
            # the current server.  Quarantine the cold tier and switch to a
            # filtered sub-file (edges_cold.current.pkl).
            # 2026-08-30 fix(D): 兼容边合并进 current, 不再被整体重载丢弃 —
            # 原逻辑: 抽出兼容边后无条件 reload current.pkl 覆盖 self._cold,
            # 导致 rescue 集合永远落不了地 (legacy 73827 条躺在文件里进不了内存)。
            _current = {}
            if self._cold_path.exists() and self._cold_path.stat().st_size > 0:
                try:
                    with open(self._cold_path, "rb") as handle:
                        _loaded = pickle.load(handle)
                    if isinstance(_loaded, dict):
                        _current = _loaded
                except Exception:
                    _current = {}
            _merged = dict(_current)
            for _src, _edges in compatible.items():
                _bucket = _merged.setdefault(_src, [])
                _have = {d for d, _w in _bucket}
                for _dst, _w in _edges:
                    if _dst not in _have:
                        _bucket.append((_dst, _w))
                        _have.add(_dst)
            # 对合并结果执行同样的兼容过滤 — 否则 _current 里历史遗留的死边
            # (dst 指向已不存在神经元) 会在每次 merge 中永久存活。
            _clean = {}
            for _src, _edges in _merged.items():
                if not _src.startswith(prefixes):
                    continue
                _valid = [(_dst, _w) for _dst, _w in _edges if _dst.startswith(prefixes)]
                if _valid:
                    _clean[_src] = _valid
            _merged = _clean
            self._cold = _merged
            self._cold_mem_cache.clear()
            self._incompatible_cold_sources += removed_sources
            self._incompatible_cold_edges += removed_edges
            self._cold_quarantined = True
            self._legacy_cold_path = self._cold_path
            self._cold_path = self._cold_path.with_name("edges_cold.current.pkl")
            try:
                self._save_cold()
            except Exception:
                pass
            print(
                f"[edge_store_v2] quarantined incompatible cold graph: "
                f"sources={removed_sources} edges={removed_edges}; "
                f"new_cold_path={self._cold_path.name}",
                flush=True,
            )
        elif removed_edges:
            # 2026-09-02 fix(G): All source IDs are compatible (removed_sources==0)
            # but there are dead edges (dst points to vanished neurons).
            # Dead-edge pruning only — safe to reset quarantine, but keep the
            # filtered result in memory so cold hits can resume.
            _clean = {}
            for _src, _edges in self._cold.items():
                if not _src.startswith(prefixes):
                    continue
                _valid = [(_dst, _w) for _dst, _w in _edges if _dst.startswith(prefixes)]
                if _valid:
                    _clean[_src] = _valid
            self._cold = _clean
            self._cold_mem_cache.clear()
            self._incompatible_cold_edges += removed_edges
            self._cold_quarantined = False
            if self._legacy_cold_path and self._cold_path != self._legacy_cold_path:
                self._cold_path = self._legacy_cold_path
                self._legacy_cold_path = None
            try:
                self._save_cold()
            except Exception:
                pass
            print(
                f"[edge_store_v2] pruned {removed_edges} dead edges from cold tier; "
                "quarantine lifted (all sources compatible)",
                flush=True,
            )
        else:
            # 2026-09-02 fix(F): _cold_quarantined was a one-way door —
            # once set to True it was never cleared, so cold tier stayed
            # permanently quarantined even after all edges became compatible.
            #
            # Two sub-cases:
            #  A) removed_sources==0  → all source IDs are compatible;
            #     removed_edges>0     → dead-edge pruning only (dst points to
            #     vanished neurons).  Safe to reset quarantine — the cold tier
            #     is already filtered, just needs the flag cleared.
            #  B) removed_sources>0   → genuine source-level incompatibility;
            #     keep quarantine active.
            if removed_sources == 0:
                self._cold_quarantined = False
                if self._legacy_cold_path and self._cold_path != self._legacy_cold_path:
                    self._cold_path = self._legacy_cold_path
                    self._legacy_cold_path = None
                print(
                    "[edge_store_v2] cold sources all compatible — quarantine lifted",
                    flush=True,
                )
            else:
                print(
                    "[edge_store_v2] quarantine retained: incompatible sources remain",
                    flush=True,
                )

    def _save_cold(self):
        """Serialize cold tier to pickle (atomic write)."""
        # Purge empty entries before saving — they are useless and waste pickle space
        self._cold = {k: v for k, v in self._cold.items() if v}
        tmp = self._cold_path.with_suffix(".pkl.tmp")
        try:
            with open(tmp, "wb") as f:
                pickle.dump(self._cold, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(self._cold_path)
        except Exception:
            pass  # non-critical; cold tier can be rebuilt on restart

    def _touch_src(self, src: str):
        """Record last-access time for a source neuron."""
        self._src_last_active[src] = time.time()

    def _maybe_promote_cold_src(self, src: str) -> Optional[List[Tuple[str, float]]]:
        """If src is in cold tier, load + promote to active. Returns edges or None."""
        cold = self._load_cold()
        edges = cold.pop(src, None)
        if edges is not None:
            self._cold_mem_cache.pop(src, None)
            self._active[src] = edges
            self._touch_src(src)
            with _stats_lock:
                _edge_store_v2_stats["cold_hits"] += 1
                _edge_store_v2_stats["cold_promotions"] += 1
                _edge_store_v2_stats["cold_count"] = len(cold)
            return edges
        with _stats_lock:
            _edge_store_v2_stats["cold_misses"] += 1
        return None

    def _maybe_demote_src(self, src: str) -> bool:
        """Demote src from active if idle too long. Returns True if demoted.

        2026-08-12 memory: evicted edges are DROPPED (not written to self._cold)
        — same rationale as _enforce_active_cap_locked: save_checkpoint persists
        active edges to SQLite; the cold dict is memory-only redundancy.
        """
        last = self._src_last_active.get(src)
        if last is None:
            return False
        idle = time.time() - last
        if idle < COLD_IDLE_SECS:
            return False
        edges = self._active.pop(src, None)
        if edges is None:
            return False
        if not edges:
            return False  # skip empty edge lists — no value to cold-store
        with _stats_lock:
            _edge_store_v2_stats["active_downgrades"] += 1
        return True

    def _enforce_active_cap_locked(self):
        """Evict excess active sources (LRU). Must hold self._lock.

        2026-08-12 memory: evicted edges are DROPPED, not written to self._cold.
        save_checkpoint() in tl_ego_server persists ALL active edges to SQLite
        before/at eviction time, and the cold dict is memory-only (never
        serialized back to disk), so writing it here only grew RAM (~27M cold
        edges, ~4GB). SQLite is the authoritative store.
        """
        while len(self._active) > ACTIVE_MAX_ENTRIES:
            src, edges = self._active.popitem(last=False)
            if not edges:
                continue  # skip empty edge lists
            with _stats_lock:
                _edge_store_v2_stats["active_downgrades"] += 1
                _edge_store_v2_stats["evicted_dropped"] = \
                    _edge_store_v2_stats.get("evicted_dropped", 0) + 1

    # ────────────────────────────────────────────────────────────────
    # Public API — read
    # ────────────────────────────────────────────────────────────────

    def _resolve_grid_db(self) -> Optional[Path]:
        """Locate the authoritative Neural Grid edge database.

        The workspace-local skills/ copy can be a tiny stub; the real graph
        lives in the global ~/.openclaw/skills/ tree. Prefer a non-stub file.

        P6-coldfix 2026-08-11: under the nssm service (LocalSystem),
        Path.home() resolves to C:\\Windows\\System32\\config\\systemprofile
        where the 35GB authoritative DB does not exist, so the service ran
        in degraded mode (723K cold edges only, no SQLite). Candidates now
        include explicit USERPROFILE / known-Administrator paths as a
        hard fallback so the service finds the real graph regardless of the
        service account's home directory.
        """
        candidates = [
            Path.home() / ".openclaw" / "skills" / "neural_grid" / "data" / "grid_edges.sqlite",
            self._cold_path.parent.parent / "skills" / "neural_grid" / "data" / "grid_edges.sqlite",
        ]
        # Hard fallbacks for service accounts (LocalSystem etc.) whose home
        # is not the real user profile.
        for _env in ("USERPROFILE", "HOMEDRIVE", "PROFILE"):
            _v = os.environ.get(_env)
            if _v:
                candidates.append(Path(_v) / ".openclaw" / "skills" / "neural_grid" / "data" / "grid_edges.sqlite")
        # Common concrete install (this machine's operator account).
        candidates.append(Path("C:/Users/Administrator/.openclaw/skills/neural_grid/data/grid_edges.sqlite"))
        for p in candidates:
            try:
                if p.exists() and p.stat().st_size > 1024 * 1024:
                    return p
            except OSError:
                continue
        for p in candidates:
            try:
                if p.exists():
                    return p
            except OSError:
                continue
        return None

    def _get_sqlite_conn(self):
        """Return a cached read-only connection to the authoritative grid DB."""
        db_path = self._resolve_grid_db()
        if db_path is None:
            return None
        if self._sqlite_conn is None or self._sqlite_conn_path != db_path:
            try:
                import sqlite3
                uri = "file:" + str(db_path).replace("\\", "/") + "?mode=ro"
                self._sqlite_conn = sqlite3.connect(uri, uri=True, timeout=2)
                self._sqlite_conn_path = db_path
            except Exception:
                self._sqlite_conn = None
        return self._sqlite_conn

    def _sqlite_edges(self, srcs: List[str]) -> Dict[str, List[Tuple[str, float]]]:
        """Read current-graph edges for sources missing from active memory.

        The cold pickle can be from an older neuron-ID family. SQLite is the
        authoritative current graph and must be consulted before that legacy
        artifact, otherwise valid current edges appear to be missing.
        """
        if not srcs:
            return {}
        try:
            conn = self._get_sqlite_conn()
            if conn is None:
                return {}
            out: Dict[str, List[Tuple[str, float]]] = {}
            placeholders = ",".join("?" for _ in srcs)
            rows = conn.execute(
                f"SELECT src, dst, weight FROM edges WHERE src IN ({placeholders}) AND weight > 0",
                srcs,
            ).fetchall()
            for src, dst, weight in rows:
                out.setdefault(src, []).append((str(dst), float(weight)))
            return out
        except Exception:
            return {}

    def get_edges_topk(self, src: str, k: int) -> List[Tuple[str, float]]:
        """Strongest k outgoing edges of src, in weight order.

        Callers that only compete over the top k neighbours (KWTA propagation
        caps at EDGE_CAP=200) used to fetch the FULL adjacency and nlargest()
        it afterwards: a heavy source carries 38k edges (measured), and the
        fetch also wrote all 38k into the shared EDGE_ACTIVE mirror, whose
        12M-edge cap then held ~2.7k sources instead of the ~300k the graph
        has -> every tick re-read ~1k sources from the 29GB SQLite and evicted
        what the next tick needed. Reading k rows is 7.6x faster and 85x
        fewer rows (measured, 200 heaviest sources).
        """
        if k <= 0:
            return []
        with self._lock:
            # In-memory tiers hold the freshest STDP weights; prefer them.
            if src in self._active:
                self._active.move_to_end(src)
                self._touch_src(src)
                edges = self._active[src]
                return heapq.nlargest(k, edges, key=lambda e: e[1]) if len(edges) > k else list(edges)
            if src in self._cold_mem_cache:
                edges = self._cold_mem_cache.pop(src)
                self._cold_mem_cache[src] = edges
                return heapq.nlargest(k, edges, key=lambda e: e[1]) if len(edges) > k else list(edges)
            # Cold: let SQLite order and cut. Deliberately does NOT populate
            # self._active — caching edges the caller discards is what starved
            # the mirror.
            try:
                conn = self._get_sqlite_conn()
                if conn is None:
                    return []
                rows = conn.execute(
                    "SELECT dst, weight FROM edges WHERE src = ? AND weight > 0 "
                    "ORDER BY weight DESC LIMIT ?",
                    (src, k),
                ).fetchall()
                return [(str(d), float(w)) for d, w in rows]
            except Exception:
                return []

    def get_edges(self, src: str) -> List[Tuple[str, float]]:
        """
        Get outgoing edges for src neuron.
        Priority: Active -> Cold mem-cache -> Cold on disk -> empty list.
        Returns list of (dst, weight).
        """
        with self._lock:
            # 1. Active tier hit
            if src in self._active:
                self._active.move_to_end(src)
                self._touch_src(src)
                return self._active[src]

            # 2. Current SQLite graph takes precedence over legacy cold pickle.
            current = self._sqlite_edges([src]).get(src)
            if current:
                self._active[src] = current
                self._touch_src(src)
                self._enforce_active_cap_locked()
                return current

            # 3. Cold memory cache hit (fast, no pickle I/O)
            if src in self._cold_mem_cache:
                edges = self._cold_mem_cache.pop(src)
                self._cold_mem_cache[src] = edges  # re-insert at end
                # Promote to active
                self._active[src] = edges
                self._touch_src(src)
                self._enforce_active_cap_locked()
                with _stats_lock:
                    _edge_store_v2_stats["cold_hits"] += 1
                    _edge_store_v2_stats["cold_promotions"] += 1
                return edges

            # 4. Cold tier on disk (slow path — pickle deserialise)
            edges = self._maybe_promote_cold_src(src)
            if edges is not None:
                self._enforce_active_cap_locked()
                return edges

            # 5. Complete miss
            return []

    def get_edges_bulk(self, srcs: List[str]) -> Dict[str, List[Tuple[str, float]]]:
        """Batch-get edges for multiple src neurons."""
        result: Dict[str, List[Tuple[str, float]]] = {}
        misses: List[str] = []
        with self._lock:
            for src in srcs:
                if src in self._active:
                    self._active.move_to_end(src)
                    self._touch_src(src)
                    result[src] = self._active[src]
                elif src in self._cold_mem_cache:
                    edges = self._cold_mem_cache.pop(src)
                    self._cold_mem_cache[src] = edges
                    self._active[src] = edges
                    self._touch_src(src)
                    result[src] = edges
                    with _stats_lock:
                        _edge_store_v2_stats["cold_hits"] += 1
                        _edge_store_v2_stats["cold_promotions"] += 1
                else:
                    misses.append(src)

            if misses:
                # Resolve current graph first; legacy cold data is only a fallback.
                current = self._sqlite_edges(misses)
                remaining = [src for src in misses if src not in current]
                for src, edges in current.items():
                    self._active[src] = edges
                    self._touch_src(src)
                    result[src] = edges
                cold = self._load_cold()
                for src in remaining:
                    edges = cold.pop(src, None)
                    if edges is not None:
                        self._active[src] = edges
                        self._touch_src(src)
                        result[src] = edges
                        with _stats_lock:
                            _edge_store_v2_stats["cold_hits"] += 1
                            _edge_store_v2_stats["cold_promotions"] += 1
                            _edge_store_v2_stats["cold_count"] = len(cold)
                    else:
                        result[src] = []
                        with _stats_lock:
                            _edge_store_v2_stats["cold_misses"] += 1
                self._enforce_active_cap_locked()
        return result

    def has_src(self, src: str) -> bool:
        """Fast check: does src have any edges in active or cold tier?"""
        if src in self._active:
            return True
        if src in self._cold_mem_cache:
            return True
        with self._lock:
            if not self._cold_loaded and self._cold_path.exists():
                self._load_cold()
            return src in self._cold

    # ────────────────────────────────────────────────────────────────
    # Public API — write (mutate active tier)
    # ────────────────────────────────────────────────────────────────

    def set_edges(self, src: str, edges: List[Tuple[str, float]]):
        """Replace the entire edge list for src (in active tier)."""
        with self._lock:
            # Remove from cold if present
            if not self._cold_loaded:
                self._load_cold()
            self._cold.pop(src, None)
            self._cold_mem_cache.pop(src, None)
            # Write to active
            self._active[src] = edges
            self._touch_src(src)
            self._enforce_active_cap_locked()

    def upsert_edge(self, src: str, dst: str, weight: float):
        """Add or update a single (src->dst) edge in the active tier."""
        with self._lock:
            self._touch_src(src)
            if src in self._active:
                edges = self._active[src]
                replaced = False
                new_edges: List[Tuple[str, float]] = []
                for d, w in edges:
                    if d == dst:
                        new_edges.append((d, weight))
                        replaced = True
                    else:
                        new_edges.append((d, w))
                if not replaced:
                    new_edges.append((dst, weight))
                self._active[src] = new_edges
                self._active.move_to_end(src)
            else:
                self._active[src] = [(dst, weight)]
            self._enforce_active_cap_locked()

    def touch_src(self, src: str):
        """Mark src as recently accessed (prevents idle sweep demotion)."""
        with self._lock:
            self._touch_src(src)
            if src in self._active:
                self._active.move_to_end(src)

    # ────────────────────────────────────────────────────────────────
    # Tier eviction / promotion
    # ────────────────────────────────────────────────────────────────

    def sweep_idle_to_cold(self) -> int:
        """
        Scan active tier; demote sources idle > COLD_IDLE_SECS to cold tier.
        Returns number of sources demoted.
        Must be called with self._lock held (e.g. from save_checkpoint).
        """
        now = time.time()
        demoted = 0
        with self._lock:
            to_demote = [
                src for src, last in self._src_last_active.items()
                if src in self._active and (now - last) > COLD_IDLE_SECS
            ]
            cold = self._load_cold()
            for src in to_demote:
                edges = self._active.pop(src, None)
                if edges is not None:
                    cold[src] = edges
                    demoted += 1
            if demoted:
                self._save_cold()
            with _stats_lock:
                _edge_store_v2_stats["active_downgrades"] += demoted
                _edge_store_v2_stats["cold_count"] = len(cold)
                _edge_store_v2_stats["last_sweep_tick"] = int(now)
        return demoted

    def downgrade_lru(self, n: int = 100) -> int:
        """
        Force-demote the n least-recently-used active sources to cold tier,
        regardless of idle time. Used during cold-start warmup.
        Returns number of sources demoted.
        """
        demoted = 0
        with self._lock:
            print(f"[downgrade_lru] EDGE_ACTIVE_REF len={len(_EDGE_ACTIVE_REF) if _EDGE_ACTIVE_REF else 0} n={n}", flush=True)
            active_srcs = _EDGE_ACTIVE_REF if _EDGE_ACTIVE_REF else self._active
            # popitem(last=False) = LRU (oldest at front)
            to_demote = []
            for src, edges in active_srcs.items():
                if len(to_demote) >= n:
                    break
                to_demote.append((src, edges))
            print(f"[downgrade_lru] collected {len(to_demote)} candidates: {[s for s,_ in to_demote[:3]]}...", flush=True)
            cold = self._load_cold()
            for src, edges in to_demote:
                _EDGE_ACTIVE_REF.pop(src, None) if _EDGE_ACTIVE_REF else self._active.pop(src, None)
                if edges:
                    cold[src] = edges
                    demoted += 1
            if demoted:
                self._save_cold()
            with _stats_lock:
                _edge_store_v2_stats["active_downgrades"] += demoted
                _edge_store_v2_stats["cold_count"] = len(cold)
        return demoted

    def persist_cold(self):
        """Force-save cold tier to disk."""
        with self._lock:
            self._save_cold()

    # ────────────────────────────────────────────────────────────────
    # Stats
    # ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return edge-store statistics for /status.

        Reads active source count from EDGE_ACTIVE (set via set_edge_active_ref)
        or falls back to self._active. Cold count reflects the in-memory cold
        tier (0 if cold not yet loaded on first access). Stats counters use a
        separate lock so this method is safe to call from inside self._lock.
        """
        active_items = list((_EDGE_ACTIVE_REF or self._active).items())
        active_source_count = sum(1 for _, edges in active_items if edges)
        active_edge_count = sum(len(edges) for _, edges in active_items)
        # Eager-load cold tier so total_count is accurate even before any
        # lazy edge access has occurred (fixes "edges=0 after restart" illusion).
        cold = self._load_cold()
        cold_source_count = sum(1 for edges in cold.values() if edges)
        cold_edge_count = sum(len(edges) for edges in cold.values())
        with _stats_lock:
            return {
                # Legacy fields retain their historical source-count semantics.
                "active_count": active_source_count,
                "cold_count": cold_source_count,
                "total_count": active_source_count + cold_source_count,
                "active_source_count": active_source_count,
                "cold_source_count": cold_source_count,
                "total_source_count": active_source_count + cold_source_count,
                "active_edge_count": active_edge_count,
                "cold_edge_count": cold_edge_count,
                "total_edge_count": active_edge_count + cold_edge_count,
                "compatible_id_prefixes": list(self._compatible_id_prefixes or ()),
                "incompatible_cold_sources": self._incompatible_cold_sources,
                "incompatible_cold_edges": self._incompatible_cold_edges,
                "cold_quarantined": self._cold_quarantined,
                "cold_path": str(self._cold_path),
                "legacy_cold_path": str(self._legacy_cold_path) if self._legacy_cold_path else None,
                "cold_hits": _edge_store_v2_stats["cold_hits"],
                "cold_misses": _edge_store_v2_stats["cold_misses"],
                "cold_promotions": _edge_store_v2_stats["cold_promotions"],
                "active_downgrades": _edge_store_v2_stats["active_downgrades"],
                "last_sweep_tick": _edge_store_v2_stats["last_sweep_tick"],
                "cold_mem_cache_size": len(self._cold_mem_cache),
                "active_source_cap": ACTIVE_MAX_ENTRIES,
                "cold_idle_days": COLD_IDLE_DAYS,
            }

    # ────────────────────────────────────────────────────────────────
    # Bulk operations (for save_checkpoint compatibility)
    # ────────────────────────────────────────────────────────────────

    def all_active_items(self):
        """Iterate all (src, [(dst, weight), ...]) in active tier (snapshot)."""
        return list(self._active.items())

    def iter_active_srcs(self):
        """Iterate all src neuron IDs in active tier."""
        return list(self._active.keys())

    # ────────────────────────────────────────────────────────────────
    # Init / reset
    # ────────────────────────────────────────────────────────────────

    def reset_stats(self):
        with _stats_lock:
            _edge_store_v2_stats["cold_hits"] = 0
            _edge_store_v2_stats["cold_misses"] = 0
            _edge_store_v2_stats["cold_promotions"] = 0
            _edge_store_v2_stats["active_downgrades"] = 0


# ─── Module-level singleton ────────────────────────────────────
_edge_store_v2_singleton: Optional[EdgeStoreV2] = None
_singleton_lock = threading.Lock()


def get_edge_store_v2(workspace: Optional[Path] = None) -> EdgeStoreV2:
    """Thread-safe singleton accessor."""
    global _edge_store_v2_singleton
    if _edge_store_v2_singleton is None:
        with _singleton_lock:
            if _edge_store_v2_singleton is None:
                _edge_store_v2_singleton = EdgeStoreV2(workspace or _WORKSPACE)
    return _edge_store_v2_singleton


def set_global_instance(store: EdgeStoreV2):
    """Allow tl_ego_server.py to inject a pre-built instance."""
    global _edge_store_v2_singleton
    _edge_store_v2_singleton = store


def set_edge_active_ref(edge_active_dict):
    """Tell EdgeStoreV2 which dict is the authoritative EDGE_ACTIVE runtime cache."""
    global _EDGE_ACTIVE_REF
    _EDGE_ACTIVE_REF = edge_active_dict

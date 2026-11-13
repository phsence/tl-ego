"""
P2 动态稀疏拓扑 — Edge Sparsifier
=====================================
两阶段：
  1. 经验生长：两神经元在 ≥COACTIVATION_THRESHOLD 个不同 tick 同时激活 → 建边
     (计数按 tick 累加：coactivation_update 内部先把 activation_path 折叠去重，
      所以「同一 tick 内出现几次」只算 1 次)
  2. 反Hebbian删除：边超 30 天未激活 → 删除；另外共激活次数始终未达阈值的边
     也会被剪掉

集成点：
  - full_pipeline Step 5（synaptogenesis 之后）调用 coactivation_update
  - save_checkpoint 末尾调用 prune_stale_edges
  - /status 暴露 edge_sparsifier 统计

2026-10-01: start_grow_worker() / _grow_queue 已于 09-11 重构中删除，生长改为
同步执行；tl_ego_server 里残留的 start_grow_worker() 调用同步删除。
"""

import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_WORKSPACE = Path(__file__).resolve().parent

# ─── 配置 ────────────────────────────────────────────────────────────
EDGE_EXPIRY_DAYS = 30.0            # 边超此天数未激活 → 删除
EDGE_EXPIRY_SECS = EDGE_EXPIRY_DAYS * 86400.0

# 2026-11-03: 阈值改可调, 默认回到 2。l5g-20261029-060372 把 2 改成 1 之后从没回滚:
# 阈值 1 = "共激活一次就建边", 而每 tick 采样 300 个神经元 → 44,850 对全部过阈值,
# 队列上限 20,000 立刻灌满, 实测 grow_dropped_total=68,990 / edges_grown_total=870,
# 也就是说绝大多数经验根本没进图。阈值 2 才代表"重复共激活", 是这条链的本意。
COACTIVATION_THRESHOLD = int(os.environ.get("EGO_COACT_THRESHOLD", "2") or 2)
MAX_NEW_EDGES_PER_TICK = 500       # 每 tick 最多「尝试」建边数（有界生长；其余入队顺延）
_GROW_QUEUE_MAX = 20000            # _pending_grow 队列上限；溢出丢弃并计数（grow_dropped_total）
NEW_EDGE_WEIGHT = 0.1              # 新建边的初始权重
PRUNE_INTERVAL = 50               # 每 N 次 coactivation_update 自动剪一次低频边 (P3-fix 2026-09-06: 给死类型更多累积时间)
_coactivation_call_count = 0      # 模块级累计调用计数器
# 2026-10-18 有界生长队列：pair 刚跨过 COACTIVATION_THRESHOLD 时入队一次，
# 每 tick 最多消费 MAX_NEW_EDGES_PER_TICK 条（见 coactivation_update 注释）。
_pending_grow = deque()
_enqueued_keys = set()      # 2026-11-03: 已在队列里的配对, 避免 >= 阈值时重复入队
_grow_deferred_total = 0    # 队列满时本 tick 没挤进去的配对 (下个 tick 还会再试, 不算丢弃)
_grow_dropped_total = 0
# l5g-20261030-061306: _try_grow_edge 早退计数（forward/reverse/already split，
# 供下一轮 probe 测量 pending 消费时「已存在边」占多少）
_grow_exists_fwd_total = 0
_grow_exists_rev_total = 0
_grow_exists_total = 0
# l5g-20261029-061131: rare-type priority state.
# _type_admitted = per-type ADMITTED-enqueue counter (rarity signal:
# starved types stay low and keep winning room; dropped attempts do
# NOT count, so the poverty trap cannot launder itself via attempts).
_type_admitted = defaultdict(int)
_grow_evicted_total = 0

# ─── 锁 ─────────────────────────────────────────────────────────────
_stats_lock = threading.Lock()
_sup_lock = threading.Lock()        # protects coactivation_map + edge_ages

# ─── 统计（模块级，供 /status 读取）─────────────────────────────────
_sparsifier_stats = {
    "coactivation_threshold": COACTIVATION_THRESHOLD,
    "edge_expiry_days": EDGE_EXPIRY_DAYS,
    "edges_pruned_total": 0,
    "edges_grown_total": 0,
    "last_prune_tick": 0,
    "last_grow_tick": 0,
}

# ─── 共激活计数器 ───────────────────────────────────────────────────
# key = (src, dst) sorted; value = count  (per-tick sliding window)
coactivation_map: Dict[Tuple[int, int], int] = defaultdict(int)

# ─── 边年龄追踪 ─────────────────────────────────────────────────────
# key = (src, dst); value = epoch_seconds of last activation
edge_ages: Dict[Tuple[int, int], float] = {}


# ─── 模块级引用（tl_ego_server.py 注入）─────────────────────────────
_EDGE_ACTIVE_REF = None            # EDGE_ACTIVE dict (src -> [(dst, weight)])
_NEURON_TYPE_REF = None            # NEURON_TYPE dict (nid -> type_str)
_EDGE_STORE_V2_REF = None          # EdgeStoreV2 singleton (cold-tier persistence)


def set_refs(edge_active=None, neuron_type=None, edge_store_v2=None):
    """由 tl_ego_server.py 在启动时注入全局引用。"""
    global _EDGE_ACTIVE_REF, _NEURON_TYPE_REF, _EDGE_STORE_V2_REF
    _EDGE_ACTIVE_REF = edge_active
    _NEURON_TYPE_REF = neuron_type
    _EDGE_STORE_V2_REF = edge_store_v2


# ────────────────────────────────────────────────────────────────────
# P2a — 经验生长
# ────────────────────────────────────────────────────────────────────

def coactivation_update(activation_path: list) -> int:
    """统计当前 tick 共激活对，≥ 阈值则建边。返回本次新建边数。"""
    if not activation_path:
        return 0

    # 收集当前 tick 所有激活神经元
    tick_neurons: List[int] = []
    seen: Set[int] = set()
    for step_set in activation_path:
        for nid in step_set:
            if nid not in seen:
                seen.add(nid)
                tick_neurons.append(nid)
    if len(tick_neurons) > 300:
        import random as _r
        tick_neurons = _r.sample(tick_neurons, 300)
    if len(tick_neurons) < 2:
        return 0

    # TEMP-PROBE 2026-10-18 (tick-cadence diagnosis; remove once growth verified)
    print(f"[spars-dbg] tn={len(tick_neurons)} map={len(coactivation_map)} "
          f"pend={len(_pending_grow)} ap_rounds={len(activation_path)} "
          f"ap_neurons={sum(len(s) for s in activation_path)}", flush=True)

    # 统计本 tick 共激活对
    global _grow_dropped_total, _grow_evicted_total, _grow_deferred_total
    with _sup_lock:
        for i, a in enumerate(tick_neurons):
            for b in tick_neurons[i + 1:]:
                key = (a, b) if a < b else (b, a)
                cnt = coactivation_map.get(key, 0) + 1
                coactivation_map[key] = cnt
                if cnt >= COACTIVATION_THRESHOLD and key not in _enqueued_keys:
                    # 2026-11-03: 原来是 cnt == COACTIVATION_THRESHOLD —— 一次性判定。
                    # 队列满时这对被丢掉后 cnt 已经超过阈值, 永远不再入队 = 永久丢经验
                    # (实测 grow_dropped_total=68,990 vs edges_grown_total=870)。
                    # 改成 >= + _enqueued_keys 去重: 没挤进队列的下个 tick 还有机会。
                    _nt = _NEURON_TYPE_REF or {}
                    # l5g-20261030-061288: NEURON_TYPE values may be dicts (same
                    # root shape fixed at tl_ego_server.py:5582); coerce non-str ->
                    # "unknown" so _type_admitted keys stay hashable.
                    _t_new = tuple(
                        (_nt.get(k, "unknown") if isinstance(_nt.get(k, "unknown"), str) else "unknown")
                        for k in key)
                    if len(_pending_grow) < _GROW_QUEUE_MAX:
                        _pending_grow.append(key)
                        _enqueued_keys.add(key)
                        for _t in _t_new:
                            _type_admitted[_t] += 1
                    else:
                        # l5g-20261029-061131 rare-type priority: 队列满时不再
                        # 无条件丢弃新对。实测 pending 恒 19500/20000, 125s 内
                        # grow_dropped +16421; 稀有类型(decider/doubt/action)越阈
                        # 机会少且几乎总被丢弃 → cached mass 永不转化。
                        # 新对最稀有类型的已入队数 < 队首最常见类型的已入队数
                        # → 驱逐队首让位(计 evicted); 否则维持旧丢弃行为。
                        # ponytail: 只扫队首; 若实测 evicted 命中率低再加深扫描。
                        _head = _pending_grow[0]
                        _t_head = tuple(
                            (_nt.get(k, "unknown") if isinstance(_nt.get(k, "unknown"), str) else "unknown")
                            for k in _head)
                        if (min(_type_admitted.get(_t, 0) for _t in _t_new)
                                < max(_type_admitted.get(_t, 0) for _t in _t_head)):
                            _evicted = _pending_grow.popleft()
                            _enqueued_keys.discard(_evicted)
                            _grow_evicted_total += 1
                            _pending_grow.append(key)
                            _enqueued_keys.add(key)
                            for _t in _t_new:
                                _type_admitted[_t] += 1
                        else:
                            # 2026-11-03: 这里原来是 _grow_dropped_total += 1 =
                            # 永久丢经验。现在没入队的配对不进 _enqueued_keys,
                            # 下个 tick 还会再来, 所以只算"延后"不算"丢弃"。
                            _grow_deferred_total += 1

        # 2026-10-18 有界生长：每 tick 只尝试 MAX_NEW_EDGES_PER_TICK 条。
        # 旧实现每 tick 对「全部过阈值对」同步建边 —— 实测 3.6 万条/分钟，把 tick 拖成
        # 4~15 分钟（tick_profile 的 after_stdp→before_causal 空档即此处）。其余排到
        # _pending_grow 队列、下一 tick 继续消费，tick 成本自此有上界。
        # 2026-09-11 deadlock fix 保留：_stats_lock 不跨 _try_grow_edge 持有。
        created = 0
        for _ in range(min(len(_pending_grow), MAX_NEW_EDGES_PER_TICK)):
            src, dst = _pending_grow.popleft()
            _enqueued_keys.discard((src, dst))
            created += _try_grow_edge(src, dst)

    # P1b-fix: 每 PRUNE_INTERVAL 次自动剪低频边
    global _coactivation_call_count
    _coactivation_call_count += 1
    if _coactivation_call_count % PRUNE_INTERVAL == 0:
        try:
            prune_stale_edges()
        except Exception:
            pass
    return created


def _try_grow_edge(src: int, dst: int) -> int:
    """尝试为 (src, dst) 建边。返回 1 如果成功，否则 0。"""
    global _grow_exists_fwd_total, _grow_exists_rev_total, _grow_exists_total
    if _EDGE_ACTIVE_REF is None:
        return 0
    edges_out = _EDGE_ACTIVE_REF.get(src)
    if edges_out is not None and any(tgt == dst for tgt, _ in edges_out):
        with _stats_lock:  # counter-only critical section; no I/O inside
            _grow_exists_fwd_total += 1
            _grow_exists_total += 1
        return 0  # edge already exists
    edges_rev = _EDGE_ACTIVE_REF.get(dst)
    if edges_rev is not None and any(tgt == src for tgt, _ in edges_rev):
        with _stats_lock:  # counter-only critical section; no I/O inside
            _grow_exists_rev_total += 1
            _grow_exists_total += 1
        return 0  # edge already exists (reverse)
    # 追加到现有边列表，不覆盖（修复：原代码 = [...] 会擦除同一源的其他边）
    _EDGE_ACTIVE_REF.setdefault(src, []).append((dst, NEW_EDGE_WEIGHT))
    with _stats_lock:  # counter-only critical section; no I/O inside
        _sparsifier_stats["edges_grown_total"] += 1
        # 2026-10-01: last_grow_tick was declared and exposed via get_stats() but
        # never written — it read 0 forever, so /status could not tell growth
        # apart from a dead path. Written here, where growth actually happens.
        _sparsifier_stats["last_grow_tick"] = int(time.time())
    touch_edge(src, dst)
    # 同步到 EdgeStoreV2 cold tier（修复：原代码只写 EDGE_ACTIVE，EdgeStoreV2 不知情 → 重启后全部丢失）
    if _EDGE_STORE_V2_REF is not None:
        try:
            _EDGE_STORE_V2_REF.upsert_edge(str(src), str(dst), NEW_EDGE_WEIGHT)
        except Exception:
            pass
    return 1


# ────────────────────────────────────────────────────────────────────
# P2b — 反Hebbian删除（30天未激活 → 删除边）
# ────────────────────────────────────────────────────────────────────

def prune_stale_edges() -> int:
    """删除超 EDGE_EXPIRY_DAYS 未激活的边，以及 coactivation 低频边。返回删除数。"""
    if _EDGE_ACTIVE_REF is None:
        return 0
    now = time.time()
    threshold = now - EDGE_EXPIRY_SECS
    removed = 0
    with _sup_lock:
        to_remove: List[Tuple[int, int]] = []

        # P1-fix: 30 天超期边
        for (src, dst), last_active in list(edge_ages.items()):
            if last_active < threshold:
                to_remove.append((src, dst))

        # P1-fix: coactivation 低频边（建边后从未达到阈值的共激活对）
        for (src, dst), cnt in list(coactivation_map.items()):
            if 0 < cnt < COACTIVATION_THRESHOLD:
                edges = _EDGE_ACTIVE_REF.get(src)
                if edges is not None and any(tgt == dst for tgt, _ in edges):
                    to_remove.append((src, dst))

        for src, dst in to_remove:
            edge_ages.pop((src, dst), None)
            coactivation_map.pop((src, dst), None)
            edges = _EDGE_ACTIVE_REF.get(src)
            if edges:
                _EDGE_ACTIVE_REF[src] = [(t, w) for t, w in edges if t != dst]
                removed += 1
    if removed:
        with _stats_lock:
            _sparsifier_stats["edges_pruned_total"] += removed
            _sparsifier_stats["last_prune_tick"] = int(time.time())
    return removed


def touch_edge(src: int, dst: int):
    """记录边 (src, dst) 的最近激活时间。"""
    key = (src, dst) if src < dst else (dst, src)
    edge_ages[key] = time.time()


# ────────────────────────────────────────────────────────────────────
# Stats
# ────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    active_n = len(_EDGE_ACTIVE_REF) if _EDGE_ACTIVE_REF else 0
    with _stats_lock:
        return {
            "coactivation_threshold": COACTIVATION_THRESHOLD,
            "edge_expiry_days": EDGE_EXPIRY_DAYS,
            "edges_grown_total": _sparsifier_stats["edges_grown_total"],
            "edges_pruned_total": _sparsifier_stats["edges_pruned_total"],
            "last_grow_tick": _sparsifier_stats["last_grow_tick"],
            "last_prune_tick": _sparsifier_stats["last_prune_tick"],
            "active_edges_tracked": active_n,
            "max_new_edges_per_tick": MAX_NEW_EDGES_PER_TICK,
            "pending_grow": len(_pending_grow),
            "grow_deferred_total": _grow_deferred_total,
            "grow_dropped_total": _grow_dropped_total,
            "grow_evicted_total": _grow_evicted_total,
            "grow_exists_fwd_total": _grow_exists_fwd_total,
            "grow_exists_rev_total": _grow_exists_rev_total,
            "grow_exists_total": _grow_exists_total,
        }

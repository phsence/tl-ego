"""Is edge_sparsifier actually growing and pruning, and are its stats honest?

Context: the 2026-09-11 refactor deleted start_grow_worker()/_grow_queue and made
coactivation_update() grow synchronously, but left a start_grow_worker() call in
tl_ego_server that raised AttributeError on every boot. Two things needed proving
before deleting that call:
  1. growth and pruning still run on the direct path (otherwise deleting the
     call would hide a real outage);
  2. the /status counters actually move (they read 0 forever, which is what made
     "is it alive?" unanswerable from status alone).

Key semantics, measured not assumed: coactivation_map is counted PER TICK.
coactivation_update collapses activation_path to a deduped neuron list first, so
a pair co-activating 10 times inside ONE call counts once. THRESHOLD=2 therefore
means "co-activate on 2 separate ticks".

Run: python verify_edge_sparsifier.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import edge_sparsifier as ES  # noqa: E402


def main():
    # 1. the API the server calls must exist; the deleted worker must stay gone
    for fn in ("set_refs", "coactivation_update", "prune_stale_edges",
               "touch_edge", "get_stats"):
        assert hasattr(ES, fn), "server calls edge_sparsifier.%s() but it is missing" % fn
    assert not hasattr(ES, "start_grow_worker"), \
        "start_grow_worker came back — reconcile with tl_ego_server before shipping"

    active = {}
    ES.set_refs(edge_active=active, neuron_type={}, edge_store_v2=None)
    s0 = ES.get_stats()
    print("baseline:           grown=%d pruned=%d last_grow_tick=%s last_prune_tick=%s"
          % (s0["edges_grown_total"], s0["edges_pruned_total"],
             s0["last_grow_tick"], s0["last_prune_tick"]))

    # 2. growth. One tick is not enough: the counter is per-tick, and
    #    COACTIVATION_THRESHOLD=2 demands a second co-activating tick.
    n = 40
    path = [set(range(n)) for _ in range(10)]
    first = ES.coactivation_update(path)
    s1 = ES.get_stats()
    assert first == 0, "one tick should not pass a threshold of %d" % ES.COACTIVATION_THRESHOLD
    assert s1["edges_grown_total"] == s0["edges_grown_total"], s1

    second = ES.coactivation_update(path)
    s2 = ES.get_stats()
    print("tick1 created=%d (below threshold %d)" % (first, ES.COACTIVATION_THRESHOLD))
    print("tick2 created=%d grown_total=%d last_grow_tick=%s"
          % (second, s2["edges_grown_total"], s2["last_grow_tick"]))
    assert second > 0, "growth path is DEAD — deleting the worker call would hide this"
    assert s2["edges_grown_total"] > s1["edges_grown_total"], s2
    # the dead field must now move; it read 0 forever before 2026-10-01
    assert s2["last_grow_tick"] > 0, "last_grow_tick still never written: %r" % s2
    assert len(active) > 0, "edges were not actually inserted"

    # 3. bounded growth (2026-10-18): per-tick attempts capped, backlog carries over
    assert second <= ES.MAX_NEW_EDGES_PER_TICK, \
        "per-tick growth budget violated: %d > %d" % (second, ES.MAX_NEW_EDGES_PER_TICK)
    backlog = len(ES._pending_grow)
    assert backlog > 0, "expected crossings beyond the budget to stay queued"
    third = ES.coactivation_update(path)
    assert 0 < third <= ES.MAX_NEW_EDGES_PER_TICK, third
    assert len(ES._pending_grow) < backlog, "backlog did not shrink"
    guard = 0
    while ES._pending_grow and guard < 200:
        ES.coactivation_update(path)
        guard += 1
    assert not ES._pending_grow, "backlog never drained"
    # 3b. re-growing fully-drained pairs must not duplicate them
    again = ES.coactivation_update(path)
    assert again == 0, "duplicate edges created: %d" % again
    for _src, _edges in active.items():
        _dsts = [d for d, _w in _edges]
        assert len(_dsts) == len(set(_dsts)), ("duplicate target for src", _src)

    # 4. pruning: a pair that co-activated but never reached threshold is a
    #    low-frequency edge and must be removable
    ES.coactivation_map[(999, 1000)] = 1
    active.setdefault(999, []).append((1000, ES.NEW_EDGE_WEIGHT))
    pruned = ES.prune_stale_edges()
    s3 = ES.get_stats()
    print("prune removed=%d pruned_total=%d last_prune_tick=%s"
          % (pruned, s3["edges_pruned_total"], s3["last_prune_tick"]))
    assert pruned >= 1, "low-frequency edge was not pruned"
    assert s3["last_prune_tick"] > 0, "last_prune_tick not written: %r" % s3
    assert (1000, ) != tuple() and not any(t == 1000 for t, _ in active.get(999, [])), \
        "pruned edge still present in EDGE_ACTIVE"

    # 5. the 30-day expiry branch: an old edge_ages entry must be removable
    ES.edge_ages[(5555, 6666)] = time.time() - (ES.EDGE_EXPIRY_SECS + 3600)
    active.setdefault(5555, []).append((6666, ES.NEW_EDGE_WEIGHT))
    assert ES.prune_stale_edges() >= 1, "30-day expired edge was not pruned"
    assert not any(t == 6666 for t, _ in active.get(5555, [])), "expired edge survived"

    # 6. degenerate input, called every tick — must never raise
    for bad in ([], None, [[]], [set()], [{1}], [set(range(1000))]):
        assert ES.coactivation_update(bad) == 0, bad
    assert ES.prune_stale_edges() >= 0
    # touch_edge on unknown ids must not raise
    ES.touch_edge(4242, 4343)

    print("\nOK: growth fires on the 2nd co-activating tick, pruning removes both "
          "low-frequency and 30-day-expired edges, last_grow_tick/last_prune_tick "
          "now move — deleting the stale start_grow_worker() call is correct")


if __name__ == "__main__":
    main()

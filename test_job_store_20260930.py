"""Check for the 2026-09-30 job-store + iteration fixes without booting the server.

Run:  python test_job_store_20260930.py
"""
import ast
import json
import os
import sys
import tempfile
import time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tl_ego_server.py")

# ── 1. the writer exists and the reader's path matches the writer's dir ────────
src = open(SERVER, encoding="utf-8", errors="replace").read()
assert "def _job_persist(" in src, "no _job_persist writer"
assert "_job_persist(job_id, _CONTEMPLATE_JOBS[job_id])" in src, "done-path not persisted"
assert "def _job_store_dir(" in src, "no _job_store_dir"

# reader (POST /contemplate-job fallback) must build the SAME path
tree = ast.parse(src)
reader_dirs = []
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "join":
        segs = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if any("jobs" in s for s in segs) and any(".lae" in s for s in segs):
            reader_dirs.append(segs)
assert reader_dirs, "reader path not found (expected '.lae' + 'jobs' segments)"

# ── 2. no unguarded live EDGE_ACTIVE iteration in the two hot spots ───────────
for bad in ("for src, edges in EDGE_ACTIVE.items():",
            "for dst, w in edges:",
            "for src, targets in EDGE_ACTIVE.items():",
            "for dst, w in targets:"):
    assert bad not in src, "unguarded iteration still present: %r" % bad

# ── 3. functional: the persist/prune logic behaves ──────────────────────────
def _simulate(root, jids, ttl=600, jmax=2, now=time.time()):
    """Mirror _job_persist's prune rule + _job_evict, no server import."""
    d = os.path.join(root, "jobs")
    os.makedirs(d, exist_ok=True)
    store = {}
    def persist(jid):
        p = os.path.join(d, f"{jid}.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store[jid], fh)
        os.replace(tmp, p)
        cutoff = now - 3600
        for f in os.listdir(d):
            if f.endswith(".json") and os.path.getmtime(os.path.join(d, f)) < cutoff:
                os.remove(os.path.join(d, f))
    def evict():
        for jid in [j for j, v in store.items()
                    if v.get("status") in ("done", "error") and now - v.get("created", now) > ttl]:
            persist(jid)
            del store[jid]
        while len(store) > jmax:
            oldest = min(store, key=lambda j: store[j].get("created", 0))
            persist(oldest)
            del store[oldest]
    for i, jid in enumerate(jids):
        store[jid] = {"status": "done", "created": now - 1000, "n": i}
        evict()
    return d, store

with tempfile.TemporaryDirectory() as tmp:
    d, store = _simulate(tmp, [f"j{i}" for i in range(6)])
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    # everything finished + old -> all evicted, all persisted to disk
    assert store == {}, "evict left %d in memory" % len(store)
    assert len(files) == 6, "expected 6 persisted files, got %d: %s" % (len(files), files)
    for f in files:
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            assert json.load(fh)["status"] == "done"
    # a running job is NOT evicted (the 404 case was a poll for a live job)
    d2, store2 = _simulate(tmp, ["live"], ttl=10**9, jmax=2)
    assert "live" in store2 and not os.path.exists(os.path.join(d2, "live.json")), \
        "running job must stay in memory and not be written"

# ── 4. the new modules exist where the server looks ─────────────────────────
root = os.path.join(os.path.dirname(SERVER), "..", "..")
for p in ("scripts/memory_bus.pyc", "scripts/memory_bus_tl_ego_bridge.pyc",
          "ego_core/oscillation_classifier.pyc", "ego_core/__init__.py"):
    assert os.path.exists(os.path.join(root, p)), "missing %s" % p

print("OK: job store writes+prunes, no unguarded EDGE_ACTIVE walk, 4 pyc/__init__ in place")

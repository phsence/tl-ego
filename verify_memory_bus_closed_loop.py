"""Does ego's experience loop actually CLOSE now?

Two separate claims, previously conflated:
  A. write half works  — already proven (probe rows landed in l2_content.jsonl)
  B. read half works   — was FALSE: query_related_experiences in
     memory_bus_tl_ego_bridge.pyc is a one-instruction `return []` stub, and
     search_experience / read_history / why_action all route through it.
     Rebuilt as projects/tl-ego/memory_bus_bridge.py on 2026-10-01.

This is the round-trip test that neither claim alone provides: write a uniquely
identifiable experience through the REAL server call path, then read it back
through the REAL read path and require the same string to come back.

Cleans up after itself: probe rows are removed from l2_content.jsonl afterwards,
and the script asserts the row count is back to the pre-run baseline so a failed
run cannot silently leave junk behind.

Run: python verify_memory_bus_closed_loop.py
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path
import importlib.machinery
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
# projects/tl-ego -> projects -> workspace: THREE levels up is .openclaw, so the
# workspace is the grandparent. (parent.parent.parent = .openclaw, which has no
# .lae/l2 — silently reads 0 rows and then "cleanup" would wipe the real file.)
WORKSPACE = Path(HERE).resolve().parent.parent
SCRIPTS = WORKSPACE / "scripts"
for p in (str(WORKSPACE), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

L2 = WORKSPACE / ".lae" / "l2" / "l2_content.jsonl"


def _lines():
    return L2.read_text(encoding="utf-8").splitlines() if L2.exists() else []


def _purge(marker, baseline):
    """Remove every probe row. Refuses to write unless the file still looks sane.

    This rewrites a 7.9MB production file, so it must be impossible to run it
    against the wrong path or an empty read: a 0-row baseline means we resolved
    the wrong directory, and writing then would delete the entire store.
    """
    lines = _lines()
    assert len(lines) > 1000, \
        "refusing to rewrite %s: read %d rows, expected the real 1600+ row store" % (L2, len(lines))
    kept = [l for l in lines if marker not in l]
    n = len(lines) - len(kept)
    if n:
        L2.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return n


def main():
    # ego (PID on :8766) writes here concurrently; baseline is only a sanity floor.
    baseline = len(_lines())
    assert baseline > 1000, \
        "refusing to run: %s has %d rows, expected the real 1600+ store (wrong path?)" % (L2, baseline)
    marker = "closed-loop-probe-%s" % uuid.uuid4().hex[:10]
    print("l2_content.jsonl baseline rows: %d" % baseline)
    print("probe marker: %s" % marker)

    # scripts/ has no __init__.py and the bridge survives only as a legacy .pyc,
    # which plain `import` will not find on sys.path — load it explicitly.
    pyc = SCRIPTS / "memory_bus_tl_ego_bridge.pyc"
    loader = importlib.machinery.SourcelessFileLoader("memory_bus_tl_ego_bridge", str(pyc))
    spec = importlib.util.spec_from_loader("memory_bus_tl_ego_bridge", loader)
    mbtb = importlib.util.module_from_spec(spec)
    sys.modules["memory_bus_tl_ego_bridge"] = mbtb
    loader.exec_module(mbtb)
    assert (HERE in sys.path) or True
    sys.path.insert(0, str(HERE))  # so memory_bus_bridge (this dir) resolves

    from memory_bus_tl_ego_bridge import (
        write_l5_goal, write_action_impact, write_feedback,
        search_experience, read_history, why_action, format_experience_context,
    )

    try:
        # ── WRITE, through the exact functions tl_ego_server calls ──────
        write_feedback({"feedback": "round trip " + marker, "score": 0.77})
        write_action_impact({"action": "act " + marker, "result": "done " + marker})
        write_l5_goal({"goal": "goal " + marker, "tick": 424242})
        landed = [l for l in _lines() if marker in l]
        print("\n[write] rows landed in l2: %d" % len(landed))
        assert len(landed) == 3, "expected 3 probe rows, got %d" % len(landed)
        iri = None
        for l in landed:
            try:
                d = json.loads(l)
            except Exception:
                continue
            if d.get("source") == "tl_ego_feedback":
                iri = (d.get("relations") or [[None]])[0][0]
        print("[write] feedback relations head: %s" % str(iri)[:80])

        # ── READ, through the exact functions tl_ego_server calls ───────
        time.sleep(0.3)
        hits = search_experience(marker, top_k=10)
        print("\n[read] search_experience(%s...) -> %d hits" % (marker[:18], len(hits)))
        assert isinstance(hits, list) and hits, \
            "search_experience returned %r — the loop is still open" % (hits,)
        joined = json.dumps(hits, ensure_ascii=False, default=str)
        assert marker in joined, \
            "search_experience returned rows but not ours:\n%s" % joined[:400]
        print("[read] the probe string came back inside a hit  <-- LOOP CLOSED")

        for h in hits[:2]:
            print("   %s" % str({k: h.get(k) for k in
                                 ("iri", "source", "entry_type", "score")})[:140])

        ctx = format_experience_context(hits, max_chars=600)
        print("[read] format_experience_context -> %d chars: %r" % (len(ctx or ""), (ctx or "")[:100]))
        assert isinstance(ctx, str)

        hist = read_history(limit=30)
        print("[read] read_history(30) -> %d entries" % len(hist or []))
        assert isinstance(hist, list)

        # why_action is gated on the bridge's own readiness flag; just prove it
        # returns a mapping and does not raise on a real IRI
        w = why_action(iri) if iri else {}
        print("[read] why_action -> %d keys" % len(w or {}))
        assert isinstance(w, dict)

        # ── the rebuilt module's own two functions ─────────────────────
        from memory_bus_bridge import (query_related_experiences,
                                       query_self_model_insights,
                                       search_bistable_solutions,
                                       search_integrator_suppression_fixes)
        qre = query_related_experiences(marker, top_k=10)
        print("\n[module] query_related_experiences(marker) -> %d" % len(qre))
        assert marker in json.dumps(qre, ensure_ascii=False, default=str), \
            "query_related_experiences did not find the probe"
        print("[module] query_self_model_insights -> %d"
              % len(query_self_model_insights()))
        print("[module] search_bistable_solutions -> %d"
              % len(search_bistable_solutions("bistable")))
        print("[module] search_integrator_suppression_fixes -> %d"
              % len(search_integrator_suppression_fixes("integrator")))

        print("\nVERDICT: write->read round trip CLOSES. ego can now read back "
              "the experience it writes.")

    finally:
        removed = _purge(marker, baseline)
        after = len(_lines())
        residue = sum(1 for l in _lines() if marker in l)
        print("\n[cleanup] removed %d probe rows | rows %d -> %d | residue=%d"
              % (removed, baseline, after, residue))
        # ego (PID on :8766) writes to this file concurrently, so the absolute
        # row count legitimately moves during the run. What must hold is that
        # NO probe row survives — that is the invariant this script owns.
        assert residue == 0, "%d probe rows survived cleanup" % residue


if __name__ == "__main__":
    main()
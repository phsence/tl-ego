"""Is the MemoryBus feedback loop actually closed for tl-ego?

Claim to test: "ego's experience feedback loop is dead; the bridge only forwards
to a stub". Earlier batches established the bridge is NOT disabled (no
"[memory-bus-bridge] disabled" line) and DOES write (real
"[WARN] memory_bus unknown tags from tl_ego_l5_goal" lines, and
.lae/l2/l2_content.jsonl has a current mtime). So the write half works.

What is unproven is the READ half and the CLOSED LOOP:
  1. do the 7 functions the server actually calls all work?
  2. does write_feedback -> later search_experience find the same content?
     (a loop that writes but cannot read back is not a closed loop)
  3. what is the exact state of the 4 functions the bridge pyc imports but
     never uses (query_related_experiences, query_self_model_insights,
     search_bistable_solutions, search_integrator_suppression_fixes)?

Read-only against live state: every write goes to a unique probe IRI and is
deleted afterwards by nothing -- instead this only WRITES new probe entries and
reports; it never truncates or rewrites an existing file.

Run: python verify_memory_bus_loop.py
"""
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "..", "scripts"))
sys.path.insert(0, SCRIPTS)

PROBE = "tl-ego-loop-probe-%s" % uuid.uuid4().hex[:10]


def main():
    import memory_bus as MB
    from memory_bus_tl_ego_bridge import (
        write_l5_goal, write_action_impact, write_feedback,
        search_experience, read_history, why_action, format_experience_context,
    )

    print("memory_bus  : %s" % MB.__file__)
    print("probe IRI   : %s" % PROBE)

    # 1. every function the server calls must be callable
    for fn in (write_l5_goal, write_action_impact, write_feedback,
               search_experience, read_history, why_action, format_experience_context):
        assert callable(fn), fn
    print("\n[1] all 7 server-facing functions import and are callable")

    # 2. write half
    goal_iri = write_l5_goal({"goal": "memory bus loop probe " + PROBE,
                              "tick": 999999, "type": "l5_goal"})
    print("[2] write_l5_goal -> %r" % (goal_iri,))
    impact_iri = write_action_impact({"action": "probe action " + PROBE,
                                      "result": "ok"})
    print("    write_action_impact -> %r" % (impact_iri,))
    fb_iri = write_feedback({"feedback": "probe feedback " + PROBE,
                             "score": 0.5, "action": "probe action " + PROBE})
    print("    write_feedback -> %r" % (fb_iri,))

    # 3. READ half -- this is the half nobody has proven
    time.sleep(0.5)
    hits = search_experience(PROBE, top_k=10)
    print("\n[3] search_experience(%s) -> %d hit(s)" % (PROBE[:28], len(hits)))
    for h in (hits or [])[:3]:
        print("      %s" % (str(h)[:150],))

    hist = read_history(limit=20)
    print("    read_history(20) -> %d entries" % len(hist or []))

    fmt = format_experience_context(hits or [], max_chars=400)
    print("    format_experience_context -> %d chars: %r" % (len(fmt or ""), (fmt or "")[:90]))

    if fb_iri or impact_iri:
        why = why_action(fb_iri or impact_iri)
        print("    why_action -> %d keys: %s"
              % (len(why or {}), sorted((why or {}).keys())[:8]))

    # 4. the four functions the bridge pyc imports but never calls. They live in
    #    projects/tl-ego/memory_bus_bridge.py, which was lost in the 09-30 wipe.
    print("\n[4] the 4 unused bridge imports:")
    for name in ("query_related_experiences", "query_self_model_insights",
                 "search_bistable_solutions", "search_integrator_suppression_fixes"):
        try:
            import memory_bus_bridge as MBB
            fn = getattr(MBB, name, None)
            print("    %-42s %s" % (name, "present" if fn else "module loaded, attr MISSING"))
        except ModuleNotFoundError as e:
            print("    %-42s MISSING MODULE (%s)" % (name, e))
            break
        except Exception as e:
            print("    %-42s import error: %s" % (name, e))
            break

    # 5. verdict on the loop
    loop_closed = bool(hits) or any(
        PROBE in str(x) for x in (hist or []))
    print("\n[verdict] write half: OK | read half: %s | closed loop: %s"
          % ("OK" if hits else "NO HITS",
             "CLOSED" if loop_closed else "OPEN (writes land, reads do not find them)"))
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
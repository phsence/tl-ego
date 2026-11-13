"""Prove ego's LIVE query_memorybus handler now returns real experience.

The [memorybus] log line is 0 in every generation, which is ambiguous: the block
is gated behind the internal tool "query_memorybus" being selected, so a 0 count
does not prove the read path is broken. This calls the same handler the tool
router calls (tl_ego_server._query_memorybus_handler) in the live server module
and requires it to find experiences.

What it proves, precisely: search_experience() and format_experience_context()
as bound INSIDE tl_ego_server resolve to the real bridge (not the
`return []` fallbacks at tl_ego_server.py:344-347), and RESTING_STATE receives a
non-empty result set.

Read-only: it does not write to l2_content.jsonl and does not restart anything.

Run: python verify_memorybus_handler_live.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    import tl_ego_server as T

    # the fallbacks at :344-347 mean the bridge import failed at boot
    assert T._MEMORY_BUS_BRIDGE, \
        "_MEMORY_BUS_BRIDGE is False — tl_ego_server imported the stub fallbacks"

    fn = T.search_experience
    print("search_experience bound to: %s" % getattr(fn, "__module__", "?"))
    assert getattr(fn, "__module__", "") != "tl_ego_server", \
        "tl_ego_server is using its OWN `return []` stub, not the real bridge"

    T.RESTING_STATE.setdefault("dominant_type", "integrator")
    T.RESTING_STATE["stagnation_streak"] = 5
    T.RESTING_STATE["competition"] = 0.9
    T.RESTING_STATE["trajectory"] = [{"dominant": "integrator"}] * 5 + \
                                    [{"dominant": "emotional"}] * 5 + \
                                    [{"dominant": "integrator"}] * 5 + \
                                    [{"dominant": "action"}] * 5

    ok = T._query_memorybus_handler()
    print("_query_memorybus_handler() -> %r" % (ok,))

    results = T.RESTING_STATE.get("_memorybus_results")
    ctx = T.RESTING_STATE.get("_memorybus_context")
    print("RESTING_STATE['_memorybus_results'] -> %s (%s)"
          % (len(results) if results is not None else None,
             type(results).__name__))
    print("RESTING_STATE['_memorybus_context'] -> %d chars" % len(ctx or ""))

    assert results, "handler returned with ZERO experiences — read path still dead"
    assert ctx, "context formatter produced nothing from a non-empty result set"
    print("\n   sample context: %r" % (ctx or "")[:180])

    assert T.RESTING_STATE.get("_memorybus_query_tick"), "query tick not recorded"

    print("\nOK: ego's own query_memorybus tool path now returns real experience "
          "(was: 0 results, and the [memorybus] log line never appeared)")


if __name__ == "__main__":
    main()
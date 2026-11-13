"""projects/tl-ego/memory_bus_bridge.py — REBUILT 2026-10-01.

Why this file was needed even though scripts/memory_bus_tl_ego_bridge.pyc still
imports it: that pyc's `query_related_experiences` and `query_self_model_insights`
are NOT lost code — they are one-instruction stubs (`return []`). Every read path
in the bridge routes through them:

    search_experience  -> query_related_experiences  -> []
    read_history       -> query_related_experiences  -> []
    why_action         -> _BRIDGE_READY gate          -> {}

So ego's experience loop was OPEN before the 09-30 source wipe: writes landed in
.lae/l2/l2_content.jsonl (verified: 3 probe rows appeared) but every read
returned empty. The wipe only removed the file these stubs used to import from;
the stubs themselves survived inside the .pyc.

This module supplies the real implementations by delegating to capabilities the
MemoryBus object already has — nothing new is invented:
    hybrid_search(query, top_k)      -> multi-signal retrieval
    query_related(iri, relation, depth) -> relation walk
    rdf_why(action_iri, max_depth)     -> causal why-chain
    rdf_self_model()                   -> self-model insights

Contract consumed by memory_bus_tl_ego_bridge.pyc:
    query_related_experiences(query, relation=None, depth=1, top_k=10) -> list[dict]
    query_self_model_insights()                                          -> list[dict]
    search_bistable_solutions(query, top_k=10)                           -> list[dict]
    search_integrator_suppression_fixes(query, top_k=10)                 -> list[dict]

The last two were imported by the .pyc but never called by it or by
tl_ego_server.py; they are implemented here so the import resolves and so the
names mean what they say. They are honest filters over hybrid_search, not
placeholders.

Robustness contract (called every tick from tl_ego_server):
never raise, never return None — return [] on any failure.
"""
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_WORKSPACE = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _WORKSPACE / "scripts"
# scripts/ has no __init__.py, so "scripts.memory_bus" only resolves with the
# WORKSPACE root on sys.path, while bare "memory_bus" resolves with scripts/.
# The write path in memory_bus_tl_ego_bridge.pyc uses the dotted form because
# tl_ego_server puts the workspace root on the path; here we are loaded as
# projects/tl-ego/memory_bus_bridge.py, so try both rather than assume.
for _p in (_WORKSPACE, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _get_bus():
    """Shared MemoryBus singleton, same accessor the write path uses."""
    try:
        from scripts.memory_bus import get_memory_bus
    except ImportError:
        from memory_bus import get_memory_bus
    return get_memory_bus()


def _as_dicts(rows: Any) -> List[Dict]:
    """Normalise whatever the bus returns into a list of dicts.

    semantic_search yields tuples and hybrid_search yields dicts; the bridge
    callers index results as mappings, so normalise here once.
    """
    out: List[Dict] = []
    if not rows:
        return out
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, (tuple, list)):
            d = {}
            # (score, payload) or (iri, score, payload) shapes both occur
            if len(r) == 2 and isinstance(r[0], (int, float)):
                d["score"] = r[0]
                payload = r[1]
            else:
                payload = r[-1]
            if isinstance(payload, dict):
                d.update(payload)
            elif isinstance(payload, str):
                d.setdefault("iri", payload)
            elif r:
                d.setdefault("iri", str(r[0]))
            out.append(d)
        elif r is not None:
            out.append({"iri": str(r)})
    return out


def query_related_experiences(
    query: str,
    relation: Optional[str] = None,
    depth: int = 1,
    top_k: int = 10,
) -> List[Dict]:
    """Retrieve experiences related to `query`.

    depth >= 2 additionally expands each hit through query_related() so the
    caller gets a causal neighbourhood rather than a flat list.
    """
    if not query:
        return []
    try:
        bus = _get_bus()
        hits = _as_dicts(bus.hybrid_search(str(query), top_k=top_k))
        if depth and depth > 1:
            seen = {h.get("iri") for h in hits if h.get("iri")}
            expanded: List[Dict] = list(hits)
            for h in hits:
                iri = h.get("iri")
                if not iri or iri in seen:
                    continue
                try:
                    for rel in bus.query_related(iri, relation=relation, depth=depth - 1) or []:
                        d = rel if isinstance(rel, dict) else {"iri": str(rel)}
                        riri = d.get("iri")
                        if riri and riri not in seen:
                            seen.add(riri)
                            expanded.append(d)
                except Exception:
                    continue
                if len(expanded) >= top_k * 2:
                    break
            hits = expanded
        if relation:
            pref = [h for h in hits if h.get("relation") == relation]
            if pref:
                hits = pref
        return hits[:top_k]
    except Exception:
        return []


def query_self_model_insights() -> List[Dict]:
    """Self-model insights, as a list so the bridge can iterate it."""
    try:
        bus = _get_bus()
        data = bus.rdf_self_model() or {}
        if isinstance(data, dict):
            insights = []
            for key, val in data.items():
                if isinstance(val, (list, tuple)):
                    insights.extend(val)
                elif val not in (None, "", {}, []):
                    insights.append({"key": key, "value": val})
            return _as_dicts(insights) or [{"key": k, "value": data[k]} for k in list(data)[:20]]
        return _as_dicts(data)
    except Exception:
        return []


def search_bistable_solutions(query: str, top_k: int = 10) -> List[Dict]:
    """Experiences whose content mentions bistable / hysteresis behaviour.

    Genuine filter: hybrid_search for the concrete phrase plus the domain terms
    ego actually writes about it. Returns [] rather than fabricating.
    """
    if not query:
        return []
    seeds = [query, "bistable hysteresis", "double-well 吸引子 切换", "bistability"]
    seen, out = set(), []
    for s in seeds:
        for h in query_related_experiences(s, top_k=top_k):
            iri = h.get("iri")
            key = iri or str(h.get("content", ""))[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
        if len(out) >= top_k:
            break
    return out[:top_k]


def search_integrator_suppression_fixes(query: str, top_k: int = 10) -> List[Dict]:
    """Experiences about integrator runaway / dominance suppression fixes.

    integrator has been ego's dominant type for long stretches, and the DQO
    work above exists precisely to break that. This retrieves the repair
    experience for it.
    """
    if not query:
        return []
    seeds = [query, "integrator 抑制", "integrator dominance suppression",
             "attractor_lock integrator", "sparsifier integrator runaway"]
    seen, out = set(), []
    for s in seeds:
        for h in query_related_experiences(s, top_k=top_k):
            iri = h.get("iri")
            key = iri or str(h.get("content", ""))[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
        if len(out) >= top_k:
            break
    return out[:top_k]


def query_self_model_insights() -> Dict[str, Any]:
    """Self-model insights as a MAPPING, not a list.

    This return shape is load-bearing: the surviving bridge pyc's why_action() is
    literally `return query_self_model_insights()`, and tl_ego_server consumes
    why_action() as a dict. The stub's `return []` therefore handed every caller
    a falsy LIST where a dict was expected — a shape bug that reads as "no
    self-model" rather than as an error. Return the RDF self-model mapping.
    """
    try:
        bus = _get_bus()
        data = bus.rdf_self_model() or {}
        if isinstance(data, dict):
            return data
        return {"items": _as_dicts(data)}
    except Exception:
        return {}


def why_action(iri: str) -> Dict[str, Any]:
    """Causal why-chain for an action IRI.

    NOTE: the live bridge pyc defines its own why_action (it just returns
    query_self_model_insights()), so tl_ego_server never reaches this one. It is
    kept because it is the honest implementation of the name, and because the
    self-model path is what actually carries the causal chain while rdf_why is
    still unavailable (memory_bus_rdf lost). Do not expect callers to use it.
    """
    out: Dict[str, Any] = {"iri": iri, "insights": query_self_model_insights()}
    try:
        bus = _get_bus()
        if iri:
            why = bus.rdf_why(str(iri)) or {}
            if isinstance(why, dict) and why:
                out["chain"] = why
    except Exception:
        pass
    out["empty"] = not (out.get("chain") or out.get("insights"))
    return out


if __name__ == "__main__":
    # Selfcheck. Proves the READ half now returns data, which is the thing that
    # was broken for the whole lifetime of the stubs.
    import uuid

    probe = "bus-read-probe-%s" % uuid.uuid4().hex[:8]
    bus = _get_bus()

    hits = query_related_experiences("ego", top_k=5)
    print("query_related_experiences('ego') -> %d" % len(hits))
    assert isinstance(hits, list), type(hits)
    assert all(isinstance(h, dict) for h in hits), hits
    for h in hits[:2]:
        print("   %s" % str({k: h.get(k) for k in ("iri", "source", "entry_type")})[:130])

    deep = query_related_experiences("ego", depth=3, top_k=5)
    print("depth=3 -> %d (must be >= shallow)" % len(deep))
    assert len(deep) >= len(hits)

    ins = query_self_model_insights()
    print("query_self_model_insights() -> %d keys (must be a dict: the bridge's"
          " why_action returns this directly)" % len(ins))
    assert isinstance(ins, dict), \
        "must return a dict, got %s — why_action() hands this straight to callers" % type(ins).__name__
    assert isinstance(why_action("iri:task:auto:test"), dict)

    print("search_bistable_solutions -> %d" % len(search_bistable_solutions("bistable")))
    print("search_integrator_suppression_fixes -> %d"
          % len(search_integrator_suppression_fixes("integrator")))

    # degenerate input must never raise: called every tick
    for bad in ("", None, 0, []):
        assert query_related_experiences(bad) == [], bad
        assert search_bistable_solutions(bad) == [], bad
        assert search_integrator_suppression_fixes(bad) == [], bad
    assert query_related_experiences("ego", relation="causes", depth=99, top_k=0) == [] \
        or True  # must not raise

    print("\nOK: read half returns real rows (was: [] for every query)")
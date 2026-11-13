"""
L7→L5 Bridge — Inject External Observations into L5 Perception Pipeline

This is the critical integration point: L7 exploration results become
L5-compatible observations that flow through the standard perception pipeline.

The bridge:
1. Converts L7 ActionResult dicts into L5 observation format
2. Injects them into RESTING_STATE["external_evidence"]["l7_observations"]
3. Tags each with source="l7_external" and the target neuron type
4. Sets evidence TTL so L5 naturally consumes them

Type → observation mapping:
  L7 result type      → L5 observation fields
  web_search          → domain="sensory", categories={types_searched: 1}
  web_fetch           → domain="sensory", file-like content
  file_read           → domain="memory", file content
  workspace_scan      → domain="sensory", workspace structure
  exec_command        → domain="action", command output
"""

import time, json, random, hashlib, urllib.parse
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

TZ_CST = timezone(timedelta(hours=8))

# ── Type mapping: L7 source → best-fit L5 domain/type ──
TOOL_TYPE_MAP = {
    "web_fetch": {"domain": "sensory", "types": ["sensory", "curiosity", "novelty"]},
    "web_search": {"domain": "sensory", "types": ["sensory", "curiosity", "novelty", "social"]},
    "file_read": {"domain": "memory", "types": ["memory", "consolidation"]},
    "workspace_scan": {"domain": "sensory", "types": ["sensory", "action"]},
    "exec_command": {"domain": "action", "types": ["action", "decider"]},
}

# ── Summarize ──
def _truncate(text: str, length: int = 300) -> str:
    if len(text) > length:
        return text[:length] + "..."
    return text


def _is_low_quality_search_host(url: str) -> bool:
    """Reject dictionary/translation/encyclopedia junk that Bing often returns."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if not host:
        return True
    markers = (
        "dictionary.", "dict.", "iciba.", "youdao.", "eudic.",
        "merriam-webster", "oxfordlearners", "collinsdictionary",
        "baike.baidu", "fanyi.", "translate.", "vocabulary.com",
    )
    if any(m in host for m in markers):
        return True
    if "bing." in host and ("/dict" in path or "dict" in query):
        return True
    return False


def assess_web_evidence(result: dict) -> dict:
    """Assess whether a web search returned attributable, usable evidence."""
    data = result.get("data", {}) if isinstance(result, dict) else {}
    raw_results = data.get("results", []) if isinstance(data, dict) else []
    backend = str(data.get("backend", "unknown")) if isinstance(data, dict) else "unknown"
    query_terms = {
        term for term in re.findall(r"[a-z0-9]{3,}", str(data.get("query", "")).lower())
        if term not in {"and", "the", "for", "with", "from", "latest", "most"}
    }
    # Prefer content-bearing research tokens when scoring relevance; ignore
    # mission-template filler that makes dictionary pages look "related".
    soft_terms = {
        "activate", "dormant", "explore", "novel", "topics", "general",
        "knowledge", "current", "events", "scan", "critically", "review",
        "past", "decisions", "find", "contradictions", "web", "search",
    }
    hard_terms = query_terms - soft_terms
    relevance_terms = hard_terms or query_terms
    valid_results = []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        parsed = urllib.parse.urlsplit(url)
        if _is_low_quality_search_host(url):
            continue
        has_content = bool(str(item.get("title", "")).strip() or str(item.get("content", "")).strip())
        searchable = f"{item.get('title', '')} {item.get('content', '')} {parsed.path}".lower()
        term_matches = sum(1 for term in relevance_terms if term in searchable)
        # arXiv is structured and already filtered upstream; trust backend==arxiv
        # with real content even when query template terms are abstract.
        if backend == "arxiv" and has_content and parsed.hostname:
            relevant = True
        else:
            relevant = not relevance_terms or term_matches >= min(2, len(relevance_terms))
        score = item.get("score", 0.0)
        try:
            score = float(score or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        backend_relevant = backend != "tavily" or score >= 0.5
        if parsed.scheme in {"http", "https"} and parsed.hostname and has_content and relevant and backend_relevant:
            valid_results.append(item)

    source_urls = [str(item.get("url", "")) for item in valid_results[:5]]
    source_domains = list(dict.fromkeys(
        urllib.parse.urlsplit(url).hostname or "" for url in source_urls
    ))
    valid_count = len(valid_results)
    quality_score = round(min(1.0, valid_count / 3.0), 3)
    return {
        "valid": result.get("status") == "ok" and valid_count > 0,
        "result_count": len(raw_results) if isinstance(raw_results, list) else 0,
        "valid_result_count": valid_count,
        "quality_score": quality_score,
        "source_urls": source_urls,
        "source_domains": source_domains,
        "backend": backend,
        "reason": "usable_public_sources" if valid_count else "no_relevant_attributable_web_results",
    }


def action_result_to_observation(result: dict, target_type: str = None) -> dict:
    """Convert an L7 ActionResult dict into an L5 observation dict.
    
    Returns a dict matching the format L5 expects from _scan_workspace():
    {
        "source": "l7_external",
        "status": "ok",
        "count": int,
        "files": list,
        "categories": dict,
        "summary": str,
        "intent_type": "external_exploration",
        "domain": "sensory"|"memory"|"action",
        "trigger_types": [str],  # L5 types this observation aims to activate
        ...
    }
    """
    tool = result.get("tool", "unknown")
    data = result.get("data", {})
    summary = result.get("summary", "")
    
    type_info = TOOL_TYPE_MAP.get(tool, {"domain": "sensory", "types": ["sensory"]})
    trigger_types = list(type_info["types"])
    if target_type:
        trigger_types = [target_type, *[t for t in trigger_types if t != target_type]]
    # Deduplicate preserving order
    trigger_types = list(dict.fromkeys(trigger_types))
    # Mission target must win seed bias. Tool default domain (often sensory)
    # previously became primary and starved the intended dormant type.
    primary_domain = target_type or type_info["domain"]
    
    # Build observation
    obs = {
        "source": "l7_external",
        "status": result.get("status", "error"),
        "error": result.get("error") if result.get("status") == "error" else None,
        "count": data.get("count", 0) if isinstance(data, dict) else 1,
        "tool": tool,
        "timestamp": result.get("timestamp", datetime.now(TZ_CST).isoformat()),
        "intent_type": "external_exploration",
        "domain": primary_domain,
        "tool_domain": type_info["domain"],
        "trigger_types": trigger_types,
        "l7_mission_target": target_type,
        "l7_observation_id": hashlib.md5(
            (tool + str(time.time()) + str(random.random())).encode()
        ).hexdigest()[:12],
    }
    
    # Convert tool-specific data
    if tool == "web_search":
        results = data.get("results", []) if isinstance(data, dict) else []
        evidence = assess_web_evidence(result)
        obs.update({
            "categories": {"web_results": len(results)},
            "summary": f"[L7] Web search: {data.get('query', '')[:80]} — {len(results)} results" if isinstance(data, dict) else summary,
            "content_preview": _truncate(json.dumps(results[:3], ensure_ascii=False)) if results else summary,
            "files": evidence["source_urls"],
            "evidence_valid": evidence["valid"],
            "evidence_quality": evidence["quality_score"],
            "evidence_reason": evidence["reason"],
            "source_domains": evidence["source_domains"],
            "search_backend": evidence["backend"],
        })
    elif tool == "web_fetch":
        text = data.get("text", "") if isinstance(data, dict) else ""
        obs.update({
            "categories": {"fetched_pages": 1},
            "summary": f"[L7] Fetched: {data.get('url', '')[:80]}" if isinstance(data, dict) else summary,
            "content_preview": _truncate(text),
            "files": [data.get("url", "")] if isinstance(data, dict) else [],
        })
    elif tool == "file_read":
        content = data.get("content", "") if isinstance(data, dict) else ""
        obs.update({
            "categories": {"read_files": 1},
            "summary": f"[L7] Read: {data.get('filepath', '')[:80]}" if isinstance(data, dict) else summary,
            "content_preview": _truncate(content),
            "files": [data.get("filepath", "")] if isinstance(data, dict) else [],
        })
    elif tool == "workspace_scan":
        obs.update({
            "categories": {"dirs_scanned": data.get("dirs", 0)} if isinstance(data, dict) else {},
            "summary": f"[L7] Workspace scan: {data.get('dirs', 0)} dirs" if isinstance(data, dict) else summary,
        })
    elif tool == "exec_command":
        stdout = data.get("stdout", "") if isinstance(data, dict) else ""
        obs.update({
            "categories": {"command_executed": 1},
            "summary": f"[L7] Exec: {_truncate(data.get('command', ''), 100)}" if isinstance(data, dict) else summary,
            "content_preview": _truncate(stdout),
        })
    else:
        obs["summary"] = f"[L7] {tool}: {_truncate(summary, 150)}"
    
    return obs


def inject_observation(observation: dict, resting_state: dict) -> dict:
    """Inject an L7 external observation into L5's RESTING_STATE.
    
    This is the main entry point for L7→L5 communication.
    The observation is stored in external_evidence under "l7_observations"
    with a TTL so L5's natural consumption cycle picks it up.
    
    Returns a status dict.
    """
    obs_id = observation.get("l7_observation_id", "unknown")

    if observation.get("tool") == "web_search" and not observation.get("evidence_valid", False):
        return {
            "injected": False,
            "observation_id": obs_id,
            "tool": "web_search",
            "trigger_types": [],
            "reason": observation.get("evidence_reason", "invalid_web_evidence"),
            "queue_size": len(resting_state.get("external_evidence", {}).get("l7_observations", [])),
        }
    
    # Initialize L7 observation queue in external_evidence
    evidence = resting_state.setdefault("external_evidence", {})
    l7_obs_list = evidence.setdefault("l7_observations", [])
    
    # Add with timestamp
    observation["injected_at"] = time.time()
    observation["injected_tick"] = resting_state.get("tick", 0)
    observation["l7_ttl"] = 30  # Keep for 30 ticks to survive KWTA propagation delay
    
    l7_obs_list.append(observation)
    
    # Cap queue to prevent memory leak
    if len(l7_obs_list) > 50:
        evidence["l7_observations"] = l7_obs_list[-50:]
    
    # Set TTL
    ttl_dict = resting_state.setdefault("evidence_ttl", {})
    ttl_dict[f"l7_obs_{obs_id}"] = 10
    
    return {
        "injected": True,
        "observation_id": obs_id,
        "source": observation.get("source"),
        "tool": observation.get("tool"),
        "trigger_types": observation.get("trigger_types", []),
        "summary": observation.get("summary", "")[:120],
        "injected_at_tick": resting_state.get("tick", 0),
        "queue_size": len(evidence.get("l7_observations", [])),
    }


def process_l7_results(l7_results: list, resting_state: dict) -> list:
    """Batch process L7 action results into L5 observations and inject them.
    
    Args:
        l7_results: List of (mission, result_dict) tuples
        resting_state: L5's RESTING_STATE dict (mutable)
    
    Returns:
        List of injection status dicts
    """
    injected = []
    for mission, result in l7_results:
        target = mission.target_type if mission else None
        obs = action_result_to_observation(result, target_type=target)
        status = inject_observation(obs, resting_state)
        injected.append(status)
    return injected


def collect_l7_observations(resting_state: dict) -> list:
    """Collect pending L7 observations from external_evidence.
    
    L5's action gateway should call this to check for L7 external input.
    Observations older than TTL are automatically cleaned up.
    
    Returns: list of active L7 observations (newest first)
    """
    evidence = resting_state.get("external_evidence", {})
    l7_obs = evidence.get("l7_observations", [])
    ttl_dict = resting_state.get("evidence_ttl", {})
    tick = resting_state.get("tick", 0)
    
    now = time.time()
    active = []
    stale = []
    
    for obs in l7_obs:
        obs_id = obs.get("l7_observation_id", "")
        ttl_key = f"l7_obs_{obs_id}"
        ttl = ttl_dict.get(ttl_key, 10)
        age_ticks = tick - obs.get("injected_tick", tick)
        age_time = now - obs.get("injected_at", now)
        
        if age_ticks < ttl and age_time < 300:  # 5 min real-time cap
            active.append(obs)
        else:
            stale.append(obs)
    
    # Clean up stale observations
    if stale:
        stale_ids = {s.get("l7_observation_id") for s in stale}
        evidence["l7_observations"] = [o for o in l7_obs if o.get("l7_observation_id") not in stale_ids]
        for sid in stale_ids:
            ttl_dict.pop(f"l7_obs_{sid}", None)
    
    # Return newest first
    active.sort(key=lambda x: x.get("injected_at", 0), reverse=True)
    return active


def get_l7_bridge_status(resting_state: dict) -> dict:
    """Status snapshot for the /l7-status endpoint."""
    evidence = resting_state.get("external_evidence", {})
    l7_obs = evidence.get("l7_observations", [])
    ttl_dict = resting_state.get("evidence_ttl", {})
    
    trigger_types = {}
    for obs in l7_obs:
        for t in obs.get("trigger_types", []):
            trigger_types[t] = trigger_types.get(t, 0) + 1
    
    return {
        "queued_observations": len(l7_obs),
        "active_types_triggered": len(trigger_types),
        "ttl_entries": len(ttl_dict),
        "tools_used": list({o.get("tool") for o in l7_obs if o.get("tool")}),
        "trigger_type_distribution": trigger_types,
        "oldest_obs_age_s": round(time.time() - (l7_obs[0]["injected_at"] if l7_obs else time.time())),
        "latest_obs_domain": l7_obs[-1].get("domain") if l7_obs else None,
        "latest_obs_summary": l7_obs[-1].get("summary", "")[:120] if l7_obs else None,
    }

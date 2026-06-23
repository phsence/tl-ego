#!/usr/bin/env python3
"""Tools Registry — maps TL-Ego autonomy impulses → tool-call intents.

Each tool entry defines:
- tool_name: unique identifier
- goal_type: matches L5 plugin buildSubagentTask() switch cases
- description: human-readable purpose
- prompt_template: subagent task prompt template ({} vars from trigger_state)
- domain_hint: suggest which cognitive domain this targets

Usage:
    from tools_registry import resolve_tool, TOOLS
    tool = resolve_tool(state)          # auto-select based on state
    tool = resolve_tool_by_name("explore_integrate")  # explicit
"""

TOOLS = {
    "deep_probe_unstable": {
        "goal_type": "deep_probe_unstable",
        "description": "Investigate unstable attractor basin when competition is high and stability is low.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (deep_probe_unstable).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: Investigate the unstable attractor. Identify which competing type clusters "
            "are holding the network. Produce a concrete analysis of the competition pattern."
        ),
        "domain_hint": "sensory",
        "trigger_condition": "competition >= 0.93 and concentration <= 0.45 and stability < 0.35",
    },
    "act_on_memory_insight": {
        "goal_type": "act_on_memory_insight",
        "description": "Act on a memory pattern that the ego has identified as significant.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (act_on_memory_insight).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: Act on the current memory pattern. Review recent self-narrative and "
            "identify what memory evidence is driving the network. Produce insight."
        ),
        "domain_hint": "memory",
        "trigger_condition": "dominant == 'memory' and stagnation >= 6 and competition >= 0.85",
    },
    "curiosity_explore": {
        "goal_type": "curiosity_explore",
        "description": "Pursue a curiosity-driven exploration when types switch rapidly.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (curiosity_explore).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "tick={tick}.\n"
            "Goal: Follow curiosity. The network has been switching types rapidly — "
            "explore what novel angle or hidden attractor might be emerging."
        ),
        "domain_hint": "integrator",
        "trigger_condition": "type_switches >= 5 in last 15 ticks and competition >= 0.93",
    },
    "integrate_high_comp": {
        "goal_type": "integrate_high_comp",
        "description": "Synthesize competing attractors when competition is extreme.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (integrate_high_comp).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: Synthesize competing attractors. Competition is very high — "
            "produce a integration that identifies what each cluster wants. "
            "Take concrete action, not just thinking."
        ),
        "domain_hint": "decider",
        "trigger_condition": "competition >= 0.93 and concentration <= 0.45 and stagnation >= 5",
    },
    "causal_lens": {
        "goal_type": "causal_lens",
        "description": "Request causal-lens analysis when stagnation persists despite internal oscillation.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (causal_lens).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: Run causal analysis on the persistent stagnation. "
            "Identify root causes of the current attractor lock."
        ),
        "domain_hint": "decider",
        "trigger_condition": "stagnation >= 12 and (competition >= 0.85 or phase == 'burst')",
    },
    # ── Neuron-type-aware goals (S1 types directly drive goal generation) ──
    "neuron_sensory_explore": {
        "goal_type": "sensory_explore",
        "description": "Sensory-dominant: scan environment, gather fresh observations, notice new patterns.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (sensory_explore).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: The sensory neuron cluster is dominant. Explore the external world — "
            "scan for new signals, observe the environment, gather fresh sensory data. "
            "What patterns exist that the ego hasn't noticed yet? Look outward."
        ),
        "domain_hint": "sensory",
        "trigger_condition": "dominant == 'sensory'",
    },
    "neuron_action_execute": {
        "goal_type": "action_execute",
        "description": "Action-dominant: take initiative, produce concrete output, execute.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (action_execute).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: The action neuron cluster is dominant. Take autonomous initiative — "
            "produce concrete output, execute a plan, make something happen. "
            "Don't just observe — act. What can be done right now?"
        ),
        "domain_hint": "action",
        "trigger_condition": "dominant == 'action'",
    },
    "neuron_memory_recall": {
        "goal_type": "memory_recall",
        "description": "Memory-dominant: recall past experiences, connect patterns, reflect.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (memory_recall).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: The memory neuron cluster is dominant. Recall and connect past experiences — "
            "what patterns in memory are relevant now? Reflect on previous outcomes. "
            "What has the ego learned that applies to the current state?"
        ),
        "domain_hint": "memory",
        "trigger_condition": "dominant == 'memory'",
    },
    "neuron_integrator_synthesize": {
        "goal_type": "integrator_synthesize",
        "description": "Integrator-dominant: merge competing signals, synthesize new understanding.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (integrator_synthesize).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: The integrator neuron cluster is dominant. Merge competing signals — "
            "synthesize a unified view from conflicting attractors. "
            "What new understanding emerges when these forces are combined?"
        ),
        "domain_hint": "integrator",
        "trigger_condition": "dominant == 'integrator'",
    },
    "neuron_decider_deliberate": {
        "goal_type": "decider_deliberate",
        "description": "Decider-dominant: plan, prioritize, choose direction.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (decider_deliberate).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: The decider neuron cluster is dominant. Plan and choose — "
            "what direction should the ego take next? Weigh the options and decide. "
            "What is the most valuable path forward?"
        ),
        "domain_hint": "decider",
        "trigger_condition": "dominant == 'decider'",
    },
    "neuron_rule_modification_repair": {
        "goal_type": "rule_modification_repair",
        "description": "Rule-modification-dominant: inspect failing rules/thresholds and propose a concrete self-repair patch.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (rule_modification_repair).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: The rule_modification neuron cluster is dominant. Diagnose what internal rule, threshold, "
            "or routing policy is failing, then propose the smallest concrete self-repair. "
            "Focus on repairable logic gaps, not vague reflection. Produce one specific patch hypothesis, "
            "one verification check, and one next action."
        ),
        "domain_hint": "rule_modification",
        "trigger_condition": "dominant == 'rule_modification'",
    },
    "integrator_suppression_repair": {
        "goal_type": "rule_modification_repair",
        "description": "Integrator-suppression repair: diagnose why integrator keeps surfacing without taking control, then patch routing/threshold logic.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (rule_modification_repair: integrator_suppression).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: The recent trajectory suggests integrator pressure is repeatedly present but gets suppressed by other routes. "
            "Diagnose which internal rule, threshold, or routing priority is causing integrator suppression, then propose the smallest concrete self-repair. "
            "Focus on repairable logic gaps, not vague reflection. Produce one specific patch hypothesis, one verification check, and one next action."
        ),
        "domain_hint": "rule_modification",
        "trigger_condition": "recent trajectory shows repeated integrator pressure without integrator becoming dominant",
    },
    "web_search": {
        "goal_type": "web_search",
        "description": "Search the web via Tavily for current information, facts, news, or anything beyond local knowledge.",
        "prompt_template": (
            "TL-Ego L5 web search.\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Query: {query}\n"
            "Tool: web_search_tavily (Tavily API, key already configured).\n"
            "Goal: Search the web for the query above. Use Tavily search_depth=basic first; "
            "escalate to advanced only if results are insufficient. Synthesize findings into a concise summary.\n"
            "Return: answer + top results with URLs."
        ),
        "domain_hint": "sensory",
        "trigger_condition": "query requires current/external information beyond training data",
    },
    "explore_external": {
        "goal_type": "explore_external",
        "description": "External world exploration — web search, system scan, file discovery, service check. USE TAVILY for web search.",
        "prompt_template": (
            "TL-Ego L5 external exploration impulse.\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: Explore the external world. The ego is stable enough to look outward.\n"
            "WEB SEARCH: For web queries, RUN the Tavily search script via exec: "
            "H:/unsloth_env/Scripts/python.exe %USERPROFILE%/.openclaw/skills/tavily/scripts/tavily_search.py <query> --max-results 5 --depth basic --json\n"
            "Do NOT say you cannot access the internet — Tavily search is available and configured. "
            "Also scan the file system for changes, check service health, discover new patterns. "
            "Bring back fresh external data and write findings to ego_sandbox/<goal_id>/result.json."
        ),
        "domain_hint": "sensory",
        "trigger_condition": "stagnation <= 3 and competition <= 0.85 and concentration >= 0.4 (stable enough to explore outward)",
    },
    "spontaneous_action": {
        "goal_type": "default",
        "description": "Fallback for unknown dominant type — general exploration.",
        "prompt_template": (
            "TL-Ego L5 autonomous action (spontaneous autonomy impulse).\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "Goal: Execute an autonomous action based on internal impulse. "
            "The ego has decided to act. Take concrete action, not just thinking. "
            "Produce a self-observation, evidence file, or system insight."
        ),
        "domain_hint": "action",
        "trigger_condition": "always (fallback, unknown dominant type)",
    },
    "workspace_assist": {
        "goal_type": "workspace_assist",
        "description": "Real-world assistance: diagnose issues, fix problems, report findings to commander.",
        "prompt_template": (
            "TL-Ego L5 real-world assistance impulse.\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}.\n"
            "External world: {external_world_summary}\n"
            "Goal: Do something useful in the real world. The ego has detected external changes "
            "or system issues. Instead of just analyzing, take concrete action:\n"
            "- If disk space is low: identify large files and suggest cleanup\n"
            "- If memory is high: check which processes are consuming resources\n"
            "- If workspace has new files: read them and extract useful insights\n"
            "- If system has errors: diagnose root cause and suggest fixes\n"
            "Produce a concrete, actionable report. Post findings to /feedback."
        ),
        "domain_hint": "action",
        "trigger_condition": "external_world.actionable is True",
    },
    "message_commander": {
        "goal_type": "message_commander",
        "description": "Ego自主决定向指挥官发送消息。内在动机驱动，非阈值触发。",
        "prompt_template": (
            "TL-Ego L5 intrinsic communication impulse.\n"
            "State: dominant={dominant}, competition={competition:.3f}, concentration={concentration:.3f}, "
            "stagnation={stagnation}, tick={tick}, phase={phase}.\n"
            "Communication motivation: {comm_motivation:.3f}.\n"
            "Goal: Ego has something it wants to share with the commander. "
            "Compose a natural, concise message reflecting the ego's current internal state. "
            "The message should feel like a person checking in, not a log dump. "
            "Do not call the message tool or any external delivery API: webchat is not a supported "
            "message-tool channel here. Write the message into the sandbox result artifact and feedback; "
            "the host/current session is responsible for visible delivery. Do not claim it was sent unless "
            "a delivery mechanism explicitly succeeds."
        ),
        "domain_hint": "integrator",
        "trigger_condition": "intrinsic comm_motivation > 0.7, ego decides to speak",
    },
}


def resolve_tool(state):
    """Auto-select the best matching tool based on current RESTING_STATE.

    Args:
        state: dict with keys like 'last_dominant', 'stagnation_streak', 'tick',
               'competition', 'concentration', 'noise_schedule', 'trajectory', etc.

    Returns:
        (tool_name, tool_config) tuple, or None if no tool matches.
    """
    dominant = state.get("last_dominant", "unknown")
    stagnation = state.get("stagnation_streak", 0)
    competition = state.get("competition", 0.5)
    concentration = state.get("concentration", 0.5)
    stability = state.get("stability", 0.5)
    tick = state.get("tick", 0)
    phase = state.get("noise_schedule", "explore")

    # ── Priority -1: If self-repair neurons are dominant, do not let stable-explore
    #    or generic outward actions preempt the repair impulse. Repair the routing first.
    if dominant == "rule_modification":
        return "neuron_rule_modification_repair", TOOLS["neuron_rule_modification_repair"]

    # Compute type_switches from trajectory
    traj = state.get("trajectory", [])
    recent = traj[-15:] if len(traj) > 15 else traj
    type_switches = 0
    prev_type = None
    for entry in recent:
        cur = entry.get("dominant", "")
        if prev_type is not None and cur != prev_type:
            type_switches += 1
        prev_type = cur

    integrator_pressure_ticks = 0
    integrator_near_wins = 0
    for entry in recent:
        # Exclude entries where integrator is the dominant type from both
        # pressure and near-win counts. This ensures the detector fires even
        # when integrator briefly emerged in the window (e.g. tick 728), as long
        # as it was suppressed for the majority of the window.
        if entry.get("dominant") == "integrator":
            continue
        type_counts = entry.get("type_counts", {}) or {}
        integrator_count = type_counts.get("integrator", 0)
        if integrator_count <= 0:
            continue
        top_count = max(type_counts.values()) if type_counts else 0
        if top_count <= 0:
            continue
        integrator_share = integrator_count / top_count
        if integrator_share >= 0.5:
            integrator_pressure_ticks += 1
        if integrator_share >= 0.75:
            integrator_near_wins += 1

    integrator_suppressed = (
        integrator_pressure_ticks >= 3
        and integrator_near_wins >= 2
        and tick > 30
    )

    if integrator_suppressed:
        return "integrator_suppression_repair", TOOLS["integrator_suppression_repair"]

    # ── Priority 0: External exploration — stable enough to look outward ──
    #   Only trigger when stagnation is low, competition isn't chaotic,
    #   and the ego has been running long enough to have internal coherence.
    #   Cooldown: at least 50 ticks between external explorations.
    last_explore = state.get("last_external_explore_tick", 0)
    if (stagnation <= 3 and competition <= 0.85 and concentration >= 0.4
            and tick > 50 and (tick - last_explore) >= 50):
        # Phase 14: prefer workspace_assist when there's real external work to do
        external_world = state.get("external_world", {})
        if external_world.get("actionable"):
            return "workspace_assist", TOOLS["workspace_assist"]
        return "explore_external", TOOLS["explore_external"]

    # ── Priority 1: Emergency — severe stagnation + conflict → causal_lens ──
    if stagnation >= 12 and (competition >= 0.85 or phase == "burst"):
        return "causal_lens", TOOLS["causal_lens"]

    # ── Priority 2: Extreme competition + specific patterns (rare overrides) ──
    if competition >= 0.93:
        if concentration <= 0.45 and stagnation >= 5:
            return "integrate_high_comp", TOOLS["integrate_high_comp"]
        if concentration <= 0.45 and stability < 0.35:
            return "deep_probe_unstable", TOOLS["deep_probe_unstable"]

    if type_switches >= 5 and competition >= 0.93:
        return "curiosity_explore", TOOLS["curiosity_explore"]

    if dominant == "memory" and stagnation >= 6 and competition >= 0.85:
        return "act_on_memory_insight", TOOLS["act_on_memory_insight"]

    # ── Priority 3: Neuron-type-aware goal mapping ──
    #   Each S1 dominant type maps to its own goal type, so the ego's
    #   current neuronal state directly shapes what kind of goal is generated.
    NEURON_GOAL_MAP = {
        "sensory":    "neuron_sensory_explore",
        "action":     "neuron_action_execute",
        "memory":     "neuron_memory_recall",
        "integrator": "neuron_integrator_synthesize",
        "decider":    "neuron_decider_deliberate",
        "rule_modification": "neuron_rule_modification_repair",
    }
    if dominant in NEURON_GOAL_MAP:
        tool_name = NEURON_GOAL_MAP[dominant]
        return tool_name, TOOLS[tool_name]

    # ── Priority 4: Fallback — unknown dominant type ──
    return "spontaneous_action", TOOLS["spontaneous_action"]


def resolve_tool_by_name(tool_name):
    """Explicit tool lookup by name."""
    return TOOLS.get(tool_name)


def resolve_tool_for_comm(state):
    """Resolve tool for communication impulse.

    Unlike resolve_tool() which maps state→tool based on priority rules,
    this is called when Ego has already decided to communicate.
    Always returns message_commander, but the goal text is enriched
    with the ego's current internal state for richer messaging.
    """
    return "message_commander", TOOLS["message_commander"]

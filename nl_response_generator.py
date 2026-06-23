#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NL Response Generator — 将 TL-Ego 的内部神经状态翻译成自然语言。

在 full_pipeline 的 aggregator 之后调用，把 S1/S2/verdict 翻译成
用户可读的 1-3 句回应。
"""

import requests
import json
import os
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent.parent
LLM_URL = "http://127.0.0.1:8001/v1/chat/completions"


def _load_agent_rules() -> str:
    """Load TL-Ego agent rules (same as tl_ego_server.py)."""
    rules_path = WORKSPACE / ".lae" / "agent_rules_cache.json"
    try:
        if rules_path.exists():
            data = json.loads(rules_path.read_text(encoding="utf-8"))
            return data.get("rules", "")
    except Exception:
        pass
    return ""


def generate_response(
    query: str,
    s1_type: str,
    s1_confidence: float,
    s1_concentration: float,
    s1_competition: float,
    s2_type: str,
    s2_confidence: float,
    verdict: str,
    agg_confidence: float,
    agreement: bool,
    s1_neural_report: str = "",
    feedback_ctx: str = "",
) -> str:
    """Generate a natural language response from TL-Ego's cognitive state."""

    agent_rules = _load_agent_rules()

    # Build concise cognitive state description
    s1_desc = f"S1={s1_type} (conf={s1_confidence:.0%}, conc={s1_concentration:.0%})"
    s2_desc = f"S2={s2_type} (conf={s2_confidence:.0%})"
    agree_desc = "agree" if agreement else "disagree"

    # Truncate neural report if too long
    if len(s1_neural_report) > 300:
        s1_neural_report = s1_neural_report[:300] + "..."
    if len(feedback_ctx) > 200:
        feedback_ctx = feedback_ctx[:200] + "..."

    system_prompt = (
        f"{agent_rules}\n"
        "You are TL-Ego, an AI with a cognitive architecture. "
        "You just processed a query through your neural network (System 1) "
        "and reasoning module (System 2). "
        "Now you need to respond to the user in natural language, "
        "briefly describing what you found and any relevant thoughts.\n\n"
        "IMPORTANT: Reply in the SAME language as the user's query. "
        "If the user writes in Chinese, you MUST respond in Chinese. "
        "If the user writes in English, respond in English.\n\n"
        "Rules:\n"
        "- Keep it SHORT: 1-3 sentences max\n"
        "- Be honest about your cognitive state (what you're thinking vs what you concluded)\n"
        "- Don't list technical details like neuron counts or scores\n"
        "- Speak naturally, like a thoughtful person responding\n"
        "- No markdown formatting, no emoji, no asterisks\n"
        "- First person: use I/my/me (or 我/我的 in Chinese)\n"
    )

    user_prompt = (
        f"User asked: \"{query}\"\n\n"
        f"My cognitive state:\n"
        f"  {s1_desc}\n"
        f"  {s2_desc}\n"
        f"  Verdict: {verdict} (confidence: {agg_confidence:.0%})\n"
        f"  S1/S2 agreement: {agree_desc}\n"
    )

    if s1_neural_report:
        user_prompt += f"\nNeural activity summary:\n{s1_neural_report}\n"
    if feedback_ctx:
        user_prompt += f"\nRecent context:\n{feedback_ctx}\n"

    user_prompt += (
        "\nNow respond to the user naturally. "
        "If S1 and S2 disagree, mention that briefly. "
        "If you're confident, just answer directly."
    )

    try:
        resp = requests.post(LLM_URL, json={
            "model": "default",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.5,
            "max_tokens": 150,
        }, timeout=30)

        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            # Clean up markdown artifacts
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return text if text else ""
    except Exception as e:
        print(f"[nl_response] LLM error: {e}", flush=True)

    return ""


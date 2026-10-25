"""Conversational layer over the agent swarm (no external AI service).

* Greetings, thanks, "help" and other small talk get an immediate reply without running agents.
* Short follow-ups ("and tomorrow?", "what about traffic") are read in the context of the previous question.
* Agent runs end with an *answer* that leads with the agents that matched the question, plus follow-up suggestions.

Everything is deterministic and runs on the server; the message is only ever used as data for keyword matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from swarmos.agents.planner import score_agents
from swarmos.agents.registry import REGISTRY

GREETING = re.compile(r"^(hi+|hello+|hey+|hii+|namaste|namaskar|hola|yo|good (morning|afternoon|evening|night)|greetings)\b", re.I)
THANKS = re.compile(r"^(thanks?|thank you|thx|ty|great|awesome|nice|cool|ok(ay)?|got it|perfect|super)\b", re.I)
BYE = re.compile(r"^(bye|goodbye|see you|that'?s all|done)\b", re.I)
HELP = re.compile(r"(what can you do|who are you|what are you|how do(es)? (this|it|you) work|^help\b|capabilit|what do you know|how to use)", re.I)
FOLLOW_UP = re.compile(r"^(and|what about|how about|also|then|same for|tomorrow|today|now|next)\b", re.I)

DOMAIN_SUGGESTIONS: dict[str, list[str]] = {
    "environment": ["Will it rain in the next 24 hours?", "How is air quality today?", "Is there a heat risk this week?"],
    "mobility": ["Which roads are most congested right now?", "What will traffic be like tomorrow morning?"],
    "water": ["Are there any suspected water leaks?", "Which drains need attention before heavy rain?"],
    "energy": ["What is the expected peak power demand?", "Any grid anomalies or outages?"],
    "safety": ["Which open incidents need dispatch?", "Are there any official disaster alerts?"],
    "civic": ["What are citizens complaining about most?", "Which complaints are overdue?"],
    "planning": ["Which assets need maintenance first?", "How fast is the city growing?"],
    "health": ["How many hospitals and clinics are there?", "Which areas are underserved by public services?"],
    "governance": ["How is the budget being used?", "What do our SOPs say about heavy rain?"],
    "cross": ["Give me a city situation overview", "What if 80 mm of extra rain falls tomorrow?"],
}


@dataclass
class ChatTurn:
    kind: str  # "reply" (no agents) or "task" (run agents for `prompt`)
    text: str = ""
    prompt: str = ""
    suggestions: list[str] = field(default_factory=list)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)


def scenario_params(text: str) -> dict[str, dict[str, Any]]:
    """'What if 80 mm of rain…' / '…and 2 °C hotter' → parameters for the scenario simulation agent."""
    params: dict[str, Any] = {}
    rain = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:mm|millimet)", text, re.I)
    if rain:
        params["extra_rainfall_mm"] = min(500.0, float(rain.group(1)))
    temp = re.search(r"([+-]?\d{1,2}(?:\.\d+)?)\s*(?:°\s*c?|deg(?:ree)?s?\s*c?\b)\s*(hotter|warmer|cooler|colder)?", text, re.I)
    if temp:
        delta = float(temp.group(1))
        if (temp.group(2) or "").lower() in ("cooler", "colder"):
            delta = -abs(delta)
        params["temperature_delta_c"] = max(-10.0, min(10.0, delta))
        params.setdefault("extra_rainfall_mm", 0.0)  # a heat-only scenario adds no rain
    return {"scenario_simulation_agent": params} if params else {}


def _clean(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()


def interpret(message: str, city_name: str, history: list[dict[str, str]] | None = None) -> ChatTurn:
    """Decide whether a message is small talk (answered directly) or a question for the agents."""
    msg = _clean(message)
    words = msg.split()
    has_topic = bool(score_agents(msg))
    if not has_topic and len(words) <= 6:
        if HELP.search(msg):
            return ChatTurn("reply", text=(
                f"I'm the SwarmOS assistant for {city_name}. Ask me anything about the city's operations and I'll run "
                "the right specialist agents (50 of them: weather, air quality, flooding, traffic, water, power, "
                "incidents, complaints, health facilities, budgets and more), then answer with the evidence and its "
                "sources. I only analyse and recommend — nothing is executed without a person approving it."),
                suggestions=["Give me a city situation overview", "Will it rain in the next 24 hours?",
                             "Which open incidents need dispatch?", "What are citizens complaining about most?"])
        if GREETING.match(msg):
            return ChatTurn("reply", text=f"Hello! I can check what's happening in {city_name} right now — weather, air "
                                          "quality, traffic, incidents, utilities and more. What would you like to know?",
                            suggestions=["Give me a city situation overview", "How is air quality today?",
                                         "Are there any official disaster alerts?"])
        if THANKS.match(msg):
            return ChatTurn("reply", text="You're welcome! Anything else you'd like me to check?",
                            suggestions=["Give me a city situation overview", "Which roads are most congested right now?"])
        if BYE.match(msg):
            return ChatTurn("reply", text="Goodbye! Your conversation stays here if you need it later.")
    prompt = msg
    if not has_topic and history:
        # Short follow-up such as "and tomorrow?" — combine it with the last question that had a topic.
        previous = next((h.get("text", "") for h in reversed(history)
                         if h.get("role") == "user" and score_agents(h.get("text", ""))), "")
        if previous and (FOLLOW_UP.match(msg) or len(words) <= 5):
            prompt = f"{previous} {msg}"
    if len(prompt) < 3:
        return ChatTurn("reply", text="Could you tell me a bit more about what you'd like to know?",
                        suggestions=["Give me a city situation overview"])
    return ChatTurn("task", prompt=prompt[:2000], params=scenario_params(prompt))


def primary_agents(plan: dict[str, Any]) -> list[str]:
    """Agents chosen because they match the question (in planner rank order), not dependencies or wrap-up agents."""
    out = [a["id"] for a in plan.get("agents", [])
           if str(a.get("reason", "")).startswith(("Matched request terms", "Explicitly requested", "No specific domain"))]
    return out or [a["id"] for a in plan.get("agents", []) if a["id"] not in ("city_operations_planning_agent", "compliance_checklist_agent")]


def compose_answer(plan: dict[str, Any], results: dict[str, Any], statuses: dict[str, str], prompt: str = "") -> tuple[str, list[str]]:
    """A conversational answer that addresses the question first, then caveats, plus follow-up suggestions."""
    primary = [a for a in primary_agents(plan) if a in results and statuses.get(a) in ("succeeded", "degraded")]
    if not primary:
        missing = sorted({m for r in results.values() for m in (r.missing or [])})[:3]
        text = "I couldn't get a usable answer from the agents for this question."
        if missing:
            text += " Missing data: " + "; ".join(missing) + "."
        return text, ["Give me a city situation overview"]
    lead = [results[a].summary.rstrip(".") + "." for a in primary[:3]]
    extra_findings = []
    seen = set()
    for a in primary:
        for f in sorted(results[a].findings, key=lambda f: ["info", "low", "moderate", "high", "critical"].index(f.severity),
                        reverse=True):
            if f.title not in seen and f.severity in ("moderate", "high", "critical"):
                seen.add(f.title)
                extra_findings.append(f"{f.title} — {f.detail}".rstrip("."))
    text = " ".join(lead)
    if extra_findings:
        text += "\n\nWorth noting:\n" + "\n".join(f"• {x}." for x in extra_findings[:3])
    provenance = set().union(*(set(results[a].provenance) for a in primary))
    if {"demo", "synthetic"} & provenance:
        text += "\n\nPart of this is based on simulated data where no live feed exists for the city."
    elif "live" in provenance:
        text += "\n\nBased on live data."
    if len(primary) > 3:
        text += f" ({len(primary) - 3} more agent result(s) are listed below.)"
    domains = []
    for a in primary:
        d = REGISTRY[a].domain if a in REGISTRY else "cross"
        if d not in domains:
            domains.append(d)
    suggestions: list[str] = []
    for d in [*domains, "cross"]:
        for q in DOMAIN_SUGGESTIONS.get(d, []):
            if q not in suggestions and q.lower().rstrip("?") != prompt.lower().strip().rstrip("?"):
                suggestions.append(q)
    return text, suggestions[:4]

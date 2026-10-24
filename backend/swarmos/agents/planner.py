"""Deterministic, explainable planner: request → selected agents → dependency DAG → execution stages.

The prompt is treated purely as data for keyword matching. Nothing in it can change permissions, add tools,
or alter the plan beyond selecting registered agents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from swarmos.agents.contracts import DOMAINS
from swarmos.agents.registry import REGISTRY
from swarmos.core.security import ROLE_RANK

FINAL_STAGE = ("city_operations_planning_agent", "compliance_checklist_agent")
DEFAULT_ENTRY = "city_situation_summarizer"


class PlanningError(ValueError):
    pass


@dataclass
class Plan:
    agents: list[str]
    reasons: dict[str, str]
    edges: list[tuple[str, str]]  # (from, to): `to` depends on `from`
    stages: list[list[str]]
    excluded: dict[str, str] = field(default_factory=dict)

    def deps_of(self, agent_id: str) -> list[str]:
        return [a for a, b in self.edges if b == agent_id]

    def as_dict(self) -> dict[str, Any]:
        return {
            "agents": [
                {"id": a, "name": REGISTRY[a].name, "domain": REGISTRY[a].domain, "reason": self.reasons[a],
                 "depends_on": self.deps_of(a)}
                for a in self.agents
            ],
            "edges": [{"from": a, "to": b} for a, b in self.edges],
            "stages": self.stages,
            "excluded": self.excluded,
        }


def _normalise(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9.\s]", " ", text.lower()) + " "


def _matches(keyword: str, text: str) -> bool:
    """Whole-word match allowing common English inflections (leak → leaks, flood → flooding)."""
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?:s|es|ing|ed|y)?(?![a-z0-9])", text) is not None


def score_agents(prompt: str) -> dict[str, list[str]]:
    text = _normalise(prompt)
    hits: dict[str, list[str]] = {}
    for spec in REGISTRY.values():
        matched = [k for k in spec.keywords if _matches(k, text)]
        if matched:
            hits[spec.id] = matched
    return hits


def build_plan(prompt: str, *, role: str, agent_ids: list[str] | None = None, domains: list[str] | None = None,
               max_agents: int = 14) -> Plan:
    reasons: dict[str, str] = {}
    excluded: dict[str, str] = {}
    selected: list[str] = []

    if agent_ids:
        for a in agent_ids:
            if a not in REGISTRY:
                raise PlanningError(f"Unknown agent '{a}'")
            if a in FINAL_STAGE or a in selected:
                continue  # always appended as the final stage
            selected.append(a)
            reasons[a] = "Explicitly requested by the operator"
    if domains:
        for d in domains:
            if d not in DOMAINS:
                raise PlanningError(f"Unknown domain '{d}'")
            for spec in REGISTRY.values():
                if spec.domain == d and spec.id not in selected and not spec.aggregate:
                    selected.append(spec.id)
                    reasons[spec.id] = f"Member of requested domain '{DOMAINS[d]}'"
    if not selected and not agent_ids:
        hits = score_agents(prompt)
        ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for a, kws in ranked:
            if a in FINAL_STAGE:
                continue
            selected.append(a)
            reasons[a] = "Matched request terms: " + ", ".join(f"“{k}”" for k in kws[:4])
    if not selected and not agent_ids:
        selected.append(DEFAULT_ENTRY)
        reasons[DEFAULT_ENTRY] = "No specific domain matched; producing a city-wide situation overview"

    # Transitive explicit dependencies
    queue = list(selected)
    while queue:
        cur = queue.pop()
        for dep in REGISTRY[cur].depends_on:
            if dep in FINAL_STAGE:
                continue
            if dep not in selected:
                selected.append(dep)
                reasons[dep] = f"Required input for {REGISTRY[cur].name}"
                queue.append(dep)

    # Role filter (server-side permission enforcement)
    allowed = []
    for a in selected:
        if ROLE_RANK[role] < ROLE_RANK[REGISTRY[a].min_role]:
            excluded[a] = f"Requires role '{REGISTRY[a].min_role}'"
        else:
            allowed.append(a)
    selected = allowed

    core_cap = max(1, max_agents - len(FINAL_STAGE))
    if len(selected) > core_cap:
        # keep explicitly-matched agents first, then their dependencies, preserving dependency closure
        keep: list[str] = []
        for a in selected:
            closure = _closure(a, set(selected))
            if len(set(keep) | closure) <= core_cap:
                for c in closure:
                    if c not in keep:
                        keep.append(c)
        for a in selected:
            if a not in keep:
                excluded[a] = f"Plan capped at {max_agents} agents"
        selected = keep

    for a in FINAL_STAGE:
        selected.append(a)
        reasons[a] = ("Consolidates all proposed actions into a plan" if a == FINAL_STAGE[0]
                      else "Reviews every proposed action against the approval checklist")

    sel = set(selected)
    edges: set[tuple[str, str]] = set()
    for a in selected:
        spec = REGISTRY[a]
        for d in spec.depends_on:
            if d in sel:
                edges.add((d, a))
        if spec.aggregate:
            for other in selected:
                if other != a and not REGISTRY[other].aggregate:
                    edges.add((other, a))
    return Plan(agents=selected, reasons=reasons, edges=sorted(edges), stages=_stages(selected, edges), excluded=excluded)


def _closure(a: str, within: set[str]) -> set[str]:
    out = {a}
    stack = [a]
    while stack:
        for d in REGISTRY[stack.pop()].depends_on:
            if d in within and d not in out:
                out.add(d)
                stack.append(d)
    return out


def _stages(agents: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    indeg = {a: 0 for a in agents}
    for _, b in edges:
        indeg[b] += 1
    stages = []
    remaining = set(agents)
    while remaining:
        ready = sorted(a for a in remaining if indeg[a] == 0)
        if not ready:
            raise PlanningError("Dependency cycle in plan")
        stages.append(ready)
        for a in ready:
            remaining.discard(a)
            for x, y in edges:
                if x == a:
                    indeg[y] -= 1
    return stages

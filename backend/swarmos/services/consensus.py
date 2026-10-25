"""Deterministic comparison of overlapping persisted agent signals.

This is a review aid, not a vote or calibrated estimate of truth.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from swarmos.models import AgentExecution

SEVERITY_RANK = {"info": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}


def _numeric_metrics(value: Any, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            key = str(key).lower()
            if key in {"baseline", "scenario", "delta"}:
                continue
            out.update(_numeric_metrics(item, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out[prefix] = float(value)
    return out


def compare_executions(executions: list[AgentExecution]) -> dict[str, Any]:
    """Compare exact shared metric paths and exact normalized finding titles only."""
    metrics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    findings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for execution in executions:
        if execution.status not in ("succeeded", "degraded") or not execution.output:
            continue
        for key, value in _numeric_metrics(execution.output.get("metrics") or {}).items():
            metrics[key].append({"agent_id": execution.agent_id, "value": round(value, 6)})
        for finding in execution.output.get("findings") or []:
            title = str(finding.get("title") or "").strip()
            normalized = re.sub(r"\W+", " ", title.lower()).strip()
            if normalized:
                findings[normalized].append({"agent_id": execution.agent_id, "title": title,
                                             "severity": str(finding.get("severity") or "info")})

    comparisons: list[dict[str, Any]] = []
    for key, values in sorted(metrics.items()):
        if len(values) < 2:
            continue
        numbers = [item["value"] for item in values]
        spread = max(numbers) - min(numbers)
        scale = max((abs(item) for item in numbers), default=0)
        material = spread > max(0.25, scale * 0.2)
        comparisons.append({"kind": "metric", "signal": key, "values": values,
                            "spread": round(spread, 6), "material_difference": material})
    for normalized, values in sorted(findings.items()):
        if len(values) < 2:
            continue
        ranks = {SEVERITY_RANK.get(item["severity"], 0) for item in values}
        comparisons.append({"kind": "finding", "signal": normalized, "values": values,
                            "material_difference": len(ranks) > 1})

    material = sum(1 for item in comparisons if item["material_difference"])
    return {"comparisons": comparisons, "shared_signals": len(comparisons),
            "material_differences": material,
            "status": "differences_found" if material else "no_material_difference_detected" if comparisons else "insufficient_overlap",
            "limitations": ["Only exact shared metric paths and exactly matching finding titles are compared.",
                            "A difference is a review flag, not proof that one agent is correct.",
                            "No shared signals means insufficient overlap, not agreement.",
                            "Agent confidence values are not calibrated and are not averaged."]}

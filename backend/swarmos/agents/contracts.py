"""Typed agent contracts: specs, inputs, results. All agent-to-agent payloads are validated models."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from swarmos.ai.runtime import ModelRuntime
    from swarmos.models import City
    from swarmos.services.data import CityData

DOMAINS: dict[str, str] = {
    "mobility": "Mobility & traffic",
    "environment": "Environment & climate",
    "water": "Water & sanitation",
    "energy": "Energy & utilities",
    "safety": "Public safety & emergency response",
    "civic": "Civic services",
    "planning": "Urban planning & infrastructure",
    "health": "Public health & community services",
    "governance": "Governance & operations",
    "cross": "Cross-domain intelligence",
}

ResultStatus = Literal["ok", "degraded", "unavailable", "failed"]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=200)
    detail: str = Field(default="", max_length=2000)
    severity: Literal["info", "low", "moderate", "high", "critical"] = "info"
    location: dict[str, float] | None = None


class ActionDraft(BaseModel):
    """A recommendation. It is never executed by an agent — only by a human approval through the API."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["create_incident", "escalate_incident", "notify_operators", "manual"]
    title: str = Field(max_length=240)
    rationale: str = Field(default="", max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)
    risk: Literal["low", "moderate", "high"] = "low"
    required_role: Literal["operator", "admin"] = "operator"

    def fingerprint(self) -> str:
        return f"{self.kind}:{self.title.strip().lower()}"


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ResultStatus
    summary: str = Field(max_length=4000)
    findings: list[Finding] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    actions: list[ActionDraft] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    method: str = "rules"
    data_refs: list[dict[str, Any]] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _honesty(self) -> AgentResult:
        if self.status == "unavailable":
            if not self.missing:
                raise ValueError("An unavailable result must state what is missing")
            if self.confidence is not None:
                raise ValueError("An unavailable result cannot carry a confidence score")
            if self.actions:
                raise ValueError("An unavailable result cannot propose actions")
        if self.status in ("ok", "degraded") and self.confidence is None:
            raise ValueError("ok/degraded results must report confidence")
        self.provenance = sorted(set(self.provenance))
        demo_note = "Includes SIMULATED data (climate-calibrated, generated where no live feed exists) — not for operational decisions."
        if ("demo" in self.provenance or "synthetic" in self.provenance) and demo_note not in self.limitations:
            self.limitations.append(demo_note)
        return self


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=2000)


class ScenarioParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra_rainfall_mm: float = Field(default=50.0, ge=0, le=500)
    temperature_delta_c: float = Field(default=0.0, ge=-10, le=10)


SkillFn = Callable[["AgentContext"], AgentResult]


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    domain: str
    description: str
    capabilities: tuple[str, ...]
    keywords: tuple[str, ...]
    skill: SkillFn
    data: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    aggregate: bool = False  # depends on every other agent selected in the plan
    params: type[BaseModel] = NoParams
    output_metrics: tuple[str, ...] = ()
    model: str | None = None
    min_role: Literal["viewer", "operator", "admin"] = "viewer"
    timeout_s: float | None = None
    max_retries: int = 1
    config: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "domain": self.domain,
            "domain_label": DOMAINS[self.domain],
            "description": self.description,
            "capabilities": list(self.capabilities),
            "keywords": list(self.keywords),
            "data_requirements": list(self.data),
            "depends_on": list(self.depends_on),
            "aggregate": self.aggregate,
            "model": self.model,
            "min_role": self.min_role,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "input_schema": self.params.model_json_schema(),
            "output_schema": {"$ref": "AgentResult", "required_metrics": list(self.output_metrics)},
        }


class Cancelled(Exception):
    pass


class TransientAgentError(Exception):
    """Raise for retryable failures (e.g. a locked database)."""


@dataclass
class AgentContext:
    spec: AgentSpec
    city: City
    data: CityData
    params: BaseModel
    prompt: str
    upstream: dict[str, AgentResult]
    models: ModelRuntime
    now: datetime
    task_id: str | None = None
    clearance: set[str] = field(default_factory=lambda: {"public"})
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def up(self, agent_id: str) -> AgentResult | None:
        r = self.upstream.get(agent_id)
        return r if r is not None and r.status in ("ok", "degraded") else None

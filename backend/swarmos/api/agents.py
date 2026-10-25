"""Agent registry, AI tasks (plan / submit / inspect / cancel) and proposed-action approvals."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swarmos.agents.chat import interpret
from swarmos.agents.contracts import DOMAINS
from swarmos.agents.registry import AGENTS, REGISTRY
from swarmos.api.common import PageParams, iso, page_params, paginate
from swarmos.api.outcomes import outcome_dict
from swarmos.config import get_settings
from swarmos.core.errors import bad_request, not_found
from swarmos.core.ratelimit import limiter
from swarmos.core.security import Principal, city_access, current_principal, require_role
from swarmos.db import get_db, utcnow
from swarmos.jobs.dispatch import dispatch
from swarmos.models import (
    AgentExecution,
    AgentHandoff,
    AgentTask,
    City,
    DataSource,
    GeoFeature,
    Incident,
    InterventionOutcome,
    Job,
    Measurement,
    ProposedAction,
    ScenarioRun,
    ServiceRequest,
    new_id,
)
from swarmos.services.consensus import compare_executions
from swarmos.services.tasks import cancel_task, decide_action, preview_plan, submit_task

router = APIRouter(tags=["agents"])


class ScenarioIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    extra_rainfall_mm: float = Field(default=50, ge=0, le=500)
    temperature_delta_c: float = Field(default=0, ge=-10, le=10)


def scenario_dict(row: ScenarioRun) -> dict[str, Any]:
    task = row.task
    execution = next((e for e in task.executions if e.agent_id == "scenario_simulation_agent"), None)
    result = execution.output if execution else None
    return {"id": row.id, "city_id": row.city_id, "name": row.name, "params": row.params,
            "task_id": row.task_id, "status": task.status, "summary": result.get("summary") if result else task.error,
            "result": result, "created_at": iso(row.created_at), "started_at": iso(task.started_at),
            "finished_at": iso(task.finished_at), "error": task.error}


@router.get("/cities/{city_id}/scenarios")
def list_scenarios(page: PageParams = Depends(page_params), city: City = Depends(city_access),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(ScenarioRun).where(ScenarioRun.city_id == city.id).order_by(ScenarioRun.created_at.desc())
    return paginate(db, stmt, page, scenario_dict)


@router.post("/cities/{city_id}/scenarios", status_code=202)
def create_scenario(body: ScenarioIn, background_tasks: BackgroundTasks,
                    city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    limiter.check(f"task:{principal.id}", get_settings().task_rate_limit_per_minute)
    name = body.name.strip()
    if len(name) < 3:
        raise bad_request("Scenario name must contain at least 3 non-space characters")
    scenario_id = new_id()
    params = {"scenario_simulation_agent": {"extra_rainfall_mm": body.extra_rainfall_mm,
                                             "temperature_delta_c": body.temperature_delta_c}}
    task, _created = submit_task(db, principal, city.id,
                               f"Saved what-if scenario: {name}", params=params,
                               agent_ids=["scenario_simulation_agent"], domains=None,
                               idempotency_key=f"scenario:{scenario_id}")
    row = ScenarioRun(id=scenario_id, city_id=city.id, task_id=task.id, created_by=principal.id,
                      name=name, params=params["scenario_simulation_agent"])
    db.add(row)
    db.flush()
    dispatch(background_tasks, db, db.scalar(select(Job).where(Job.dedupe_key == f"task:{task.id}")))
    return scenario_dict(row)


def _agent_stats(db: Session, days: int = 7) -> dict[str, dict[str, Any]]:
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(AgentExecution.agent_id, AgentExecution.status, func.count(AgentExecution.id), func.avg(AgentExecution.duration_ms),
               func.max(AgentExecution.finished_at))
        .where(AgentExecution.started_at >= since).group_by(AgentExecution.agent_id, AgentExecution.status)
    ).all()
    stats: dict[str, dict[str, Any]] = {}
    for agent_id, status, n, avg_ms, last in rows:
        s = stats.setdefault(agent_id, {"runs": 0, "by_status": {}, "avg_ms": None, "last_run_at": None})
        s["runs"] += n
        s["by_status"][status] = n
        if avg_ms is not None:
            s["avg_ms"] = round(avg_ms) if s["avg_ms"] is None else round((s["avg_ms"] + avg_ms) / 2)
        if last and (s["last_run_at"] is None or iso(last) > s["last_run_at"]):
            s["last_run_at"] = iso(last)
            s["last_status"] = status
    for s in stats.values():
        ok = s["by_status"].get("succeeded", 0) + s["by_status"].get("degraded", 0)
        s["useful_rate"] = round(ok / s["runs"], 3) if s["runs"] else None
        # Health follows the most recent run (older problems stay visible in by_status / useful_rate).
        last_status = s.get("last_status")
        s["health"] = ("failing" if last_status in ("failed", "timed_out")
                       else "degraded" if last_status in ("unavailable", "degraded") else "healthy")
    return stats


@router.get("/agents")
def list_agents(domain: str | None = Query(None, max_length=40), q: str | None = Query(None, max_length=80),
                principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    stats = _agent_stats(db)
    items = []
    for a in AGENTS:
        if domain and a.domain != domain:
            continue
        if q and q.lower() not in f"{a.name} {a.description} {' '.join(a.keywords)}".lower():
            continue
        items.append({**a.describe(), "stats_7d": stats.get(a.id, {"runs": 0, "health": "idle"})})
    return {"items": items, "total": len(items), "domains": DOMAINS, "registry_size": len(AGENTS)}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    spec = REGISTRY.get(agent_id)
    if spec is None:
        raise not_found("Agent")
    recent = db.execute(
        select(AgentExecution, AgentTask.city_id).join(AgentTask, AgentTask.id == AgentExecution.task_id)
        .where(AgentExecution.agent_id == agent_id).order_by(AgentExecution.id.desc()).limit(50)
    ).all()
    visible = [(e, c) for e, c in recent if principal.can_access_city(c)][:15]
    dependents = [a.id for a in AGENTS if agent_id in a.depends_on]
    return {**spec.describe(), "dependents": dependents, "stats_7d": _agent_stats(db).get(agent_id, {"runs": 0, "health": "idle"}),
            "recent_executions": [{"task_id": e.task_id, "city_id": c, "status": e.status, "duration_ms": e.duration_ms,
                                   "confidence": e.confidence, "finished_at": iso(e.finished_at), "error": e.error} for e, c in visible]}


class TaskIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    agent_ids: list[str] | None = Field(default=None, max_length=50)
    domains: list[str] | None = Field(default=None, max_length=10)
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")


def task_dict(t: AgentTask, detail: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {"id": t.id, "city_id": t.city_id, "prompt": t.prompt, "status": t.status, "created_at": iso(t.created_at),
                         "started_at": iso(t.started_at), "finished_at": iso(t.finished_at), "cancel_requested": t.cancel_requested,
                         "agent_count": len(t.plan.get("agents", [])), "error": t.error,
                         "summary": (t.result or {}).get("summary") if t.result else None}
    if detail:
        d["plan"] = t.plan
        d["params"] = t.params
        d["result"] = t.result
        d["executions"] = [{"agent_id": e.agent_id, "name": REGISTRY[e.agent_id].name if e.agent_id in REGISTRY else e.agent_id,
                            "status": e.status, "attempts": e.attempts, "depends_on": e.depends_on, "reason": e.reason,
                            "input": e.input, "output": e.output, "error": e.error, "confidence": e.confidence,
                            "started_at": iso(e.started_at), "finished_at": iso(e.finished_at), "duration_ms": e.duration_ms}
                           for e in t.executions]
    return d


@router.post("/cities/{city_id}/tasks/plan")
def plan_task(body: TaskIn, city: City = Depends(city_access), principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return preview_plan(principal, body.prompt, body.agent_ids, body.domains)


@router.post("/cities/{city_id}/tasks", status_code=202)
def create_task(body: TaskIn, response: Response, request: Request, background_tasks: BackgroundTasks, city: City = Depends(city_access),
                principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    limiter.check(f"task:{principal.id}", get_settings().task_rate_limit_per_minute)
    task, created = submit_task(db, principal, city.id, body.prompt, params=body.params, agent_ids=body.agent_ids,
                                domains=body.domains, idempotency_key=body.idempotency_key)
    if not created:
        response.status_code = 200
    dispatch(background_tasks, db, db.scalar(select(Job).where(Job.dedupe_key == f"task:{task.id}")))
    return task_dict(task, detail=True)


class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    text: str = Field(max_length=4000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")


@router.post("/cities/{city_id}/chat")
def chat(body: ChatIn, request: Request, background_tasks: BackgroundTasks, city: City = Depends(city_access),
         principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    """One chat message: small talk is answered directly; questions start an agent run whose result carries
    a conversational `answer` and follow-up `suggestions`."""
    turn = interpret(body.message, city.name, [h.model_dump() for h in body.history])
    if turn.kind == "reply":
        return {"type": "reply", "text": turn.text, "suggestions": turn.suggestions}
    limiter.check(f"task:{principal.id}", get_settings().task_rate_limit_per_minute)
    task, _ = submit_task(db, principal, city.id, turn.prompt, params={**turn.params, **body.params}, agent_ids=None, domains=None,
                          idempotency_key=body.idempotency_key)
    dispatch(background_tasks, db, db.scalar(select(Job).where(Job.dedupe_key == f"task:{task.id}")))
    return {"type": "task", "interpreted_as": turn.prompt, "task": task_dict(task, detail=True)}


@router.get("/cities/{city_id}/tasks")
def list_tasks(status: str | None = Query(None, max_length=20), page: PageParams = Depends(page_params), city: City = Depends(city_access),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(AgentTask).where(AgentTask.city_id == city.id)
    if status:
        stmt = stmt.where(AgentTask.status == status)
    return paginate(db, stmt.order_by(AgentTask.created_at.desc()), page, task_dict)


def _task(db: Session, city: City, task_id: str) -> AgentTask:
    t = db.get(AgentTask, task_id)
    if t is None or t.city_id != city.id:
        raise not_found("Task")
    return t


@router.get("/cities/{city_id}/tasks/{task_id}")
def get_task(task_id: str, city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    t = _task(db, city, task_id)
    handoffs = db.scalars(select(AgentHandoff).where(AgentHandoff.task_id == t.id).order_by(AgentHandoff.id))
    actions = list(db.scalars(select(ProposedAction).where(ProposedAction.task_id == t.id).order_by(ProposedAction.created_at)))
    outcomes = db.scalars(select(InterventionOutcome).where(
        InterventionOutcome.action_id.in_([action.id for action in actions])
    )).all() if actions else []
    outcome_by_action = {row.action_id: row for row in outcomes}
    return {**task_dict(t, detail=True),
            "consensus": compare_executions(t.executions),
            "handoffs": [{"from": h.from_agent, "to": h.to_agent, "digest": h.payload_digest[:16], "status": h.payload_status,
                          "at": iso(h.created_at)} for h in handoffs],
            "actions": [{**action_dict(a), "outcome": outcome_dict(outcome_by_action[a.id], db) if a.id in outcome_by_action else None}
                        for a in actions]}


@router.post("/cities/{city_id}/tasks/{task_id}/cancel")
def cancel(task_id: str, city: City = Depends(city_access), principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    return task_dict(cancel_task(db, principal, _task(db, city, task_id)))


def action_dict(a: ProposedAction) -> dict[str, Any]:
    return {"id": a.id, "task_id": a.task_id, "city_id": a.city_id, "agent_id": a.agent_id,
            "agent_name": REGISTRY[a.agent_id].name if a.agent_id in REGISTRY else a.agent_id, "kind": a.kind, "title": a.title,
            "rationale": a.rationale, "payload": a.payload, "risk": a.risk, "required_role": a.required_role, "status": a.status,
            "created_at": iso(a.created_at), "decided_by": a.decided_by, "decided_at": iso(a.decided_at), "decision_note": a.decision_note,
            "executed_at": iso(a.executed_at), "execution_result": a.execution_result}


@router.get("/cities/{city_id}/actions/{action_id}/evidence")
def get_action_evidence(action_id: str, city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Resolve bounded measurement and mapped-feature IDs attached to an action, within its city scope."""
    action = db.get(ProposedAction, action_id)
    if action is None or action.city_id != city.id:
        raise not_found("Action")
    raw = (action.payload or {}).get("_evidence") or {}
    refs = [dict(ref) for ref in raw.get("data_refs", []) if isinstance(ref, dict)]
    measurement_ids = list(dict.fromkeys(
        record_id for ref in refs for record_id in ref.get("record_ids", [])
        if isinstance(record_id, int) and not isinstance(record_id, bool) and record_id > 0
    ))[:240]
    feature_ids = list(dict.fromkeys(
        record_id for ref in refs for record_id in ref.get("feature_ids", [])
        if isinstance(record_id, int) and not isinstance(record_id, bool) and record_id > 0
    ))[:240]
    request_ids = list(dict.fromkeys(
        request_id for ref in refs for request_id in ref.get("request_ids", [])
        if isinstance(request_id, str) and 0 < len(request_id) <= 32
    ))[:240]
    incident_ids = list(dict.fromkeys(
        incident_id for ref in refs for incident_id in ref.get("incident_ids", [])
        if isinstance(incident_id, str) and 0 < len(incident_id) <= 32
    ))[:240]
    execution_ids = list(dict.fromkeys(
        execution_id for ref in refs for execution_id in ref.get("execution_ids", [])
        if isinstance(execution_id, int) and not isinstance(execution_id, bool) and execution_id > 0
    ))[:240]

    measurements: dict[int, dict[str, Any]] = {}
    if measurement_ids:
        rows = db.execute(
            select(Measurement, DataSource.name, DataSource.limitations)
            .join(DataSource, DataSource.id == Measurement.source_id)
            .where(Measurement.city_id == city.id, Measurement.id.in_(measurement_ids))
        ).all()
        measurements = {row.id: {
            "id": row.id, "metric": row.metric, "value": row.value, "unit": row.unit,
            "entity": row.entity, "kind": row.kind, "source_id": row.source_id, "source_name": source_name,
            "provenance": row.provenance, "observed_at": iso(row.observed_at), "fetched_at": iso(row.fetched_at),
            "limitations": limitations or [],
        } for row, source_name, limitations in rows}

    features: dict[int, dict[str, Any]] = {}
    if feature_ids:
        from swarmos.cities import haversine_km

        rows = db.execute(
            select(GeoFeature, DataSource.name, DataSource.limitations)
            .join(DataSource, DataSource.id == GeoFeature.source_id)
            .where(GeoFeature.id.in_(feature_ids), (GeoFeature.city_id == city.id) | GeoFeature.city_id.is_(None))
        ).all()
        for row, source_name, limitations in rows:
            if row.city_id is None and haversine_km(city.lat, city.lon, row.lat, row.lon) > 800:
                continue
            features[row.id] = {
                "id": row.id, "layer": row.layer, "name": row.name, "external_id": row.external_id,
                "lat": row.lat, "lon": row.lon, "source_id": row.source_id, "source_name": source_name,
                "provenance": row.provenance, "observed_at": iso(row.observed_at), "fetched_at": iso(row.fetched_at),
                "limitations": limitations or ["Mapped coverage may be incomplete."],
            }

    requests: dict[str, dict[str, Any]] = {}
    if request_ids:
        rows = db.scalars(select(ServiceRequest).where(
            ServiceRequest.city_id == city.id, ServiceRequest.id.in_(request_ids),
        )).all()
        requests = {row.id: {
            "id": row.id, "category": row.category, "department": row.department,
            "status": row.status, "provenance": row.provenance,
            "observed_at": iso(row.created_at), "resolved_at": iso(row.resolved_at),
            "limitations": ["Citizen request text, coordinates, ward and contact data are intentionally omitted."],
        } for row in rows}

    incidents: dict[str, dict[str, Any]] = {}
    if incident_ids:
        rows = db.scalars(select(Incident).where(Incident.city_id == city.id, Incident.id.in_(incident_ids))).all()
        incidents = {row.id: {
            "id": row.id, "category": row.category, "severity": row.severity, "status": row.status,
            "department": row.department, "source": row.source, "provenance": row.provenance,
            "observed_at": iso(row.reported_at), "resolved_at": iso(row.resolved_at),
            "limitations": ["Incident description and precise coordinates are omitted from this recommendation evidence view."],
        } for row in rows}

    executions: dict[int, dict[str, Any]] = {}
    if execution_ids:
        rows = db.execute(
            select(AgentExecution, AgentTask).join(AgentTask, AgentTask.id == AgentExecution.task_id)
            .where(AgentTask.city_id == city.id, AgentExecution.id.in_(execution_ids))
        ).all()
        executions = {execution.id: {
            "id": execution.id, "agent_id": execution.agent_id, "status": execution.status,
            "started_at": iso(execution.started_at), "finished_at": iso(execution.finished_at),
            "attempts": execution.attempts,
            "summary": str((execution.output or {}).get("summary") or execution.error or "")[:500],
            "limitations": list((execution.output or {}).get("limitations") or []),
        } for execution, _task in rows}

    for ref in refs:
        if ref.get("record_ids"):
            ref["records"] = [measurements[i] for i in ref["record_ids"] if i in measurements]
            ref["unresolved_ids"] = [i for i in ref["record_ids"] if i not in measurements]
        elif ref.get("feature_ids"):
            ref["records"] = [features[i] for i in ref["feature_ids"] if i in features]
            ref["unresolved_ids"] = [i for i in ref["feature_ids"] if i not in features]
        elif ref.get("request_ids"):
            ref["records"] = [requests[i] for i in ref["request_ids"] if i in requests]
            ref["unresolved_ids"] = [i for i in ref["request_ids"] if i not in requests]
        elif ref.get("incident_ids"):
            ref["records"] = [incidents[i] for i in ref["incident_ids"] if i in incidents]
            ref["unresolved_ids"] = [i for i in ref["incident_ids"] if i not in incidents]
        elif ref.get("execution_ids"):
            ref["records"] = [executions[i] for i in ref["execution_ids"] if i in executions]
            ref["unresolved_ids"] = [i for i in ref["execution_ids"] if i not in executions]
        else:
            ref["records"] = []
            ref["unresolved_ids"] = []
    return {"action_id": action.id, "references": refs,
            "limitations": ["References identify persisted source rows where stable IDs were recorded.",
                            "Resolved records retain source provenance and timestamps; they do not independently verify a recommendation.",
                            "Citizen request evidence is redacted to category, department, status and timestamps.",
                            "Incident evidence omits descriptions and precise coordinates."]}


@router.get("/cities/{city_id}/actions")
def list_actions(status: str | None = Query(None, max_length=20), page: PageParams = Depends(page_params), city: City = Depends(city_access),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(ProposedAction).where(ProposedAction.city_id == city.id)
    if status:
        stmt = stmt.where(ProposedAction.status == status)
    return paginate(db, stmt.order_by(ProposedAction.created_at.desc()), page, action_dict)


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)


@router.post("/cities/{city_id}/actions/{action_id}/decision")
def decide(action_id: str, body: DecisionIn, city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    a = db.get(ProposedAction, action_id)
    if a is None or a.city_id != city.id:
        raise not_found("Action")
    return action_dict(decide_action(db, principal, a, body.approve, body.note))

"""Agent registry, AI tasks (plan / submit / inspect / cancel) and proposed-action approvals."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swarmos.agents.contracts import DOMAINS
from swarmos.agents.registry import AGENTS, REGISTRY
from swarmos.api.common import PageParams, iso, page_params, paginate
from swarmos.config import get_settings
from swarmos.core.errors import not_found
from swarmos.core.ratelimit import limiter
from swarmos.core.security import Principal, city_access, current_principal, require_role
from swarmos.db import get_db, utcnow
from swarmos.jobs.dispatch import dispatch
from swarmos.models import AgentExecution, AgentHandoff, AgentTask, City, Job, ProposedAction
from swarmos.services.tasks import cancel_task, decide_action, preview_plan, submit_task

router = APIRouter(tags=["agents"])


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
    for s in stats.values():
        ok = s["by_status"].get("succeeded", 0) + s["by_status"].get("degraded", 0)
        s["useful_rate"] = round(ok / s["runs"], 3) if s["runs"] else None
        s["health"] = ("failing" if s["by_status"].get("failed", 0) + s["by_status"].get("timed_out", 0) > ok
                       else "degraded" if s["by_status"].get("unavailable", 0) + s["by_status"].get("degraded", 0) > 0 else "healthy")
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
    prompt: str = Field(min_length=3, max_length=2000)
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
    actions = db.scalars(select(ProposedAction).where(ProposedAction.task_id == t.id).order_by(ProposedAction.created_at))
    return {**task_dict(t, detail=True),
            "handoffs": [{"from": h.from_agent, "to": h.to_agent, "digest": h.payload_digest[:16], "status": h.payload_status,
                          "at": iso(h.created_at)} for h in handoffs],
            "actions": [action_dict(a) for a in actions]}


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

"""Agent task submission, cancellation and proposed-action approval workflows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from swarmos.agents.planner import PlanningError, build_plan
from swarmos.agents.registry import REGISTRY
from swarmos.config import get_settings
from swarmos.core.audit import audit
from swarmos.core.errors import ApiError, bad_request, conflict, forbidden, not_found
from swarmos.core.logging import correlation_id_var
from swarmos.core.security import ROLE_RANK, Principal
from swarmos.db import utcnow
from swarmos.jobs.queue import enqueue
from swarmos.models import (
    AgentExecution,
    AgentTask,
    Incident,
    IncidentEvent,
    Job,
    Notification,
    ProposedAction,
)


def preview_plan(principal: Principal, prompt: str, agent_ids: list[str] | None, domains: list[str] | None) -> dict[str, Any]:
    try:
        # A free-text request is capped; an explicit agent selection runs everything that was picked.
        cap = max(get_settings().task_max_agents, len(agent_ids) + 8) if agent_ids else get_settings().task_max_agents
        plan = build_plan(prompt, role=principal.role, agent_ids=agent_ids, domains=domains, max_agents=min(cap, 60))
    except PlanningError as exc:
        raise bad_request(str(exc)) from exc
    return plan.as_dict()


def submit_task(db: Session, principal: Principal, city_id: str, prompt: str, *, params: dict[str, Any],
                agent_ids: list[str] | None, domains: list[str] | None, idempotency_key: str | None) -> tuple[AgentTask, bool]:
    if idempotency_key:
        existing = db.scalar(select(AgentTask).where(AgentTask.created_by == principal.id,
                                                     AgentTask.idempotency_key == idempotency_key))
        if existing:
            if existing.city_id != city_id or existing.prompt != prompt:
                raise conflict("Idempotency key was already used for a different request")
            return existing, False
    for agent_id, p in params.items():
        spec = REGISTRY.get(agent_id)
        if spec is None:
            raise bad_request(f"Parameters supplied for unknown agent '{agent_id}'")
        try:
            spec.params.model_validate(p)
        except Exception as exc:
            raise ApiError(422, "invalid_agent_params", f"Invalid parameters for {agent_id}", str(exc)[:500]) from exc
    plan = preview_plan(principal, prompt, agent_ids, domains)
    task = AgentTask(city_id=city_id, created_by=principal.id, prompt=prompt, params=params, idempotency_key=idempotency_key,
                     plan=plan, status="queued", correlation_id=correlation_id_var.get())
    for a in plan["agents"]:
        task.executions.append(AgentExecution(agent_id=a["id"], status="pending", depends_on=a["depends_on"], reason=a["reason"]))
    db.add(task)
    db.flush()
    enqueue(db, "agent_task", {"task_id": task.id}, dedupe_key=f"task:{task.id}", priority=3, max_attempts=2, created_by=principal.id)
    audit(db, "task.submit", principal, target_type="agent_task", target_id=task.id, city_id=city_id,
          details={"agents": len(plan["agents"])})
    return task, True


def cancel_task(db: Session, principal: Principal, task: AgentTask) -> AgentTask:
    if task.status in ("succeeded", "partial", "failed", "cancelled"):
        raise conflict(f"Task is already {task.status}")
    if task.created_by != principal.id and not principal.has_role("operator"):
        raise forbidden("Only the task owner or an operator can cancel this task")
    task.cancel_requested = True
    if task.status == "queued":
        job = db.scalar(select(Job).where(Job.dedupe_key == f"task:{task.id}"))
        if job and job.status == "queued":
            job.status, job.finished_at = "cancelled", utcnow()
            task.status, task.finished_at = "cancelled", utcnow()
            for e in task.executions:
                e.status, e.error = "cancelled", "Cancelled before start"
    audit(db, "task.cancel", principal, target_type="agent_task", target_id=task.id, city_id=task.city_id)
    return task


# ----------------------------------------------------------------------------- action execution (human-approved only)

def _exec_create_incident(db: Session, action: ProposedAction, principal: Principal) -> dict[str, Any]:
    p = action.payload
    sev = p.get("severity", "moderate")
    if sev not in ("low", "moderate", "high", "critical"):
        raise bad_request("Invalid severity in action payload")
    inc = Incident(city_id=action.city_id, title=str(p.get("title", action.title))[:200], description=str(p.get("description", ""))[:2000],
                   category=str(p.get("category", "general"))[:32], severity=sev, lat=p.get("lat"), lon=p.get("lon"),
                   department=p.get("department"), provenance="operator", source=f"agent:{action.agent_id} (approved)",
                   created_by=principal.id)
    db.add(inc)
    db.flush()
    db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="created", to_status="open",
                         note=f"Created from approved recommendation {action.id}"))
    return {"incident_id": inc.id}


def _exec_escalate(db: Session, action: ProposedAction, principal: Principal) -> dict[str, Any]:
    inc = db.get(Incident, action.payload.get("incident_id"))
    if inc is None or inc.city_id != action.city_id:
        raise not_found("Incident")
    to = action.payload.get("to_status", "acknowledged")
    if inc.status in ("resolved", "closed"):
        return {"incident_id": inc.id, "note": f"Incident already {inc.status}; no change"}
    db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="status_change", from_status=inc.status, to_status=to,
                         note=f"Approved recommendation {action.id}"))
    inc.status = to
    inc.version += 1
    return {"incident_id": inc.id, "status": to}


def _exec_notify(db: Session, action: ProposedAction, principal: Principal) -> dict[str, Any]:
    n = Notification(city_id=action.city_id, level=str(action.payload.get("level", "info"))[:16], title=action.title[:200],
                     body=str(action.payload.get("body", action.rationale))[:2000], meta={"approved_by": principal.id, "action_id": action.id})
    db.add(n)
    db.flush()
    return {"notification_id": n.id, "scope": "SwarmOS operators (internal only)"}


EXECUTORS = {"create_incident": _exec_create_incident, "escalate_incident": _exec_escalate, "notify_operators": _exec_notify}


def decide_action(db: Session, principal: Principal, action: ProposedAction, approve: bool, note: str | None) -> ProposedAction:
    if action.status != "pending":
        raise conflict(f"Action is already {action.status}")
    if ROLE_RANK[principal.role] < ROLE_RANK[action.required_role]:
        raise forbidden(f"Approving this action requires the '{action.required_role}' role")
    action.decided_by, action.decided_at, action.decision_note = principal.id, utcnow(), (note or "")[:1000]
    if not approve:
        action.status = "rejected"
        audit(db, "action.reject", principal, target_type="proposed_action", target_id=action.id, city_id=action.city_id)
        return action
    action.status = "approved"
    executor = EXECUTORS.get(action.kind)
    if executor is None:
        action.status = "awaiting_manual"
        action.execution_result = {"note": "No automated executor exists for this action. It is approved for manual execution by the responsible department."}
    else:
        try:
            with db.begin_nested():
                action.execution_result = executor(db, action, principal)
            action.status, action.executed_at = "executed", utcnow()
        except ApiError as exc:
            action.status = "failed"
            action.execution_result = {"error": exc.message}
    audit(db, "action.approve", principal, target_type="proposed_action", target_id=action.id, city_id=action.city_id,
          details={"kind": action.kind, "status": action.status})
    return action

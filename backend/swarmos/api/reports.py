"""Persisted, bounded situation-report snapshots."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from swarmos.api.cities import dashboard as city_dashboard
from swarmos.api.common import PageParams, iso, page_params, paginate
from swarmos.core.audit import audit
from swarmos.core.errors import not_found
from swarmos.core.security import Principal, city_access, require_role
from swarmos.db import get_db, utcnow
from swarmos.models import AgentTask, City, InterventionOutcome, ProposedAction, ScenarioRun, SituationReport
from swarmos.services.consensus import compare_executions

router = APIRouter(tags=["reports"])
SNAPSHOT_SECTIONS = ("city", "generated_at", "indicators", "incidents", "infrastructure", "agents",
                     "trends", "alerts", "source_health", "sync_failures")


def report_dict(row: SituationReport, detail: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"id": row.id, "city_id": row.city_id, "title": row.title,
                              "created_at": iso(row.created_at), "generated_at": row.snapshot.get("generated_at")}
    if detail:
        result["snapshot"] = row.snapshot
    return result


@router.get("/cities/{city_id}/reports")
def list_reports(page: PageParams = Depends(page_params), city: City = Depends(city_access),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(SituationReport).where(SituationReport.city_id == city.id).order_by(SituationReport.created_at.desc())
    return paginate(db, stmt, page, report_dict)


@router.post("/cities/{city_id}/reports", status_code=201)
def create_report(city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    dashboard = city_dashboard(city=city, principal=principal, db=db)
    snapshot = {key: dashboard[key] for key in SNAPSHOT_SECTIONS if key in dashboard}
    generated = snapshot.get("generated_at") or utcnow().isoformat()
    snapshot["reporting_period"] = {
        "captured_at": generated,
        "description": "Point-in-time city snapshot. Trend series cover up to 48 hours; incident and source status reflect records at capture time.",
    }

    actions = db.scalars(select(ProposedAction).where(ProposedAction.city_id == city.id)
                         .order_by(ProposedAction.created_at.desc()).limit(12)).all()
    snapshot["recommendations"] = [{
        "id": action.id, "title": action.title, "kind": action.kind, "agent_id": action.agent_id,
        "rationale": action.rationale[:600], "risk": action.risk, "required_role": action.required_role,
        "status": action.status, "created_at": iso(action.created_at), "decided_at": iso(action.decided_at),
        "executed_at": iso(action.executed_at), "task_id": action.task_id,
    } for action in actions]

    outcomes = db.scalars(select(InterventionOutcome).where(InterventionOutcome.city_id == city.id)
                          .order_by(InterventionOutcome.recorded_at.desc()).limit(12)).all()
    snapshot["intervention_outcomes"] = [{
        "action_id": outcome.action_id, "outcome": outcome.outcome, "recorded_at": iso(outcome.recorded_at),
        "linked_measurement_count": len(outcome.measurement_ids or []),
        "note_included": False,
    } for outcome in outcomes]

    scenario_rows = db.execute(select(ScenarioRun, AgentTask).join(AgentTask, AgentTask.id == ScenarioRun.task_id)
                               .where(ScenarioRun.city_id == city.id)
                               .order_by(ScenarioRun.created_at.desc()).limit(8)).all()
    snapshot["scenario_runs"] = [{
        "id": run.id, "task_id": task.id, "name": run.name, "inputs": run.params,
        "status": task.status, "created_at": iso(run.created_at), "finished_at": iso(task.finished_at),
        "summary": str((task.result or {}).get("summary") or "")[:600],
        "metrics": (task.result or {}).get("metrics") or {},
        "provenance": (task.result or {}).get("provenance") or [],
        "limitations": (task.result or {}).get("limitations") or [],
        "missing": (task.result or {}).get("missing") or [],
    } for run, task in scenario_rows]

    recent_tasks = db.scalars(select(AgentTask).where(AgentTask.city_id == city.id)
                              .options(selectinload(AgentTask.executions))
                              .order_by(AgentTask.created_at.desc()).limit(6)).all()
    agent_reviews = []
    for task in recent_tasks:
        comparison = compare_executions(task.executions)
        agent_reviews.append({
            "task_id": task.id, "status": task.status, "created_at": iso(task.created_at),
            "finished_at": iso(task.finished_at), "summary": str((task.result or {}).get("summary") or "")[:600],
            "agent_count": len(task.executions), "consensus": {**comparison, "comparisons": comparison["comparisons"][:12]},
        })
    snapshot["agent_reviews"] = agent_reviews

    snapshot["cross_domain_impacts"] = [{
        "task_id": task.id, "agent_id": execution.agent_id,
        "summary": str((execution.output or {}).get("summary") or "")[:600],
        "findings": ((execution.output or {}).get("findings") or [])[:10],
        "metrics": (execution.output or {}).get("metrics") or {},
        "data_refs": ((execution.output or {}).get("data_refs") or [])[:20],
        "provenance": ((execution.output or {}).get("provenance") or [])[:12],
        "limitations": ((execution.output or {}).get("limitations") or [])[:10],
    } for task in recent_tasks for execution in task.executions
      if execution.agent_id in {"cross_domain_correlation_agent", "city_situation_summarizer"}
      and execution.status in {"succeeded", "degraded"} and execution.output][:6]

    title = f"Situation report — {city.name} — {generated[:16].replace('T', ' ')} UTC"
    row = SituationReport(city_id=city.id, created_by=principal.id, title=title, snapshot=snapshot)
    db.add(row)
    db.flush()
    audit(db, "situation_report.create", principal, target_type="situation_report", target_id=row.id,
          city_id=city.id, details={"sections": sorted(snapshot)})
    return report_dict(row, detail=True)


@router.get("/cities/{city_id}/reports/{report_id}")
def get_report(report_id: str, city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(SituationReport, report_id)
    if row is None or row.city_id != city.id:
        raise not_found("Situation report")
    return report_dict(row, detail=True)

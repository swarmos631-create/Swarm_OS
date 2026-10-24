"""Integrations, model lab, knowledge base, analytics, notifications and system endpoints."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from swarmos.ai import retrieval
from swarmos.ai.runtime import get_runtime
from swarmos.api.common import PageParams, iso, page_params, paginate
from swarmos.config import get_settings
from swarmos.core.audit import audit
from swarmos.core.errors import ApiError, bad_request, not_found
from swarmos.core.security import (
    ROLES_LIST,
    Principal,
    city_access,
    current_principal,
    hash_password,
    require_role,
    validate_password_strength,
)
from swarmos.db import get_db, utcnow
from swarmos.integrations.adapters import ADAPTERS
from swarmos.integrations.sync import city_source_status
from swarmos.jobs.queue import enqueue
from swarmos.ml import pipeline
from swarmos.models import (
    AgentExecution,
    AgentTask,
    AuditLog,
    City,
    DataSource,
    Document,
    Incident,
    Job,
    Measurement,
    ModelVersion,
    Notification,
    SyncRun,
    User,
    UserCity,
)

router = APIRouter()

# ----------------------------------------------------------------------------- integrations


def source_dict(s: DataSource) -> dict[str, Any]:
    spec = ADAPTERS[s.id].spec if s.id in ADAPTERS else None
    return {"id": s.id, "name": s.name, "kind": s.kind, "categories": s.categories, "coverage": s.coverage, "auth_required": s.auth_required,
            "license": s.license, "attribution": s.attribution, "homepage": s.homepage, "freshness_threshold_s": s.freshness_threshold_s,
            "status": s.status, "status_detail": s.status_detail, "enabled": s.enabled, "last_success_at": iso(s.last_success_at),
            "last_attempt_at": iso(s.last_attempt_at), "last_error_at": iso(s.last_error_at), "last_error": s.last_error,
            "consecutive_failures": s.consecutive_failures, "limitations": s.limitations, "setup": s.setup,
            "scope": spec.scope if spec else "none", "min_sync_interval_s": spec.min_sync_interval_s if spec else None}


@router.get("/integrations", tags=["integrations"])
def list_sources(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": [source_dict(s) for s in db.scalars(select(DataSource).order_by(DataSource.kind, DataSource.id))]}


@router.get("/cities/{city_id}/integrations", tags=["integrations"])
def city_sources(city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    runs = db.scalars(select(SyncRun).where((SyncRun.city_id == city.id) | SyncRun.city_id.is_(None))
                      .order_by(SyncRun.started_at.desc()).limit(25))
    return {"items": city_source_status(db, city.id),
            "recent_runs": [{"source_id": r.source_id, "status": r.status, "started_at": iso(r.started_at), "duration_ms": r.duration_ms,
                             "records": r.records, "error": r.error} for r in runs]}


class SyncIn(BaseModel):
    force: bool = False


@router.post("/cities/{city_id}/integrations/{source_id}/sync", status_code=202, tags=["integrations"])
def trigger_sync(source_id: str, body: SyncIn, city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    adapter = ADAPTERS.get(source_id)
    if adapter is None:
        raise not_found("Data source")
    ok, reason = adapter.configured(get_settings())
    if adapter.spec.scope == "none" or not ok:
        raise ApiError(409, "source_not_syncable", reason or "This source cannot be synchronised", {"setup": adapter.spec.setup})
    city_id = city.id if adapter.spec.scope == "city" else None
    bucket = int(utcnow().timestamp() // 30)
    job = enqueue(db, "sync", {"source_id": source_id, "city_id": city_id, "force": body.force},
                  dedupe_key=f"manual:{source_id}:{city_id}:{bucket}", priority=2, max_attempts=1, created_by=principal.id)
    audit(db, "integration.sync", principal, target_type="data_source", target_id=source_id, city_id=city.id)
    return {"job_id": job.id, "status": job.status}


# ----------------------------------------------------------------------------- model lab


def mv_dict(m: ModelVersion) -> dict[str, Any]:
    return {"id": m.id, "model_name": m.model_name, "version": m.version, "task_type": m.task_type, "algorithm": m.algorithm,
            "metrics": m.metrics, "baseline_metrics": m.baseline_metrics, "dataset": m.dataset_manifest, "status": m.status,
            "passed_gate": m.passed_gate, "created_at": iso(m.created_at), "promoted_at": iso(m.promoted_at),
            "artifact_sha256": m.artifact_sha256[:16], "report_path": m.report_path, "job_id": m.job_id}


@router.get("/models", tags=["models"])
def list_models(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    out = []
    for cfg in pipeline.available_configs(settings):
        name = cfg["model_name"]
        versions = list(db.scalars(select(ModelVersion).where(ModelVersion.model_name == name).order_by(ModelVersion.created_at.desc())))
        jobs = list(db.scalars(select(Job).where(Job.kind == "train_model").order_by(Job.created_at.desc()).limit(30)))
        job = next((j for j in jobs if j.payload.get("model_name") == name), None)
        out.append({"model_name": name, "description": cfg.get("description"), "task_type": cfg["task_type"], "algorithm": cfg["algorithm"],
                    "used_by": cfg.get("used_by", []), "gate": cfg["gate"], "dataset": cfg["dataset"],
                    "promoted": next((mv_dict(v) for v in versions if v.status == "promoted"), None),
                    "versions": [mv_dict(v) for v in versions[:10]],
                    "latest_job": None if job is None else {"id": job.id, "status": job.status, "error": job.error,
                                                            "created_at": iso(job.created_at), "finished_at": iso(job.finished_at)}})
    return {"items": out, "runtime": get_runtime().health()}


class TrainIn(BaseModel):
    promote_if_better: bool = True


@router.post("/models/{model_name}/train", status_code=202, tags=["models"])
def train(model_name: str, body: TrainIn, principal: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)) -> dict[str, Any]:
    if model_name not in {c["model_name"] for c in pipeline.available_configs(get_settings())}:
        raise not_found("Model config")
    active = db.scalars(select(Job).where(Job.kind == "train_model", Job.status.in_(("queued", "running")))).all()
    if any(j.payload.get("model_name") == model_name for j in active):
        raise ApiError(409, "already_training", "A training job for this model is already queued or running")
    job = enqueue(db, "train_model", {"model_name": model_name, "promote_if_better": body.promote_if_better, "actor_id": principal.id},
                  priority=6, max_attempts=1, created_by=principal.id)
    audit(db, "model.train_requested", principal, target_type="model", target_id=model_name)
    return {"job_id": job.id, "status": job.status}


class PromoteIn(BaseModel):
    force: bool = False


@router.post("/models/{model_name}/versions/{version}/promote", tags=["models"])
def promote(model_name: str, version: str, body: PromoteIn, principal: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    try:
        pipeline.promote(model_name, version, actor_id=principal.id, force=body.force)
    except pipeline.PromotionError as exc:
        raise ApiError(409, "promotion_refused", str(exc)) from exc
    return {"ok": True, "promoted": f"{model_name}@{version}"}


@router.post("/models/{model_name}/rollback", tags=["models"])
def rollback(model_name: str, principal: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    try:
        target = pipeline.rollback(model_name, actor_id=principal.id)
    except pipeline.PromotionError as exc:
        raise ApiError(409, "rollback_refused", str(exc)) from exc
    return {"ok": True, "promoted": f"{model_name}@{target}"}


@router.get("/models/{model_name}/versions/{version}/report", tags=["models"])
def report(model_name: str, version: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    mv = db.scalar(select(ModelVersion).where(ModelVersion.model_name == model_name, ModelVersion.version == version))
    if mv is None or not mv.report_path:
        raise not_found("Report")
    root = get_settings().ml_root.resolve()
    path = (root / mv.report_path).resolve()
    if root not in path.parents or not path.exists():
        raise not_found("Report")
    return {"markdown": Path(path).read_text(encoding="utf-8")}


# ----------------------------------------------------------------------------- knowledge base

class DocumentIn(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    text: str = Field(min_length=20, max_length=retrieval.MAX_DOC_CHARS)
    source: str = Field(min_length=3, max_length=300)
    classification: Literal["public", "internal", "restricted"] = "internal"
    license: str = Field(default="Internal use", max_length=200)
    city_scoped: bool = True


@router.get("/cities/{city_id}/documents", tags=["knowledge"])
def list_documents(city: City = Depends(city_access), principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    docs = db.scalars(select(Document).where(Document.classification.in_(principal.clearance),
                                             (Document.city_id.is_(None)) | (Document.city_id == city.id)).order_by(Document.created_at.desc()))
    return {"items": [{"id": d.id, "title": d.title, "source": d.source, "classification": d.classification, "license": d.license,
                       "city_id": d.city_id, "synthetic": d.is_synthetic, "chunks": len(d.chunks), "created_at": iso(d.created_at)} for d in docs]}


@router.post("/cities/{city_id}/documents", status_code=201, tags=["knowledge"])
def add_document(body: DocumentIn, city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.classification not in principal.clearance:
        raise ApiError(403, "forbidden", "You cannot upload documents above your clearance")
    doc = retrieval.ingest(db, title=body.title, text=body.text, source=body.source, classification=body.classification,
                           license=body.license, city_id=city.id if body.city_scoped else None, uploaded_by=principal.id)
    audit(db, "document.ingest", principal, target_type="document", target_id=doc.id, city_id=city.id)
    return {"id": doc.id, "chunks": len(doc.chunks)}


@router.get("/cities/{city_id}/documents/search", tags=["knowledge"])
def search_documents(q: str = Query(..., min_length=2, max_length=300), city: City = Depends(city_access),
                     principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"query": q, "hits": [h.as_dict() for h in retrieval.search(db, q, city_id=city.id, clearance=principal.clearance, k=8)]}


# ----------------------------------------------------------------------------- analytics


@router.get("/cities/{city_id}/analytics", tags=["analytics"])
def analytics(days: int = Query(14, ge=1, le=90), city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    now = utcnow()
    since = now - timedelta(days=days)
    incs = list(db.scalars(select(Incident).where(Incident.city_id == city.id, Incident.reported_at >= since)))
    by_day: dict[str, dict[str, int]] = {}
    for i in incs:
        d = i.reported_at.date().isoformat()
        by_day.setdefault(d, {"low": 0, "moderate": 0, "high": 0, "critical": 0})[i.severity] += 1
    days_list = [(since + timedelta(days=k)).date().isoformat() for k in range(days + 1)]
    sev_dist = {s: sum(1 for i in incs if i.severity == s) for s in ("low", "moderate", "high", "critical")}
    cat_dist: dict[str, int] = {}
    for i in incs:
        cat_dist[i.category] = cat_dist.get(i.category, 0) + 1
    resolved = [i for i in incs if i.resolved_at]
    mttr = sum((i.resolved_at - i.reported_at).total_seconds() for i in resolved if i.resolved_at) / 3600 / len(resolved) if resolved else None
    tasks = db.execute(select(AgentTask.status, func.count(AgentTask.id)).where(AgentTask.city_id == city.id, AgentTask.created_at >= since)
                       .group_by(AgentTask.status)).all()
    exec_rows = db.execute(select(AgentExecution.agent_id, AgentExecution.status, func.count(AgentExecution.id))
                           .join(AgentTask, AgentTask.id == AgentExecution.task_id)
                           .where(AgentTask.city_id == city.id, AgentTask.created_at >= since)
                           .group_by(AgentExecution.agent_id, AgentExecution.status)).all()
    per_agent: dict[str, dict[str, int]] = {}
    for a, s, n in exec_rows:
        per_agent.setdefault(a, {})[s] = n
    sync_rows = db.execute(select(SyncRun.source_id, SyncRun.status, func.count(SyncRun.id), func.avg(SyncRun.duration_ms))
                           .where(SyncRun.started_at >= since, (SyncRun.city_id == city.id) | SyncRun.city_id.is_(None))
                           .group_by(SyncRun.source_id, SyncRun.status)).all()
    reliability: dict[str, dict[str, Any]] = {}
    for src, st, n, avg in sync_rows:
        r = reliability.setdefault(src, {"succeeded": 0, "failed": 0, "avg_ms": None})
        r[st] = r.get(st, 0) + n
        r["avg_ms"] = round(avg) if avg else r["avg_ms"]
    for r in reliability.values():
        tot = r["succeeded"] + r["failed"]
        r["success_rate"] = round(r["succeeded"] / tot, 3) if tot else None
    # daily means for live environmental metrics (only where data exists)
    env = {}
    for metric in ("temperature_2m", "pm2_5", "precipitation"):
        rows = db.execute(select(Measurement.observed_at, Measurement.value, Measurement.provenance, Measurement.unit)
                          .where(Measurement.city_id == city.id, Measurement.metric == metric, Measurement.entity == "city",
                                 Measurement.kind.in_(("observation", "model_analysis")), Measurement.observed_at >= since)).all()
        if rows:
            agg: dict[str, list[float]] = {}
            for t, v, _, _ in rows:
                agg.setdefault(t.date().isoformat(), []).append(v)
            env[metric] = {"unit": rows[0][3], "provenance": sorted({r[2] for r in rows}),
                           "daily": [[d, round(sum(v) / len(v), 2) if metric != "precipitation" else round(sum(v), 2)] for d, v in sorted(agg.items())],
                           "aggregation": "daily sum" if metric == "precipitation" else "daily mean"}
    return {"range_days": days, "incidents": {"total": len(incs), "by_day": [[d, by_day.get(d, {"low": 0, "moderate": 0, "high": 0, "critical": 0})] for d in days_list],
                                              "severity": sev_dist, "category": cat_dist, "mttr_h": round(mttr, 2) if mttr else None,
                                              "provenance": sorted({i.provenance for i in incs})},
            "tasks": dict(tasks), "agent_executions": per_agent, "source_reliability": reliability, "environment": env}


@router.get("/analytics/compare", tags=["analytics"])
def compare(metric: Literal["temperature_2m", "pm2_5", "pm10", "precipitation", "relative_humidity_2m"] = "pm2_5",
            hours: int = Query(24, ge=6, le=168), cities: str = Query(..., max_length=400),
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    ids = [c for c in cities.split(",") if c][:8]
    since = utcnow() - timedelta(hours=hours)
    out = []
    for cid in ids:
        city = db.get(City, cid)
        if city is None or not principal.can_access_city(cid):
            raise bad_request(f"City '{cid}' is unknown or not authorised")
        rows = db.execute(select(func.avg(Measurement.value), func.count(Measurement.id), func.max(Measurement.fetched_at), func.min(Measurement.unit))
                          .where(Measurement.city_id == cid, Measurement.metric == metric, Measurement.entity == "city",
                                 Measurement.source_id.like("open-meteo%"), Measurement.kind == "model_analysis",
                                 Measurement.observed_at >= since)).one()
        avg, n, fetched, unit = rows
        expected = hours * 0.8
        out.append({"city_id": cid, "name": city.name, "mean": round(avg, 2) if avg is not None and n >= expected else None,
                    "points": n, "unit": unit, "fetched_at": iso(fetched),
                    "note": None if avg is not None and n >= expected else "Insufficient coverage for this window — not compared"})
    return {"metric": metric, "hours": hours, "source": "Open-Meteo model analysis (same source and method for every city)", "items": out}


# ----------------------------------------------------------------------------- notifications & system


@router.get("/notifications", tags=["system"])
def notifications(city_id: str | None = Query(None, max_length=64), principal: Principal = Depends(current_principal),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(Notification).order_by(Notification.created_at.desc()).limit(30)
    if city_id:
        if not principal.can_access_city(city_id):
            raise ApiError(403, "forbidden", "Not authorised for this city")
        q = q.where((Notification.city_id == city_id) | Notification.city_id.is_(None))
    items = [n for n in db.scalars(q) if n.city_id is None or principal.can_access_city(n.city_id)]
    return {"items": [{"id": n.id, "city_id": n.city_id, "level": n.level, "title": n.title, "body": n.body, "link": n.link,
                       "created_at": iso(n.created_at)} for n in items]}


@router.get("/system/status", tags=["system"])
def system_status(principal: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    db.execute(text("SELECT 1"))
    jobs = dict(db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all())
    recent_jobs = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(20))
    return {
        "environment": settings.env, "database": settings.database_url.split(":", 1)[0], "demo_mode": settings.demo_mode,
        "worker": {"embedded": settings.worker_embedded, "job_counts": jobs},
        "jobs": [{"id": j.id, "kind": j.kind, "status": j.status, "attempts": j.attempts, "created_at": iso(j.created_at),
                  "finished_at": iso(j.finished_at), "error": (j.error or "")[:300] or None} for j in recent_jobs],
        "model_runtime": get_runtime().health(),
        "limits": {"agent_max_concurrency": settings.agent_max_concurrency, "agent_timeout_s": settings.agent_default_timeout_seconds,
                   "task_max_agents": settings.task_max_agents, "model_max_concurrency": settings.model_max_concurrency},
        "sync": {"enabled": settings.sync_enabled, "interval_minutes": settings.sync_interval_minutes, "cities": settings.sync_cities},
    }


@router.get("/system/audit", tags=["system"])
def audit_log(action: str | None = Query(None, max_length=64), page: PageParams = Depends(page_params),
              principal: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return paginate(db, stmt.order_by(AuditLog.at.desc()), page, lambda a: {
        "id": a.id, "at": iso(a.at), "actor_email": a.actor_email, "action": a.action, "target_type": a.target_type,
        "target_id": a.target_id, "city_id": a.city_id, "outcome": a.outcome, "details": a.details, "ip": a.ip,
        "correlation_id": a.correlation_id})


class UserIn(BaseModel):
    email: str = Field(min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field(min_length=2, max_length=120)
    role: Literal["viewer", "operator", "admin"]
    password: str = Field(min_length=12, max_length=256)
    all_cities: bool = False
    city_ids: list[str] = Field(default_factory=list, max_length=200)


@router.get("/system/users", tags=["system"])
def users(principal: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": [{"id": u.id, "email": u.email, "display_name": u.display_name, "role": u.role, "all_cities": u.all_cities,
                       "city_ids": [c.city_id for c in u.cities], "is_active": u.is_active, "last_login_at": iso(u.last_login_at)}
                      for u in db.scalars(select(User).order_by(User.email))], "roles": ROLES_LIST}


@router.post("/system/users", status_code=201, tags=["system"])
def create_user(body: UserIn, principal: Principal = Depends(require_role("admin")), db: Session = Depends(get_db)) -> dict[str, Any]:
    validate_password_strength(body.password)
    if db.scalar(select(User).where(User.email == body.email.lower())):
        raise ApiError(409, "conflict", "A user with this email already exists")
    for cid in body.city_ids:
        if db.get(City, cid) is None:
            raise bad_request(f"Unknown city '{cid}'")
    u = User(email=body.email.lower(), display_name=body.display_name, role=body.role, all_cities=body.all_cities,
             password_hash=hash_password(body.password), cities=[UserCity(city_id=c) for c in body.city_ids])
    db.add(u)
    db.flush()
    audit(db, "user.create", principal, target_type="user", target_id=u.id, details={"role": body.role})
    return {"id": u.id}

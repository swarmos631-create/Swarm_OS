"""Incidents and civic service requests (city-scoped, role-enforced, audited)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from swarmos.api.common import PageParams, iso, page_params, paginate
from swarmos.cities import haversine_km
from swarmos.core.audit import audit
from swarmos.core.errors import ApiError, bad_request, conflict, not_found
from swarmos.core.security import Principal, city_access, require_role
from swarmos.db import get_db, utcnow
from swarmos.ml.complaints import rule_classify
from swarmos.models import City, Incident, IncidentEvent, ServiceRequest

router = APIRouter(tags=["incidents"])

Severity = Literal["low", "moderate", "high", "critical"]
Status = Literal["open", "acknowledged", "in_progress", "resolved", "closed"]
ALLOWED_TRANSITIONS = {
    "open": {"acknowledged", "in_progress", "resolved", "closed"},
    "acknowledged": {"in_progress", "resolved", "closed"},
    "in_progress": {"resolved", "closed", "acknowledged"},
    "resolved": {"closed", "open"},
    "closed": {"open"},
}
CATEGORIES = ("road", "mobility", "flooding", "drainage", "fire", "medical", "structural", "energy", "water", "utility",
              "garbage", "construction", "hazmat", "general")


def incident_dict(i: Incident, events: bool = False) -> dict[str, Any]:
    d = {"id": i.id, "city_id": i.city_id, "title": i.title, "description": i.description, "category": i.category,
         "severity": i.severity, "status": i.status, "lat": i.lat, "lon": i.lon, "department": i.department,
         "provenance": i.provenance, "source": i.source, "reported_at": iso(i.reported_at), "updated_at": iso(i.updated_at),
         "resolved_at": iso(i.resolved_at), "version": i.version}
    return d


class IncidentIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=4000)
    category: str
    severity: Severity
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    department: str | None = Field(default=None, max_length=80)

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        return v


class IncidentPatch(BaseModel):
    status: Status | None = None
    severity: Severity | None = None
    department: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=2000)
    version: int = Field(ge=1, description="Optimistic concurrency token from the last read")


@router.get("/cities/{city_id}/incidents")
def list_incidents(status: str | None = Query(None, max_length=80), severity: str | None = Query(None, max_length=80),
                   category: str | None = Query(None, max_length=200), q: str | None = Query(None, max_length=100),
                   sort: Literal["reported_at", "-reported_at", "severity"] = "-reported_at",
                   page: PageParams = Depends(page_params), city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(Incident).where(Incident.city_id == city.id)
    if status:
        stmt = stmt.where(Incident.status.in_(status.split(",")))
    if severity:
        stmt = stmt.where(Incident.severity.in_(severity.split(",")))
    if category:
        stmt = stmt.where(Incident.category.in_(category.split(",")))
    if q:
        stmt = stmt.where(Incident.title.ilike(f"%{q}%"))  # bound parameter, not string-interpolated SQL
    if sort == "severity":
        from sqlalchemy import case

        stmt = stmt.order_by(case({"critical": 0, "high": 1, "moderate": 2, "low": 3}, value=Incident.severity), Incident.reported_at.desc())
    else:
        stmt = stmt.order_by(Incident.reported_at.desc() if sort.startswith("-") else Incident.reported_at)
    return paginate(db, stmt, page, incident_dict)


@router.post("/cities/{city_id}/incidents", status_code=201)
def create_incident(body: IncidentIn, city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.lat is not None and body.lon is not None and haversine_km(city.lat, city.lon, body.lat, body.lon) > 150:
        raise bad_request("Incident location is more than 150 km from the selected city")
    inc = Incident(city_id=city.id, title=body.title, description=body.description, category=body.category, severity=body.severity,
                   lat=body.lat, lon=body.lon, department=body.department, provenance="operator", source="operator",
                   created_by=principal.id)
    db.add(inc)
    db.flush()
    db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="created", to_status="open"))
    audit(db, "incident.create", principal, target_type="incident", target_id=inc.id, city_id=city.id, details={"severity": inc.severity})
    return incident_dict(inc)


def _get_incident(db: Session, city: City, incident_id: str) -> Incident:
    inc = db.get(Incident, incident_id)
    if inc is None or inc.city_id != city.id:  # never reveal records from other cities
        raise not_found("Incident")
    return inc


@router.get("/cities/{city_id}/incidents/{incident_id}")
def get_incident(incident_id: str, city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = _get_incident(db, city, incident_id)
    events = db.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id).order_by(IncidentEvent.created_at))
    return {**incident_dict(inc), "events": [{"kind": e.kind, "from_status": e.from_status, "to_status": e.to_status, "note": e.note,
                                              "actor_id": e.actor_id, "created_at": iso(e.created_at)} for e in events]}


@router.patch("/cities/{city_id}/incidents/{incident_id}")
def update_incident(incident_id: str, body: IncidentPatch, city: City = Depends(city_access),
                    principal: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = _get_incident(db, city, incident_id)
    if inc.version != body.version:
        raise conflict("Incident was modified by someone else; reload and retry", {"current_version": inc.version})
    changes: dict[str, Any] = {}
    if body.status and body.status != inc.status:
        if body.status not in ALLOWED_TRANSITIONS[inc.status]:
            raise ApiError(422, "invalid_transition", f"Cannot move from {inc.status} to {body.status}")
        db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="status_change", from_status=inc.status,
                             to_status=body.status, note=body.note))
        changes["status"] = [inc.status, body.status]
        inc.status = body.status
        inc.resolved_at = utcnow() if body.status in ("resolved", "closed") else None
    if body.severity and body.severity != inc.severity:
        changes["severity"] = [inc.severity, body.severity]
        db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="severity_change", note=f"{inc.severity} → {body.severity}"))
        inc.severity = body.severity
    if body.department is not None and body.department != inc.department:
        changes["department"] = [inc.department, body.department]
        inc.department = body.department
    if body.note and not changes:
        db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="note", note=body.note))
    if changes or body.note:
        inc.version += 1
        inc.updated_at = utcnow()
    audit(db, "incident.update", principal, target_type="incident", target_id=inc.id, city_id=city.id, details=changes)
    return incident_dict(inc)


# ----------------------------------------------------------------------------- service requests

class ServiceRequestIn(BaseModel):
    text: str = Field(min_length=5, max_length=2000)
    ward: str | None = Field(default=None, max_length=80)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


def sr_dict(r: ServiceRequest) -> dict[str, Any]:
    return {"id": r.id, "text": r.text, "category": r.category, "category_confidence": r.category_confidence, "classified_by": r.classified_by,
            "department": r.department, "status": r.status, "ward": r.ward, "lat": r.lat, "lon": r.lon, "provenance": r.provenance,
            "created_at": iso(r.created_at), "resolved_at": iso(r.resolved_at)}


@router.get("/cities/{city_id}/service-requests")
def list_requests(status: str | None = Query(None, max_length=40), category: str | None = Query(None, max_length=200),
                  page: PageParams = Depends(page_params), city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(ServiceRequest).where(ServiceRequest.city_id == city.id)
    if status:
        stmt = stmt.where(ServiceRequest.status == status)
    if category:
        stmt = stmt.where(ServiceRequest.category.in_(category.split(",")))
    return paginate(db, stmt.order_by(ServiceRequest.created_at.desc()), page, sr_dict)


@router.post("/cities/{city_id}/service-requests", status_code=201)
def create_request(body: ServiceRequestIn, city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    from swarmos.agents.skills.civic import DEPARTMENTS
    from swarmos.ai.runtime import ModelUnavailable, get_runtime

    try:
        model = get_runtime().load("complaint_classifier")
        probs = model.estimator.predict_proba([body.text])[0]
        cat, conf, by = model.estimator.classes_[int(probs.argmax())], float(probs.max()), model.label
    except ModelUnavailable:
        cat, conf = rule_classify(body.text)
        by = "rules:keyword_classifier"
    r = ServiceRequest(city_id=city.id, text=body.text, ward=body.ward, lat=body.lat, lon=body.lon, category=str(cat),
                       category_confidence=round(conf, 3), classified_by=by, department=DEPARTMENTS.get(str(cat)), provenance="operator")
    db.add(r)
    db.flush()
    audit(db, "service_request.create", principal, target_type="service_request", target_id=r.id, city_id=city.id)
    return sr_dict(r)

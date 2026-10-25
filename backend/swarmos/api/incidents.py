"""Incidents and civic service requests (city-scoped, role-enforced, audited)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swarmos.api.common import PageParams, iso, page_params, paginate
from swarmos.cities import haversine_km
from swarmos.core.audit import audit
from swarmos.core.errors import ApiError, bad_request, conflict, not_found
from swarmos.core.ratelimit import client_ip, limiter
from swarmos.core.security import Principal, city_access, require_role
from swarmos.db import get_db, utcnow
from swarmos.ml.complaints import rule_classify
from swarmos.models import City, Incident, IncidentEvent, IncidentEvidence, ServiceRequest
from swarmos.services.evidence import EvidenceType, evidence_dict, list_candidates, resolve_source

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
    evidence = db.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == inc.id)
                           .order_by(IncidentEvidence.created_at.desc(), IncidentEvidence.id.desc()))
    return {**incident_dict(inc), "events": [{"kind": e.kind, "from_status": e.from_status, "to_status": e.to_status, "note": e.note,
                                              "actor_id": e.actor_id, "created_at": iso(e.created_at)} for e in events],
            "evidence": [evidence_dict(db, city.id, row) for row in evidence]}


class IncidentEvidenceIn(BaseModel):
    source_type: Literal["measurement", "agent_execution", "service_request", "feature", "proposed_action"]
    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    relationship: Literal["supports", "contradicts", "context"]
    note: str = Field(min_length=6, max_length=1000)

    @field_validator("note")
    @classmethod
    def _note_is_meaningful(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 6:
            raise ValueError("note must contain at least 6 non-space characters")
        return value


@router.get("/cities/{city_id}/incidents/{incident_id}/evidence/candidates")
def incident_evidence_candidates(incident_id: str,
                                 source_type: EvidenceType = Query(...),
                                 city: City = Depends(city_access),
                                 principal: Principal = Depends(require_role("operator")),
                                 db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = _get_incident(db, city, incident_id)
    linked = set(db.scalars(select(IncidentEvidence.source_id).where(
        IncidentEvidence.incident_id == inc.id,
        IncidentEvidence.source_type == source_type,
    )).all())
    return {"items": [item for item in list_candidates(db, city.id, source_type)
                       if item["source_id"] not in linked]}


@router.post("/cities/{city_id}/incidents/{incident_id}/evidence", status_code=201)
def link_incident_evidence(incident_id: str, body: IncidentEvidenceIn, city: City = Depends(city_access),
                           principal: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = _get_incident(db, city, incident_id)
    resolve_source(db, city.id, body.source_type, body.source_id)
    existing = db.scalar(select(IncidentEvidence).where(
        IncidentEvidence.incident_id == inc.id,
        IncidentEvidence.source_type == body.source_type,
        IncidentEvidence.source_id == body.source_id,
    ))
    if existing:
        raise conflict("This evidence record is already linked to the incident")
    link = IncidentEvidence(incident_id=inc.id, source_type=body.source_type, source_id=body.source_id,
                            relationship=body.relationship, note=body.note, created_by=principal.id)
    try:
        with db.begin_nested():
            db.add(link)
            db.flush()
    except IntegrityError as exc:
        raise conflict("This evidence record is already linked to the incident") from exc
    db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="evidence_added",
                         note=f"{body.relationship}: {body.note}"))
    inc.updated_at = utcnow()
    inc.version += 1
    audit(db, "incident.evidence.add", principal, target_type="incident", target_id=inc.id, city_id=city.id,
          details={"source_type": body.source_type, "source_id": body.source_id, "relationship": body.relationship})
    return evidence_dict(db, city.id, link)


@router.delete("/cities/{city_id}/incidents/{incident_id}/evidence/{evidence_id}", status_code=204)
def unlink_incident_evidence(incident_id: str, evidence_id: int, city: City = Depends(city_access),
                             principal: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)) -> None:
    inc = _get_incident(db, city, incident_id)
    link = db.get(IncidentEvidence, evidence_id)
    if link is None or link.incident_id != inc.id:
        raise not_found("Incident evidence")
    db.add(IncidentEvent(incident_id=inc.id, actor_id=principal.id, kind="evidence_removed",
                         note=f"{link.source_type}:{link.source_id}"))
    db.delete(link)
    inc.updated_at = utcnow()
    inc.version += 1
    audit(db, "incident.evidence.remove", principal, target_type="incident", target_id=inc.id, city_id=city.id,
          details={"evidence_id": evidence_id})


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


class PublicServiceRequestIn(BaseModel):
    text: str = Field(min_length=10, max_length=2000)


class ServiceRequestStatusIn(BaseModel):
    status: Literal["open", "in_progress", "resolved"]


def sr_dict(r: ServiceRequest) -> dict[str, Any]:
    return {"id": r.id, "text": r.text, "category": r.category, "category_confidence": r.category_confidence, "classified_by": r.classified_by,
            "department": r.department, "status": r.status, "ward": r.ward, "lat": r.lat, "lon": r.lon, "provenance": r.provenance,
            "created_at": iso(r.created_at), "resolved_at": iso(r.resolved_at)}


def _classify_request_text(text: str) -> tuple[str, float, str, str | None]:
    from swarmos.agents.skills.civic import DEPARTMENTS
    from swarmos.ai.runtime import ModelUnavailable, get_runtime

    try:
        model = get_runtime().load("complaint_classifier")
        probs = model.estimator.predict_proba([text])[0]
        category = model.estimator.classes_[int(probs.argmax())]
        confidence = float(probs.max())
        by = model.label
    except ModelUnavailable:
        category, confidence = rule_classify(text)
        by = "rules:keyword_classifier"
    category = str(category)
    return category, round(confidence, 3), by, DEPARTMENTS.get(category)


@router.post("/public/cities/{city_id}/service-requests", status_code=201)
def public_create_request(city_id: str, body: PublicServiceRequestIn, request: Request,
                          db: Session = Depends(get_db)) -> dict[str, Any]:
    limiter.check(f"public-submit:{client_ip(request)}", 5, window_s=600)
    city = db.get(City, city_id)
    if city is None:
        raise not_found("City")
    category, confidence, by, department = _classify_request_text(body.text.strip())
    report = ServiceRequest(city_id=city.id, text=body.text.strip(), category=category,
                            category_confidence=confidence, classified_by=by, department=department,
                            provenance="public")
    db.add(report)
    db.flush()
    return {"reference": report.id, "city_id": city.id, "status": report.status,
            "category": report.category, "created_at": iso(report.created_at),
            "privacy": "This form does not ask for a name, contact details, or precise location."}


@router.get("/public/cities/{city_id}/service-requests/{reference}")
def public_request_status(city_id: str, reference: str, request: Request,
                          db: Session = Depends(get_db)) -> dict[str, Any]:
    limiter.check(f"public-status:{client_ip(request)}", 30)
    if len(reference) != 32 or any(char not in "0123456789abcdef" for char in reference.lower()):
        raise not_found("Request reference")
    report = db.get(ServiceRequest, reference)
    if report is None or report.city_id != city_id:
        raise not_found("Request reference")
    return {"reference": report.id, "status": report.status, "category": report.category,
            "created_at": iso(report.created_at), "resolved_at": iso(report.resolved_at)}


@router.get("/cities/{city_id}/service-requests")
def list_requests(status: str | None = Query(None, max_length=40), category: str | None = Query(None, max_length=200),
                  page: PageParams = Depends(page_params), city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(ServiceRequest).where(ServiceRequest.city_id == city.id)
    if status:
        stmt = stmt.where(ServiceRequest.status == status)
    if category:
        stmt = stmt.where(ServiceRequest.category.in_(category.split(",")))
    return paginate(db, stmt.order_by(ServiceRequest.created_at.desc()), page, sr_dict)


@router.patch("/cities/{city_id}/service-requests/{request_id}")
def update_service_request(request_id: str, body: ServiceRequestStatusIn, city: City = Depends(city_access),
                           principal: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(ServiceRequest, request_id)
    if report is None or report.city_id != city.id:
        raise not_found("Service request")
    if body.status == report.status:
        return sr_dict(report)
    report.status = body.status
    report.resolved_at = utcnow() if body.status == "resolved" else None
    audit(db, "service_request.status", principal, target_type="service_request", target_id=report.id,
          city_id=city.id, details={"status": body.status})
    return sr_dict(report)


@router.post("/cities/{city_id}/service-requests", status_code=201)
def create_request(body: ServiceRequestIn, city: City = Depends(city_access), principal: Principal = Depends(require_role("operator")),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    cat, conf, by, department = _classify_request_text(body.text)
    r = ServiceRequest(city_id=city.id, text=body.text, ward=body.ward, lat=body.lat, lon=body.lon, category=cat,
                       category_confidence=conf, classified_by=by, department=department, provenance="operator")
    db.add(r)
    db.flush()
    audit(db, "service_request.create", principal, target_type="service_request", target_id=r.id, city_id=city.id)
    return sr_dict(r)

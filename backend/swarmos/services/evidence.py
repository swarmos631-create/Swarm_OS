"""Resolve incident evidence links to their current, city-scoped source records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from swarmos.core.errors import ApiError, bad_request, not_found
from swarmos.db import utcnow
from swarmos.models import (
    AgentExecution,
    AgentTask,
    City,
    DataSource,
    GeoFeature,
    IncidentEvidence,
    Measurement,
    ProposedAction,
    ServiceRequest,
)

EvidenceType = Literal["measurement", "agent_execution", "service_request", "feature", "proposed_action"]


def _freshness(observed_at: datetime | None, threshold_s: int | None) -> dict[str, Any] | None:
    if observed_at is None or threshold_s is None:
        return None
    age_s = max(0, int((utcnow() - observed_at).total_seconds()))
    return {"state": "fresh" if age_s <= threshold_s else "stale", "age_s": age_s, "threshold_s": threshold_s}


def resolve_source(db: Session, city_id: str, source_type: EvidenceType, source_id: str) -> dict[str, Any]:
    """Return a compact evidence snapshot; never resolve a record from another city."""
    if source_type == "measurement":
        try:
            row = db.get(Measurement, int(source_id))
        except ValueError:
            row = None
        if row is None or row.city_id != city_id:
            raise not_found("Evidence record")
        source = db.get(DataSource, row.source_id)
        return {
            "source_type": source_type, "source_id": str(row.id),
            "label": f"{row.metric} · {row.value:g} {row.unit}",
            "summary": f"{row.metric} measured/modelled at {row.entity}: {row.value:g} {row.unit}.",
            "source": source.name if source else row.source_id,
            "provenance": row.provenance, "observed_at": row.observed_at.isoformat(),
            "fetched_at": row.fetched_at.isoformat(),
            "freshness": _freshness(row.observed_at, source.freshness_threshold_s if source else None),
            "limitations": list(source.limitations or []) if source else ["Source registry metadata is unavailable."],
        }

    if source_type == "agent_execution":
        try:
            row = db.get(AgentExecution, int(source_id))
        except ValueError:
            row = None
        task = db.get(AgentTask, row.task_id) if row else None
        if row is None or task is None or task.city_id != city_id:
            raise not_found("Evidence record")
        output = row.output or {}
        refs = output.get("data_refs") or []
        provenance = output.get("provenance") or []
        return {
            "source_type": source_type, "source_id": str(row.id),
            "label": f"{row.agent_id} · {row.status}",
            "summary": str(output.get("summary") or row.error or f"Agent execution status: {row.status}")[:1000],
            "source": f"Agent task {task.id[:10]}", "provenance": provenance,
            "observed_at": (row.finished_at or row.started_at or task.created_at).isoformat(),
            "fetched_at": None, "freshness": None,
            "limitations": list(output.get("limitations") or []),
            "findings": output.get("findings") or [], "data_refs": refs,
        }

    if source_type == "service_request":
        row = db.get(ServiceRequest, source_id)
        if row is None or row.city_id != city_id:
            raise not_found("Evidence record")
        return {
            "source_type": source_type, "source_id": row.id,
            "label": f"Service request · {row.category or 'unclassified'} · {row.status}",
            "summary": f"{row.category or 'Unclassified'} request routed to {row.department or 'no department'}; status {row.status}.",
            "source": "Citizen/operator service request", "provenance": row.provenance,
            "observed_at": row.created_at.isoformat(), "fetched_at": None,
            "freshness": None, "limitations": ["A service request is a report and is not independently verified by its existence."],
        }

    if source_type == "feature":
        try:
            row = db.get(GeoFeature, int(source_id))
        except ValueError:
            row = None
        city = db.get(City, city_id)
        if row is None or city is None or row.city_id not in (None, city_id):
            raise not_found("Evidence record")
        if row.city_id is None:
            from swarmos.cities import haversine_km

            if haversine_km(city.lat, city.lon, row.lat, row.lon) > 800:
                raise not_found("Evidence record")
        source = db.get(DataSource, row.source_id)
        return {
            "source_type": source_type, "source_id": str(row.id),
            "label": row.name or f"{row.layer} feature", "summary": f"Mapped {row.layer} feature.",
            "source": source.name if source else row.source_id, "provenance": row.provenance,
            "observed_at": row.observed_at.isoformat() if row.observed_at else None,
            "fetched_at": row.fetched_at.isoformat(),
            "freshness": _freshness(row.observed_at, source.freshness_threshold_s if source else None),
            "limitations": list(source.limitations or []) if source else ["Feature is mapped data; coverage is incomplete."],
        }

    if source_type == "proposed_action":
        row = db.get(ProposedAction, source_id)
        if row is None or row.city_id != city_id:
            raise not_found("Evidence record")
        return {
            "source_type": source_type, "source_id": row.id,
            "label": f"{row.title} · {row.status}", "summary": row.rationale or row.title,
            "source": f"Proposed action from {row.agent_id}",
            "provenance": row.payload.get("_evidence", {}).get("provenance", []),
            "observed_at": row.created_at.isoformat(), "fetched_at": None,
            "freshness": None,
            "limitations": ["A proposed action is a recommendation, not evidence that an intervention occurred or worked."],
        }

    raise bad_request("Unsupported evidence source type")


def list_candidates(db: Session, city_id: str, source_type: EvidenceType) -> list[dict[str, Any]]:
    """Recent records that an operator can link, excluding already linked records only at the UI layer."""
    if source_type == "measurement":
        rows = db.scalars(select(Measurement).where(Measurement.city_id == city_id)
                          .order_by(Measurement.observed_at.desc()).limit(30)).all()
        ids = [str(row.id) for row in rows]
    elif source_type == "agent_execution":
        rows = db.scalars(select(AgentExecution).join(AgentTask, AgentTask.id == AgentExecution.task_id)
                          .where(AgentTask.city_id == city_id)
                          .order_by(AgentExecution.finished_at.desc(), AgentExecution.id.desc()).limit(30)).all()
        ids = [str(row.id) for row in rows]
    elif source_type == "service_request":
        rows = db.scalars(select(ServiceRequest).where(ServiceRequest.city_id == city_id)
                          .order_by(ServiceRequest.created_at.desc()).limit(30)).all()
        ids = [row.id for row in rows]
    elif source_type == "feature":
        city = db.get(City, city_id)
        if city is None:
            return []
        rows = db.scalars(select(GeoFeature).where((GeoFeature.city_id == city_id) | GeoFeature.city_id.is_(None))
                          .order_by(GeoFeature.fetched_at.desc()).limit(100)).all()
        from swarmos.cities import haversine_km

        rows = [row for row in rows if row.city_id == city_id or haversine_km(city.lat, city.lon, row.lat, row.lon) <= 800][:30]
        ids = [str(row.id) for row in rows]
    else:
        rows = db.scalars(select(ProposedAction).where(ProposedAction.city_id == city_id)
                          .order_by(ProposedAction.created_at.desc()).limit(30)).all()
        ids = [row.id for row in rows]
    return [resolve_source(db, city_id, source_type, row_id) for row_id in ids]


def evidence_dict(db: Session, city_id: str, link: IncidentEvidence) -> dict[str, Any]:
    source: dict[str, Any]
    try:
        source = resolve_source(db, city_id, link.source_type, link.source_id)  # type: ignore[arg-type]
    except ApiError:
        source = {"source_type": link.source_type, "source_id": link.source_id,
                  "label": "Source record unavailable", "summary": "The referenced record is no longer available.",
                  "source": None, "provenance": "unavailable", "observed_at": None, "fetched_at": None,
                  "freshness": None, "limitations": ["The original source record could not be loaded."]}
    return {"id": link.id, "relationship": link.relationship, "note": link.note,
            "created_at": link.created_at.isoformat(), **source}

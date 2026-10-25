"""Operator outcome records tied to approved actions and observed measurements."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from swarmos.api.common import iso
from swarmos.core.audit import audit
from swarmos.core.errors import bad_request, not_found
from swarmos.core.security import Principal, city_access, require_role
from swarmos.db import get_db, utcnow
from swarmos.models import City, InterventionOutcome, Measurement, ProposedAction

router = APIRouter(tags=["outcomes"])


class OutcomeIn(BaseModel):
    outcome: Literal["improved", "unchanged", "worsened", "unverified"]
    note: str = Field(min_length=12, max_length=2000)
    measurement_ids: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("note")
    @classmethod
    def _meaningful_note(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 12:
            raise ValueError("note must contain at least 12 non-space characters")
        return value

    @field_validator("measurement_ids")
    @classmethod
    def _unique_measurements(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("measurement_ids must be unique")
        return value


def outcome_dict(row: InterventionOutcome, db: Session) -> dict[str, Any]:
    measurements = db.scalars(select(Measurement).where(
        Measurement.city_id == row.city_id, Measurement.id.in_(row.measurement_ids or []),
        Measurement.kind == "observation", Measurement.provenance.not_in(("demo", "synthetic")),
    ).order_by(Measurement.observed_at.desc())).all() if row.measurement_ids else []
    action = db.get(ProposedAction, row.action_id)
    boundary = action.executed_at if action and action.status == "executed" else None
    changes = _observed_changes(measurements, boundary)
    return {
        "id": row.id, "action_id": row.action_id, "city_id": row.city_id, "outcome": row.outcome,
        "note": row.note, "measurement_ids": row.measurement_ids, "recorded_at": iso(row.recorded_at),
        "updated_at": iso(row.updated_at), "evidence": [{
            "id": measurement.id, "metric": measurement.metric, "entity": measurement.entity,
            "source_id": measurement.source_id, "value": measurement.value,
            "unit": measurement.unit, "kind": measurement.kind, "provenance": measurement.provenance,
            "observed_at": iso(measurement.observed_at), "fetched_at": iso(measurement.fetched_at),
        } for measurement in measurements],
        "comparison_boundary": iso(boundary) if boundary else None,
        "change_comparison_status": "paired_measurements_found" if changes else (
            "no_matched_measurements" if boundary else "no_recorded_execution_time"
        ),
        "observed_changes": changes,
        "comparison_method": (
            "Nearest linked non-demo observations before and after the recorded execution time, matched by metric, "
            "unit, entity and source. This is a descriptive comparison and does not estimate causal impact."
        ),
        "interpretation": "Operator-reported association; evidence does not establish that the action caused the change.",
    }


def _observed_changes(measurements: list[Measurement], boundary: Any) -> list[dict[str, Any]]:
    """Pair adjacent linked observations around execution, keeping series and providers separate."""
    if boundary is None:
        return []
    grouped: dict[tuple[str, str, str, str], list[Measurement]] = defaultdict(list)
    for measurement in measurements:
        key = (measurement.metric, measurement.unit, measurement.entity, measurement.source_id)
        grouped[key].append(measurement)

    changes = []
    for (metric, unit, entity, source_id), rows in sorted(grouped.items()):
        before_rows = [measurement for measurement in rows if measurement.observed_at <= boundary]
        after_rows = [measurement for measurement in rows if measurement.observed_at > boundary]
        if not before_rows or not after_rows:
            continue
        before = max(before_rows, key=lambda measurement: (measurement.observed_at, measurement.id))
        after = min(after_rows, key=lambda measurement: (measurement.observed_at, measurement.id))
        delta = after.value - before.value
        changes.append({
            "metric": metric,
            "unit": unit,
            "entity": entity,
            "source_id": source_id,
            "before": {"measurement_id": before.id, "value": before.value, "observed_at": iso(before.observed_at)},
            "after": {"measurement_id": after.id, "value": after.value, "observed_at": iso(after.observed_at)},
            "absolute_change": round(delta, 6),
            "relative_change_pct": round(delta / abs(before.value) * 100, 2) if before.value else None,
        })
    return changes


def _action(db: Session, city: City, action_id: str) -> ProposedAction:
    action = db.get(ProposedAction, action_id)
    if action is None or action.city_id != city.id:
        raise not_found("Action")
    return action


@router.get("/cities/{city_id}/outcome-measurements")
def outcome_measurements(limit: int = Query(30, ge=1, le=100), city: City = Depends(city_access),
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(Measurement).where(
        Measurement.city_id == city.id,
        Measurement.kind == "observation",
        Measurement.provenance.not_in(("demo", "synthetic")),
    ).order_by(Measurement.observed_at.desc()).limit(limit)).all()
    return {"items": [{"id": row.id, "metric": row.metric, "value": row.value, "unit": row.unit,
                        "provenance": row.provenance, "observed_at": iso(row.observed_at)} for row in rows]}


@router.get("/cities/{city_id}/actions/{action_id}/outcome")
def get_outcome(action_id: str, city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    _action(db, city, action_id)
    row = db.scalar(select(InterventionOutcome).where(InterventionOutcome.action_id == action_id))
    if row is None:
        raise not_found("Intervention outcome")
    return outcome_dict(row, db)


@router.put("/cities/{city_id}/actions/{action_id}/outcome")
def record_outcome(action_id: str, body: OutcomeIn, city: City = Depends(city_access),
                   principal: Principal = Depends(require_role("operator")),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    action = _action(db, city, action_id)
    if action.status not in {"executed", "awaiting_manual"}:
        raise bad_request("Record outcomes only after an action is executed or marked for manual execution")
    if body.measurement_ids:
        rows = db.scalars(select(Measurement).where(Measurement.city_id == city.id,
                                                     Measurement.id.in_(body.measurement_ids))).all()
        by_id = {row.id: row for row in rows}
        for measurement_id in body.measurement_ids:
            row = by_id.get(measurement_id)
            if row is None:
                raise not_found("Observed measurement")
            if row.kind != "observation" or row.provenance in {"demo", "synthetic"}:
                raise bad_request("Outcome evidence must reference non-demo observed measurements")
    record = db.scalar(select(InterventionOutcome).where(InterventionOutcome.action_id == action.id))
    event = "intervention_outcome.update" if record else "intervention_outcome.create"
    if record is None:
        record = InterventionOutcome(action_id=action.id, city_id=city.id, recorded_by=principal.id)
        db.add(record)
    record.outcome = body.outcome
    record.note = body.note
    record.measurement_ids = body.measurement_ids
    record.recorded_by = principal.id
    record.recorded_at = utcnow()
    record.updated_at = record.recorded_at
    db.flush()
    audit(db, event, principal, target_type="proposed_action", target_id=action.id, city_id=city.id,
          details={"outcome": body.outcome, "measurement_count": len(body.measurement_ids)})
    return outcome_dict(record, db)

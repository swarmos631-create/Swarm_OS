"""Source registry persistence, synchronisation and freshness/health computation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from swarmos.config import Settings, get_settings
from swarmos.db import session_scope, utcnow
from swarmos.integrations.adapters import ADAPTERS
from swarmos.integrations.base import IntegrationError, SyncPayload
from swarmos.models import City, DataSource, GeoFeature, Measurement, SyncRun

log = logging.getLogger("swarmos.sync")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def sync_registry(db: Session, settings: Settings) -> None:
    """Persist adapter specs into the data_sources table and compute configuration status."""
    for adapter in ADAPTERS.values():
        s = adapter.spec
        row = db.get(DataSource, s.id) or DataSource(id=s.id)
        row.name, row.kind, row.categories = s.name, s.kind, list(s.categories)
        row.coverage, row.auth_required, row.license = s.coverage, s.auth_required, s.license
        row.attribution, row.homepage = s.attribution, s.homepage
        row.freshness_threshold_s, row.limitations, row.setup = s.freshness_threshold_s, list(s.limitations), s.setup
        ok, reason = adapter.configured(settings)
        if s.kind == "fixture":
            row.status = "demo" if ok else "disabled"
        elif s.kind == "internal":
            row.status = "internal"
        elif not ok:
            row.status = "awaiting_access" if s.id == "imd" else "awaiting_credentials"
            row.status_detail = reason
        elif row.status in (None, "unknown", "awaiting_credentials", "awaiting_access", "disabled"):
            row.status = "configured"
            row.status_detail = "Configured; waiting for first synchronisation"
        row.enabled = ok
        db.merge(row)


def freshness(source: DataSource, last_success: datetime | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    last_success = _aware(last_success)
    if last_success is None:
        return {"state": "never", "age_s": None, "threshold_s": source.freshness_threshold_s}
    age = (now - last_success).total_seconds()
    return {
        "state": "fresh" if age <= source.freshness_threshold_s else "stale",
        "age_s": int(age),
        "threshold_s": source.freshness_threshold_s,
    }


def _upsert(db: Session, model: type, rows: list[dict[str, Any]], conflict_cols: list[str], update_cols: list[str]) -> None:
    if not rows:
        return
    dialect = db.get_bind().dialect.name
    ins = sqlite_insert if dialect == "sqlite" else pg_insert
    for i in range(0, len(rows), 500):
        stmt = ins(model).values(rows[i : i + 500])
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_cols, set_={c: getattr(stmt.excluded, c) for c in update_cols}
        )
        db.execute(stmt)


def persist_payload(db: Session, source_id: str, city: City | None, payload: SyncPayload, provenance: str = "live") -> int:
    now = utcnow()
    mrows = [
        {
            "city_id": city.id if city else None,
            "source_id": source_id,
            "category": m.category,
            "metric": m.metric,
            "entity": m.entity,
            "value": m.value,
            "unit": m.unit,
            "kind": m.kind,
            "observed_at": m.observed_at,
            "fetched_at": now,
            "provenance": provenance,
            "lat": m.lat,
            "lon": m.lon,
            "meta": m.meta,
        }
        for m in payload.measurements
    ]
    if mrows and city is None:
        raise ValueError("Measurements require a city")
    _upsert(db, Measurement, mrows, ["city_id", "source_id", "metric", "entity", "kind", "observed_at"],
            ["value", "unit", "fetched_at", "provenance", "meta", "category", "lat", "lon"])
    frows = [
        {
            "city_id": city.id if city else None,
            "source_id": source_id,
            "layer": f.layer,
            "external_id": f.external_id,
            "name": f.name,
            "lat": f.lat,
            "lon": f.lon,
            "properties": f.properties,
            "provenance": provenance,
            "observed_at": f.observed_at,
            "fetched_at": now,
        }
        for f in payload.features
    ]
    if city is None:
        # Global features have NULL city_id; NULLs never conflict in a unique index, so dedupe manually.
        existing: dict[str, int] = {
            r[0]: r[1]
            for r in db.execute(
                select(GeoFeature.external_id, GeoFeature.id).where(
                    GeoFeature.source_id == source_id, GeoFeature.city_id.is_(None)
                )
            )
        }
        for r in frows:
            if str(r["external_id"]) in existing:
                feat = db.get(GeoFeature, existing[str(r["external_id"])])
                assert feat is not None
                for k in ("name", "lat", "lon", "properties", "observed_at", "fetched_at", "layer"):
                    setattr(feat, k, r[k])
            else:
                db.add(GeoFeature(**r))
    else:
        _upsert(db, GeoFeature, frows, ["source_id", "external_id", "city_id"],
                ["name", "lat", "lon", "properties", "provenance", "observed_at", "fetched_at", "layer"])
        if provenance != "demo" and frows:
            # Real facilities replace the simulated placeholders for the same layer.
            layers = sorted({str(r["layer"]) for r in frows})
            db.execute(delete(GeoFeature).where(GeoFeature.city_id == city.id, GeoFeature.provenance == "demo",
                                                GeoFeature.layer.in_(layers)))
    return len(mrows) + len(frows)


async def run_sync(
    source_id: str,
    city_id: str | None = None,
    *,
    force: bool = False,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Synchronise one source (for one city when city-scoped). Never raises for upstream failures."""
    settings = settings or get_settings()
    adapter = ADAPTERS.get(source_id)
    if adapter is None:
        raise ValueError(f"Unknown source '{source_id}'")
    spec = adapter.spec
    ok, reason = adapter.configured(settings)
    if spec.scope == "none" or not ok:
        return {"status": "skipped", "reason": reason or "Source is not synchronisable"}

    with session_scope() as db:
        city = db.get(City, city_id) if city_id else None
        if spec.scope == "city" and city is None:
            raise ValueError("City-scoped source requires a valid city_id")
        if spec.scope == "global":
            city = None
        if not force:
            q = select(func.max(SyncRun.started_at)).where(SyncRun.source_id == source_id, SyncRun.status == "succeeded")
            q = q.where(SyncRun.city_id == city.id) if city else q.where(SyncRun.city_id.is_(None))
            last = _aware(db.scalar(q))
            if last and (utcnow() - last).total_seconds() < spec.min_sync_interval_s:
                return {"status": "skipped", "reason": "Synchronised recently (rate-limit protection)"}
        city_snapshot = City(id=city.id, name=city.name, lat=city.lat, lon=city.lon, zoom=city.zoom, bbox=city.bbox) if city else None

    started = utcnow()
    t0 = time.monotonic()
    owns_client = client is None
    client = client or httpx.AsyncClient(headers={"User-Agent": settings.http_user_agent}, follow_redirects=True)
    error: IntegrationError | None = None
    payload: SyncPayload | None = None
    try:
        payload = await adapter.fetch(client, settings, city_snapshot)
    except IntegrationError as exc:
        error = exc
    except Exception as exc:  # validation errors etc. are classified as schema problems
        log.exception("sync_unexpected_error", extra={"source": source_id})
        error = IntegrationError("schema", f"Unexpected response from upstream: {type(exc).__name__}")
    finally:
        if owns_client:
            await client.aclose()

    duration_ms = int((time.monotonic() - t0) * 1000)
    with session_scope() as db:
        src = db.get(DataSource, source_id)
        assert src is not None
        src.last_attempt_at = started
        records = 0
        if error is None and payload is not None:
            city_row = db.get(City, city_snapshot.id) if city_snapshot else None
            records = persist_payload(db, source_id, city_row, payload)
            src.last_success_at = utcnow()
            src.status, src.status_detail, src.consecutive_failures = "connected", None, 0
        else:
            assert error is not None
            src.last_error_at, src.last_error = utcnow(), f"[{error.kind}] {error}"
            src.consecutive_failures += 1
            src.status = "degraded" if src.last_success_at else "failing"
            src.status_detail = str(error)
        db.add(
            SyncRun(
                source_id=source_id, city_id=city_snapshot.id if city_snapshot else None,
                status="succeeded" if error is None else "failed", started_at=started, finished_at=utcnow(),
                duration_ms=duration_ms, records=records, error=None if error is None else f"[{error.kind}] {error}",
            )
        )
    log.info("sync_finished", extra={"source": source_id, "city": city_id, "ok": error is None, "records": records,
                                     "duration_ms": duration_ms})
    if error is not None:
        return {"status": "failed", "error_kind": error.kind, "error": str(error), "duration_ms": duration_ms}
    return {"status": "succeeded", "records": records, "duration_ms": duration_ms}


async def sync_city(city_id: str, sources: Iterable[str] | None = None, force: bool = False) -> dict[str, Any]:
    settings = get_settings()
    ids = list(sources or [s for s, a in ADAPTERS.items() if a.spec.scope in ("city", "global")])
    async with httpx.AsyncClient(headers={"User-Agent": settings.http_user_agent}, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(run_sync(s, city_id, force=force, client=client, settings=settings) for s in ids)
        )
    return dict(zip(ids, results, strict=True))


def city_source_status(db: Session, city_id: str) -> list[dict[str, Any]]:
    """Per-city view of every source: connection status, last sync for this city and freshness."""
    now = utcnow()
    out = []
    for src in db.scalars(select(DataSource).order_by(DataSource.id)):
        spec = ADAPTERS[src.id].spec if src.id in ADAPTERS else None
        scope = spec.scope if spec else "none"
        q = select(SyncRun).where(SyncRun.source_id == src.id)
        if scope == "city":
            q = q.where(SyncRun.city_id == city_id)
        last_run = db.scalars(q.order_by(SyncRun.started_at.desc()).limit(1)).first()
        last_ok = db.scalar(
            select(func.max(SyncRun.finished_at)).where(
                SyncRun.source_id == src.id, SyncRun.status == "succeeded",
                *( [SyncRun.city_id == city_id] if scope == "city" else [] ),
            )
        )
        applies = True
        if src.id == "swarmos-demo":
            city = db.get(City, city_id)
            settings = get_settings()
            applies = bool(city and src.enabled and settings.demo_mode and (city.is_pilot or settings.simulate_all_cities))
        out.append(
            {
                "id": src.id,
                "name": src.name,
                "kind": src.kind,
                "categories": src.categories,
                "status": src.status,
                "status_detail": src.status_detail,
                "applies_to_city": applies,
                "scope": scope,
                "last_run": None if last_run is None else {
                    "status": last_run.status, "started_at": _aware(last_run.started_at),
                    "records": last_run.records, "error": last_run.error, "duration_ms": last_run.duration_ms,
                },
                "freshness": freshness(src, last_ok, now) if scope != "none" else None,
                "attribution": src.attribution,
            }
        )
    return out


def stale_cutoff(hours: float) -> datetime:
    return utcnow() - timedelta(hours=hours)

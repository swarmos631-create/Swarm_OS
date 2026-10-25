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
from swarmos.models import City, DataSource, GeoFeature, Incident, IncidentEvent, Measurement, SyncRun

log = logging.getLogger("swarmos.sync")


def http_client(settings: Settings) -> httpx.AsyncClient:
    """HTTP client for every upstream sync (tests replace this to stay offline)."""
    return httpx.AsyncClient(headers={"User-Agent": settings.http_user_agent}, follow_redirects=True)


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
        if ok and not s.scheduled and row.status in ("configured", "failing", "degraded"):
            row.status = "on_demand"
            row.status_detail = "Not synchronised automatically; run it from this page when needed"
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


FACILITY_SOURCE_RANK = {"mappls-places": 3, "osm-vector-tiles": 2, "osm-overpass": 1}


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
        rank = FACILITY_SOURCE_RANK.get(source_id)
        if rank is not None and frows:
            # One facility source per layer, so a hospital is not counted twice: MapmyIndia > OSM tiles > Overpass.
            higher = [sid for sid, r in FACILITY_SOURCE_RANK.items() if r > rank]
            taken = set(db.scalars(select(GeoFeature.layer).where(GeoFeature.city_id == city.id,
                                                                   GeoFeature.source_id.in_(higher)).distinct()))
            frows = [r for r in frows if r["layer"] not in taken]
            lower = [sid for sid, r in FACILITY_SOURCE_RANK.items() if r < rank]
            mine = sorted({str(r["layer"]) for r in frows})
            if mine and lower:
                db.execute(delete(GeoFeature).where(GeoFeature.city_id == city.id, GeoFeature.source_id.in_(lower),
                                                    GeoFeature.layer.in_(mine)))
        _upsert(db, GeoFeature, frows, ["source_id", "external_id", "city_id"],
                ["name", "lat", "lon", "properties", "provenance", "observed_at", "fetched_at", "layer"])
        if provenance != "demo" and frows:
            # Real facilities replace the simulated placeholders for the same layer.
            layers = sorted({str(r["layer"]) for r in frows})
            db.execute(delete(GeoFeature).where(GeoFeature.city_id == city.id, GeoFeature.provenance == "demo",
                                                GeoFeature.layer.in_(layers)))
    n_inc = _persist_incidents(db, source_id, city, payload) if city is not None and payload.incidents is not None else 0
    return len(mrows) + len(frows) + n_inc


def _persist_incidents(db: Session, source_id: str, city: City, payload: SyncPayload) -> int:
    """Upsert official alerts as live incidents; ones that expired or left the feed are resolved."""
    assert payload.incidents is not None
    now = utcnow()
    prefix = f"{source_id}:"
    existing = {i.source: i for i in db.scalars(select(Incident).where(Incident.city_id == city.id,
                                                                        Incident.source.like(f"{prefix}%")))}
    current: set[str] = set()
    for a in payload.incidents:
        key = (prefix + a.external_id)[:80]
        current.add(key)
        expired = a.expires_at is not None and a.expires_at <= now
        inc = existing.get(key)
        if inc is None:
            inc = Incident(city_id=city.id, title=a.title[:200], description=a.description, category=a.category,
                           severity=a.severity, status="resolved" if expired else "open", lat=a.lat, lon=a.lon,
                           department=a.department, provenance="live", source=key, reported_at=a.reported_at,
                           resolved_at=a.expires_at if expired else None)
            db.add(inc)
            db.flush()
            db.add(IncidentEvent(incident_id=inc.id, kind="created", to_status=inc.status,
                                 note=f"Official alert from {source_id}", created_at=a.reported_at))
        else:
            inc.title, inc.description, inc.severity = a.title[:200], a.description, a.severity
            if expired and inc.status not in ("resolved", "closed"):
                inc.status, inc.resolved_at = "resolved", a.expires_at
    for key, inc in existing.items():
        if key not in current and inc.status not in ("resolved", "closed"):
            inc.status, inc.resolved_at = "resolved", now  # no longer in the official feed
    return len(payload.incidents)


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
    if spec.scope == "batch":
        return await run_batch_sync(source_id, force=force, client=client, settings=settings)

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
    client = client or http_client(settings)
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
            src.status = ("degraded" if src.last_success_at else "failing") if spec.scheduled else "on_demand"
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


def _snapshot(c: City) -> City:
    return City(id=c.id, name=c.name, state=c.state, lat=c.lat, lon=c.lon, zoom=c.zoom, bbox=c.bbox, population=c.population,
                aliases=list(c.aliases or []))


async def run_batch_sync(source_id: str, *, force: bool = False, client: httpx.AsyncClient | None = None,
                         settings: Settings | None = None) -> dict[str, Any]:
    """Synchronise a batch source for every city in the catalogue, `batch_size` cities per upstream call.

    Each city gets its own SyncRun row, so per-city status and freshness work exactly as for city-scoped sources.
    Payloads are stored chunk by chunk so memory stays flat however many cities there are.
    """
    settings = settings or get_settings()
    adapter = ADAPTERS[source_id]
    spec = adapter.spec
    with session_scope() as db:
        if not force:
            last = _aware(db.scalar(select(func.max(SyncRun.started_at)).where(SyncRun.source_id == source_id,
                                                                            SyncRun.status == "succeeded")))
            if last and (utcnow() - last).total_seconds() < spec.min_sync_interval_s:
                return {"status": "skipped", "reason": "Synchronised recently (rate-limit protection)"}
        cities = [_snapshot(c) for c in db.scalars(select(City).where(City.is_active).order_by(City.id))]
        if spec.fallback_for:
            cutoff = utcnow() - timedelta(seconds=spec.fallback_after_s)
            covered = set(db.scalars(select(SyncRun.city_id).where(
                SyncRun.source_id == spec.fallback_for, SyncRun.status == "succeeded", SyncRun.started_at >= cutoff)))
            cities = [c for c in cities if c.id not in covered]
    if not cities:
        return {"status": "skipped", "reason": f"{spec.fallback_for} covers every city"}

    owns_client = client is None
    client = client or http_client(settings)
    ok_cities, failed, records, last_error = 0, 0, 0, None
    t_all = time.monotonic()
    try:
        for i in range(0, len(cities), spec.batch_size):
            chunk = cities[i : i + spec.batch_size]
            started, t0 = utcnow(), time.monotonic()
            error: IntegrationError | None = None
            payloads: dict[str, SyncPayload] = {}
            try:
                payloads = await adapter.fetch_batch(client, settings, chunk)
            except IntegrationError as exc:
                error = exc
            except Exception as exc:
                log.exception("sync_unexpected_error", extra={"source": source_id})
                error = IntegrationError("schema", f"Unexpected response from upstream: {type(exc).__name__}")
            duration_ms = int((time.monotonic() - t0) * 1000)
            with session_scope() as db:
                for c in chunk:
                    payload = payloads.get(c.id)
                    n = 0
                    if payload is not None:
                        row = db.get(City, c.id)
                        n = persist_payload(db, source_id, row, payload) if row else 0
                        records += n
                        ok_cities += 1
                    else:
                        failed += 1
                    err = None if payload is not None else f"[{(error.kind if error else 'schema')}] {error or 'No data returned for this city'}"
                    db.add(SyncRun(source_id=source_id, city_id=c.id, status="succeeded" if payload is not None else "failed",
                                   started_at=started, finished_at=utcnow(), duration_ms=duration_ms, records=n, error=err))
            if error is not None:
                last_error = error
    finally:
        if owns_client:
            await client.aclose()

    with session_scope() as db:
        src = db.get(DataSource, source_id)
        assert src is not None
        src.last_attempt_at = utcnow()
        if ok_cities:
            src.last_success_at = utcnow()
            src.consecutive_failures = 0
            src.status = "connected" if not failed else "degraded"
            src.status_detail = None if not failed else f"{failed} of {len(cities)} cities failed: {last_error}"
        else:
            src.consecutive_failures += 1
            src.last_error_at, src.last_error = utcnow(), f"[{last_error.kind if last_error else 'schema'}] {last_error}"
            src.status = "degraded" if src.last_success_at else "failing"
            src.status_detail = str(last_error)
    log.info("batch_sync_finished", extra={"source": source_id, "cities": len(cities), "ok": ok_cities, "failed": failed,
                                           "records": records, "duration_ms": int((time.monotonic() - t_all) * 1000)})
    status = "succeeded" if ok_cities and not failed else "partial" if ok_cities else "failed"
    return {"status": status, "cities": len(cities), "succeeded": ok_cities, "failed": failed, "records": records,
            **({"error": str(last_error)} if last_error and not ok_cities else {})}


def prune_measurements(retention_days: int) -> int:
    """Delete live readings older than the retention window (keeps the free database small). Simulated series are
    left alone: they are regenerated as a whole by the simulator and include multi-year history (e.g. land cover)."""
    with session_scope() as db:
        res = db.execute(delete(Measurement).where(Measurement.observed_at < utcnow() - timedelta(days=retention_days),
                                                   Measurement.provenance.not_in(("demo", "synthetic"))))
        return int(getattr(res, "rowcount", 0) or 0)


async def sync_city(city_id: str, sources: Iterable[str] | None = None, force: bool = False) -> dict[str, Any]:
    settings = get_settings()
    ids = list(sources or [s for s, a in ADAPTERS.items() if a.spec.scope in ("city", "global")])
    async with http_client(settings) as client:
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
        if scope in ("city", "batch"):
            q = q.where(SyncRun.city_id == city_id)
        last_run = db.scalars(q.order_by(SyncRun.started_at.desc()).limit(1)).first()
        last_ok = db.scalar(
            select(func.max(SyncRun.finished_at)).where(
                SyncRun.source_id == src.id, SyncRun.status == "succeeded",
                *( [SyncRun.city_id == city_id] if scope in ("city", "batch") else [] ),
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
                "last_success_at": _aware(last_ok),
                "freshness": freshness(src, last_ok, now) if scope != "none" else None,
                "attribution": src.attribution,
            }
        )
    return out


def stale_cutoff(hours: float) -> datetime:
    return utcnow() - timedelta(hours=hours)

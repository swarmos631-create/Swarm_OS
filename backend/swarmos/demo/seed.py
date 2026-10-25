"""Development bootstrap: catalog, source registry, dev accounts, sample documents and simulated city data.

Every simulated record is tagged ``provenance='demo'`` (shown as "Simulated") and comes from the ``swarmos-demo``
source. It is generated when SWARMOS_DEMO_MODE=true: for the pilot cities at bootstrap, and for any other city the
first time it is opened when SWARMOS_SIMULATE_ALL_CITIES=true. Time series are refreshed so they always end at the
current hour; real feeds always take precedence over simulated values for the same metric.
"""

from __future__ import annotations

import logging
import os
import random
import secrets
import string
import threading
import time
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from swarmos.ai.retrieval import ingest
from swarmos.cities import sync_catalog
from swarmos.config import REPO_ROOT, Settings
from swarmos.core.security import hash_password
from swarmos.db import utcnow
from swarmos.demo import simulate
from swarmos.demo.landmask import Placer, land_mask
from swarmos.integrations.base import NormalizedFeature, NormalizedMeasurement, SyncPayload
from swarmos.integrations.sync import persist_payload, sync_registry
from swarmos.ml import complaints, synthetic
from swarmos.ml.features import floor_hour
from swarmos.models import (
    City,
    GeoFeature,
    Incident,
    IncidentEvent,
    Measurement,
    ServiceRequest,
    SyncRun,
    User,
    UserCity,
)

log = logging.getLogger("swarmos.simulate")
DEMO = "swarmos-demo"
# Bump when the layout of simulated static records changes; older cities are regenerated on their next visit.
SIM_VERSION = 2
SCALE = {"bengaluru": 1.0, "mumbai": 1.2, "delhi": 1.7}
DEV_USERS = [
    ("admin@swarmos.local", "Dev Admin", "admin", True),
    ("operator@swarmos.local", "Dev Operator", "operator", True),
    ("viewer@swarmos.local", "Dev Viewer", "viewer", False),
]


def _gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(20))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def seed_users(db: Session, settings: Settings) -> Path | None:
    """Create development accounts once. The password is never logged; it is written to a 0600 file."""
    if settings.env == "production":
        raise RuntimeError("Refusing to create development accounts in production. Use 'swarmos create-user'.")
    if db.scalar(select(User).limit(1)):
        return None
    password = settings.dev_seed_password or _gen_password()
    for email, name, role, all_cities in DEV_USERS:
        u = User(email=email, display_name=name, role=role, all_cities=all_cities, password_hash=hash_password(password))
        if not all_cities:
            u.cities = [UserCity(city_id=c) for c in settings.demo_pilot_cities]
        db.add(u)
    if settings.dev_seed_password:
        return None
    path = REPO_ROOT / "var" / "dev-credentials.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SwarmOS development accounts (local only — do not commit)\n"
                    + "".join(f"{e} ({r}): {password}\n" for e, _, r, _ in DEV_USERS))
    os.chmod(path, 0o600)
    return path


def seed_documents(db: Session) -> int:
    """Sample SOPs plus reference summaries of public Indian guidance. Idempotent: ingest skips known content."""
    n = 0
    folder = resources.files("swarmos.demo").joinpath("documents")
    meta = {"incident_escalation_sop.md": "internal"}
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".md"):
            continue
        text = entry.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        ingest(db, title=title, text=text, source=f"swarmos sample: {entry.name}", classification=meta.get(entry.name, "public"),
               license="CC0-1.0 (SwarmOS sample)", city_id=None, uploaded_by=None, is_synthetic=True)
        n += 1
    reference = folder.joinpath("reference")
    for entry in sorted(reference.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".md"):
            continue
        text = entry.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        ingest(db, title=title, text=text, source=f"SwarmOS reference summary: {entry.name} (see the official source linked inside)",
               classification="public", license="CC0-1.0 (summary written by SwarmOS; not the official text)",
               city_id=None, uploaded_by=None, is_synthetic=False)
        n += 1
    return n


def _scale(city: City) -> float:
    return SCALE.get(city.id) or max(0.25, min(2.0, (city.population or 3_000_000) / 8_000_000))


def seed_series(db: Session, city: City, now: datetime | None = None) -> int:
    """(Re)generate every simulated time series for one city so it ends at the current hour."""
    rng = random.Random(f"demo:{city.id}")
    seed = sum(ord(c) for c in city.id)
    scale = _scale(city)
    now = floor_hour(now or utcnow())
    start14 = now - timedelta(hours=24 * 14 - 1)
    db.execute(delete(Measurement).where(Measurement.city_id == city.id, Measurement.source_id == DEMO))
    # Sensor positions: stable per city, inside its bounds and never on water.
    placer = Placer(city, random.Random(f"place-series:{city.id}"), land_mask(city))

    m: list[NormalizedMeasurement] = []

    def add_series(category: str, metric: str, unit: str, times, vals, entity: str = "city", lat=None, lon=None, meta=None):  # type: ignore[no-untyped-def]
        for t, v in zip(times, vals, strict=True):
            m.append(NormalizedMeasurement(category=category, metric=metric, value=round(float(v), 4), unit=unit, observed_at=t,
                                           entity=entity, lat=lat, lon=lon, meta=meta or {}))

    # Traffic corridors (generic names — synthetic data must not be attributed to real roads)
    for i, label in enumerate(["Corridor A (north arterial)", "Corridor B (east arterial)", "Corridor C (ring road segment)",
                               "Corridor D (CBD approach)", "Corridor E (south arterial)"]):
        free = rng.uniform(38, 55)
        t, v = synthetic.corridor_speed(start14, 24 * 14, seed + i, freeflow=free)
        if i == 3:
            v[-3:] *= 0.45  # injected current congestion
        lat, lon = placer.point(0.18)
        add_series("traffic", "speed_kmph", "km/h", t, v, entity=label, lat=lat, lon=lon)
        add_series("traffic", "freeflow_speed_kmph", "km/h", [now], [free], entity=label, lat=lat, lon=lon, meta={"lat": lat, "lon": lon})

    # Water: city demand, DMAs (one leak), quality sites (one exceedance)
    t, v = synthetic.water_demand(start14, 24 * 14, seed + 10, base_m3h=55000 * scale)
    add_series("water", "water_demand_m3h", "m³/h", t, v)
    for i in range(4):
        leak_from = 24 * 12 if i == 2 else None
        t, v = synthetic.dma_flow(start14, 24 * 14, seed + 20 + i, base=rng.uniform(220, 420), leak_from_hour=leak_from, leak_m3h=120)
        lat, lon = placer.point(0.18)
        add_series("water", "flow_m3h", "m³/h", t, v, entity=f"DMA-{i + 1:02d}", lat=lat, lon=lon)
    t48 = [now - timedelta(hours=h) for h in range(47, -1, -1)]
    for i, site in enumerate(["Treatment plant outlet", "Reservoir R2", "Distribution node N7"]):
        lat, lon = placer.point(0.18)
        ph = [rng.uniform(7.0, 7.8) for _ in t48]
        turb = [rng.uniform(0.3, 0.9) for _ in t48]
        cl = [rng.uniform(0.3, 0.8) for _ in t48]
        if i == 2:
            turb[-4:] = [6.2, 6.8, 7.1, 6.5]
            cl[-4:] = [0.12, 0.1, 0.11, 0.09]
        add_series("water_quality", "ph", "pH", t48, ph, entity=site, lat=lat, lon=lon)
        add_series("water_quality", "turbidity_ntu", "NTU", t48, turb, entity=site, lat=lat, lon=lon)
        add_series("water_quality", "residual_chlorine_mgl", "mg/L", t48, cl, entity=site, lat=lat, lon=lon)

    # Energy
    t, v = synthetic.energy_demand(start14, 24 * 14, seed + 30, base_mw=3000 * scale)
    add_series("energy", "power_demand_mw", "MW", t, v)
    t24 = [now - timedelta(hours=h) for h in range(23, -1, -1)]
    freq = [50 + rng.gauss(0, 0.02) for _ in t24]
    freq[-6] = 49.82
    add_series("energy", "grid_frequency_hz", "Hz", t24, freq)
    for i, f in enumerate(["Feeder-A", "Feeder-B", "Feeder-C", "Feeder-D"]):
        volt = [1 + rng.gauss(0, 0.01) for _ in t24]
        load = [rng.uniform(50, 85) for _ in t24]
        if i == 1:
            volt[-1], load[-1] = 0.91, 108
        add_series("energy", "feeder_voltage_pu", "pu", t24, volt, entity=f)
        add_series("energy", "feeder_load_pct", "%", t24, load, entity=f)

    # Wastewater
    for i, cap in enumerate([150, 90, 60]):
        stp = f"STP-{i + 1}"
        util = [1.08] * 24 if i == 2 else [rng.uniform(0.6, 0.9) for _ in t24]
        add_series("wastewater", "stp_inflow_mld", "MLD", t24, [cap * u for u in util], entity=stp)
        add_series("wastewater", "stp_capacity_mld", "MLD", [now], [cap], entity=stp)
        add_series("wastewater", "effluent_bod_mgl", "mg/L", [now], [26 if i == 1 else rng.uniform(8, 18)], entity=stp)

    # Budget (₹ crore) and field resources
    for dept, alloc in [("Roads & Infrastructure", 1200), ("Solid Waste Management", 650), ("Storm Water Drains", 420),
                        ("Water Supply", 900), ("Health", 380), ("Electrical", 210)]:
        a = alloc * scale
        add_series("budget", "budget_allocated_cr", "₹ crore", [now], [a], entity=dept)
        add_series("budget", "budget_spent_cr", "₹ crore", [now], [a * rng.uniform(0.25, 1.08)], entity=dept)
    for dept in ["Fire & Emergency", "Health", "Storm Water Drains", "Water Supply", "Electrical", "Traffic Police", "Engineering",
                 "Solid Waste Management"]:
        add_series("resources", "available_crews", "crews", [now], [rng.randint(1, 4)], entity=dept)

    m.extend(simulate.environment(city, now))
    m.extend(simulate.land_use_and_growth(city, now))
    m.extend(simulate.health_aggregates(city, now))
    return persist_payload(db, DEMO, city, SyncPayload(measurements=m), provenance="demo")


def seed_static(db: Session, city: City, *, reset: bool = False) -> dict[str, int]:
    """Assets, facilities, incidents and service requests — created once; operators may edit them afterwards."""
    if reset:
        db.execute(delete(GeoFeature).where(GeoFeature.city_id == city.id, GeoFeature.source_id == DEMO))
        db.execute(delete(Incident).where(Incident.city_id == city.id, Incident.provenance == "demo"))
        db.execute(delete(ServiceRequest).where(ServiceRequest.city_id == city.id, ServiceRequest.provenance == "demo"))
    elif db.scalar(select(func.count(Incident.id)).where(Incident.city_id == city.id, Incident.provenance == "demo")):
        return {"records": 0, "incidents": 0, "service_requests": 0}
    rng = random.Random(f"demo-static:{city.id}")
    seed = sum(ord(c) for c in city.id)
    now = floor_hour(utcnow())
    # Every simulated location lies inside this city's bounds and on land (sea, lakes and rivers are excluded).
    placer = Placer(city, random.Random(f"place-static:{city.id}"), land_mask(city))
    feats = []
    for i in range(12):
        layer = "asset_bridge" if i < 4 else "asset_road"
        lat, lon = placer.point(0.18)
        feats.append(NormalizedFeature(layer=layer, external_id=f"demo-asset-{i}", name=f"{'Flyover' if i < 4 else 'Road segment'} {city.name} {i + 1}",
                                       lat=lat, lon=lon, properties={"condition_index": rng.randint(15, 95), "sim_v": SIM_VERSION,
                                                                     "last_inspected": f"202{rng.randint(2, 5)}-0{rng.randint(1, 9)}-15"}))
    real_layers = set(db.scalars(select(GeoFeature.layer).where(GeoFeature.city_id == city.id, GeoFeature.provenance != "demo").distinct()))
    feats.extend(f for f in simulate.facilities(city, place=placer.point) if f.layer not in real_layers)
    n = persist_payload(db, DEMO, city, SyncPayload(features=feats), provenance="demo")

    # Incidents
    templates = [("Fallen tree blocking carriageway", "road", "high"), ("Waterlogging at underpass", "flooding", "high"),
                 ("Transformer fire reported", "fire", "critical"), ("Multi-vehicle collision", "road", "high"),
                 ("Power outage affecting ward", "energy", "moderate"), ("Water main burst", "water", "high"),
                 ("Garbage accumulation near market", "garbage", "low"), ("Building wall collapse risk", "structural", "critical"),
                 ("Traffic signal malfunction", "mobility", "moderate"), ("Open excavation without barricade", "construction", "moderate"),
                 ("Medical emergency at bus station", "medical", "high"), ("Sewage overflow on street", "water", "moderate")]
    templates += [("Pothole cluster on main road", "road", "moderate"), ("Streetlights not working", "energy", "low"),
                  ("Stray cattle on highway", "road", "moderate"), ("Gas leak smell reported", "fire", "critical"),
                  ("Illegal dumping of debris", "garbage", "low"), ("Tree branch on power line", "energy", "high")]
    # Each city gets its own mix: shuffled templates, city-sized count, its own ward numbers and severities.
    rng.shuffle(templates)
    lakhs = max(2.0, (city.population or 1_000_000) / 1e5)
    wards = int(min(250, max(20, lakhs * 1.5)))
    incidents = 0
    for i in range(int(min(26, max(10, 8 + lakhs / 8 + rng.randint(-3, 3))))):
        title, cat, sev = templates[i % len(templates)]
        if rng.random() < 0.3:
            sev = rng.choice(["low", "moderate", "high", "critical"])
        lat, lon = placer.point(0.15)
        reported = now - timedelta(hours=rng.uniform(0.3, 70))
        status = rng.choice(["open", "open", "acknowledged", "in_progress", "resolved"])
        inc = Incident(city_id=city.id, title=f"{title} — Ward {rng.randint(1, wards)}", description=f"Simulated incident in {city.name} (generated data).", category=cat,
                       severity=sev, status=status, lat=lat, lon=lon, provenance="demo", source=DEMO, reported_at=reported,
                       resolved_at=reported + timedelta(hours=rng.uniform(1, 8)) if status == "resolved" else None)
        db.add(inc)
        db.flush()
        db.add(IncidentEvent(incident_id=inc.id, kind="created", to_status="open", note="Simulated record", created_at=reported))
        incidents += 1
    # Correlated cluster (three incidents close in space and time)
    clat, clon = placer.point(0.1)
    cluster_ward = rng.randint(1, wards)
    clusters = [[("Waterlogging on arterial road", "flooding", "high"), ("Vehicle stalled in flood water", "road", "moderate"),
                 ("Drain overflow into homes", "flooding", "high")],
                [("Power cut across neighbourhood", "energy", "high"), ("Traffic signals dark at junction", "mobility", "moderate"),
                 ("Water pumping stopped", "water", "high")],
                [("Fire in market building", "fire", "critical"), ("Smoke reported by residents", "fire", "high"),
                 ("Road blocked by fire tenders", "road", "moderate")]]
    for j, (title, cat, sev) in enumerate(rng.choice(clusters)):
        lat, lon = placer.near(clat, clon, 300)
        db.add(Incident(city_id=city.id, title=f"{title} — Ward {cluster_ward}", description=f"Simulated incident in {city.name} (generated data).",
                        category=cat, severity=sev, status="open", lat=lat, lon=lon, provenance="demo", source=DEMO,
                        reported_at=now - timedelta(hours=1 + j * 0.5)))
        incidents += 1

    # Service requests
    rows = complaints.synthesize(140, seed=seed)
    for r in rows:
        created = now - timedelta(hours=rng.uniform(1, 24 * 45))
        resolved = None
        if rng.random() < 0.6:
            resolved = created + timedelta(hours=rng.uniform(4, 24 * 10))
            if resolved > now:
                resolved = None
        lat, lon = placer.point(0.22)
        db.add(ServiceRequest(city_id=city.id, text=r["text"], category=r["category"], classified_by="demo-label",
                              status="resolved" if resolved else "open", ward=f"Ward {rng.randint(1, wards)}", lat=lat, lon=lon,
                              provenance="demo", created_at=created, resolved_at=resolved))
    return {"records": n, "incidents": incidents, "service_requests": len(rows)}


def seed_demo_city(db: Session, city: City) -> dict[str, int]:
    """Full reset of one city's simulated data (bootstrap and tests)."""
    out = seed_static(db, city, reset=True)
    out["records"] += seed_series(db, city)
    return out


def simulation_enabled(settings: Settings, city: City) -> bool:
    return settings.demo_mode and (city.is_pilot or settings.simulate_all_cities)


_locks: dict[str, threading.Lock] = {}
_checked: dict[str, float] = {}
_guard = threading.Lock()


def _latest_demo(db: Session, city: City) -> datetime | None:
    return db.scalar(select(func.max(Measurement.observed_at)).where(
        Measurement.city_id == city.id, Measurement.source_id == DEMO, Measurement.metric == "speed_kmph"))


def ensure_city_data(db: Session, settings: Settings, city: City) -> None:
    """Make sure a city has simulated data that reaches the current hour.

    No data yet: generated synchronously (the first visit to a city takes a few seconds). Behind the current hour:
    refreshed in a background thread so the request is not held up. Checks are throttled per city.
    """
    clock = time.monotonic()
    if clock - _checked.get(city.id, -1e9) < 60:
        return
    _queue_real_places(db, settings, city)
    if not simulation_enabled(settings, city):
        _checked[city.id] = clock
        return
    latest = _latest_demo(db, city)
    now = floor_hour(utcnow())
    with _guard:
        lock = _locks.setdefault(city.id, threading.Lock())
    # Throttle only once the city has data: concurrent first-visit requests (dashboard, map layers, features…) must
    # all wait for the data being generated, not answer from an empty city.
    if latest is not None:
        _checked[city.id] = clock
    if latest is not None and _layout_outdated(db, city) and not lock.locked():
        # Simulated records from an older layout (same incident list in every city, points on water): rebuild them.
        threading.Thread(target=_relayout_in_background, args=(city.id, lock), daemon=True).start()
        return
    if latest is not None:
        latest = latest if latest.tzinfo else latest.replace(tzinfo=now.tzinfo)
        if now - latest < timedelta(hours=1):
            return  # forecasting agents need history up to the current hour
    if latest is None:
        with lock:
            if _latest_demo(db, city) is None:
                seed_static(db, city)
                seed_series(db, city)
                db.commit()
                log.info("simulated_city_seeded", extra={"city": city.id})
            else:
                db.expire_all()  # another request just generated it: read the new rows
        _checked[city.id] = time.monotonic()
        return
    if lock.locked():
        return
    threading.Thread(target=_refresh_in_background, args=(city.id, lock), daemon=True).start()


def _layout_outdated(db: Session, city: City) -> bool:
    asset = db.scalars(select(GeoFeature).where(GeoFeature.city_id == city.id, GeoFeature.source_id == DEMO,
                                                GeoFeature.layer == "asset_bridge").limit(1)).first()
    return asset is not None and (asset.properties or {}).get("sim_v") != SIM_VERSION


def _relayout_in_background(city_id: str, lock: threading.Lock) -> None:
    from swarmos.db import session_scope

    if not lock.acquire(blocking=False):
        return
    try:
        with session_scope() as db:
            city = db.get(City, city_id)
            if city is not None:
                seed_static(db, city, reset=True)
                seed_series(db, city)
        log.info("simulated_city_relayout", extra={"city": city_id, "version": SIM_VERSION})
    except Exception:  # pragma: no cover - logged, retried on the next check
        log.exception("simulated_city_relayout_failed", extra={"city": city_id})
    finally:
        lock.release()


def _queue_real_places(db: Session, settings: Settings, city: City) -> None:
    """Queue the city-level live sources (facilities, CPCB stations, traffic, …) when a city is opened.

    Each configured city-scoped source is synced at most once per its minimum interval; weather and air quality
    come from batch sources that already cover every city on the schedule.
    """
    from swarmos.integrations.adapters import ADAPTERS
    from swarmos.jobs import queue

    try:
        for sid, adapter in ADAPTERS.items():
            spec = adapter.spec
            if spec.scope != "city" or not spec.scheduled or not adapter.configured(settings)[0]:
                continue
            recent = db.scalar(select(func.count(SyncRun.id)).where(
                SyncRun.source_id == sid, SyncRun.city_id == city.id, SyncRun.status == "succeeded",
                SyncRun.started_at >= utcnow() - timedelta(seconds=spec.min_sync_interval_s)))
            if recent:
                continue
            queue.enqueue(db, "sync", {"source_id": sid, "city_id": city.id},
                          dedupe_key=f"visit:{sid}:{city.id}:{utcnow():%Y%m%d}", priority=6, max_attempts=2)
        db.commit()
    except Exception:  # pragma: no cover - best effort; the scheduled sync still runs
        db.rollback()
        log.exception("visit_enqueue_failed", extra={"city": city.id})


def _refresh_in_background(city_id: str, lock: threading.Lock) -> None:
    from swarmos.db import session_scope

    if not lock.acquire(blocking=False):
        return
    try:
        with session_scope() as db:
            city = db.get(City, city_id)
            if city is not None:
                seed_series(db, city)
        log.info("simulated_city_refreshed", extra={"city": city_id})
    except Exception:  # pragma: no cover - logged, retried on the next check
        log.exception("simulated_city_refresh_failed", extra={"city": city_id})
    finally:
        lock.release()


def _database_bytes(db: Session) -> int | None:
    if db.get_bind().dialect.name != "postgresql":
        return None
    return int(db.scalar(text("SELECT pg_database_size(current_database())")) or 0)


def prebuild_cities(settings: Settings, stop: threading.Event | None = None, pause_s: float = 0.5,
                    city_ids: list[str] | None = None) -> int:
    """Generate simulated data for every city that has none yet, so no visitor waits for it on a first visit.

    Runs in a background thread after startup: pilot cities first, then the largest cities. Each city takes a few
    seconds (land/water mask download + about 8,000 records); the per-city lock is shared with ensure_city_data, so a
    visitor opening a city that is being built just waits for that one city.
    """
    from swarmos.db import session_scope

    built = 0
    with session_scope() as db:
        cities = [c for c in db.scalars(select(City).where(City.is_active.is_(True))) if city_ids is None or c.id in city_ids]
        order = [c.id for c in sorted(cities, key=lambda c: (not c.is_pilot, -(c.population or 0), c.name))
                 if simulation_enabled(settings, c)]
    budget = settings.prebuild_db_budget_mb * 1024 * 1024
    for city_id in order:
        if stop is not None and stop.is_set():
            break
        with _guard:
            lock = _locks.setdefault(city_id, threading.Lock())
        try:
            with lock, session_scope() as db:
                city = db.get(City, city_id)
                if city is None or _latest_demo(db, city) is not None:
                    continue
                size = _database_bytes(db)
                if size is not None and size >= budget:
                    log.info("simulated_cities_prebuild_budget_reached",
                             extra={"built": built, "database_mb": round(size / 1048576), "budget_mb": settings.prebuild_db_budget_mb})
                    break
                t0 = time.monotonic()
                seed_static(db, city)
                seed_series(db, city)
                db.commit()
                built += 1
                log.info("simulated_city_prebuilt", extra={"city": city_id, "seconds": round(time.monotonic() - t0, 1)})
        except Exception as exc:  # pragma: no cover - logged; the city is then built on its first visit instead
            log.error("simulated_city_prebuild_failed", extra={"city": city_id, "error": str(exc).splitlines()[0][:200]})
            if "space" in str(exc).lower() or "DiskFull" in type(getattr(exc, "orig", exc)).__name__:
                break  # out of disk: stop rather than fail for every remaining city
        time.sleep(pause_s)  # leave room for requests and the job worker between cities
    log.info("simulated_cities_prebuild_finished", extra={"built": built, "cities": len(order)})
    return built


def bootstrap(db: Session, settings: Settings, *, demo: bool | None = None) -> dict[str, object]:
    out: dict[str, object] = {"cities": sync_catalog(db, settings.demo_pilot_cities)}
    db.flush()
    sync_registry(db, settings)
    db.flush()
    cred = seed_users(db, settings) if settings.env != "production" else None
    out["credentials_file"] = str(cred) if cred else None
    out["documents"] = seed_documents(db)
    if (settings.demo_mode if demo is None else demo):
        out["demo"] = {c: seed_demo_city(db, db.get(City, c)) for c in settings.demo_pilot_cities if db.get(City, c)}  # type: ignore[arg-type]
    return out

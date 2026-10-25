"""City catalog, command-center dashboard, map layers and time series (all city-scoped)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swarmos.agents.skills.common import NAQI_AVERAGING_H, mean, naqi
from swarmos.api.common import iso
from swarmos.config import get_settings
from swarmos.core.errors import bad_request
from swarmos.core.security import Principal, city_access, current_principal
from swarmos.db import get_db, utcnow
from swarmos.integrations.sync import city_source_status
from swarmos.models import (
    AgentExecution,
    AgentTask,
    AuditLog,
    City,
    GeoFeature,
    Incident,
    Measurement,
    Notification,
    ServiceRequest,
)
from swarmos.services.data import CityData, Series

router = APIRouter(tags=["cities"])

SEVERITIES = ("critical", "high", "moderate", "low")


def city_dict(c: City, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "state": c.state, "lat": c.lat, "lon": c.lon, "timezone": c.timezone, "zoom": c.zoom,
            "bbox": c.bbox, "bbox_note": "Approximate viewport, not an administrative boundary", "population": c.population,
            "population_source": c.population_source, "aliases": c.aliases, "is_pilot": c.is_pilot, **(extra or {})}


@router.get("/cities")
def list_cities(q: str | None = Query(None, max_length=80), principal: Principal = Depends(current_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(City).where(City.is_active.is_(True)).order_by(City.is_pilot.desc(), City.name)
    cities = [c for c in db.scalars(stmt) if principal.can_access_city(c.id)]
    if q:
        ql = q.strip().lower()
        cities = [c for c in cities if ql in c.name.lower() or ql in c.state.lower() or any(ql in a.lower() for a in c.aliases)]
    live = set(db.scalars(select(Measurement.city_id).where(Measurement.provenance == "live").distinct()))
    settings = get_settings()
    return {"items": [city_dict(c, {"has_live_data": c.id in live, "has_demo_data": settings.demo_mode and (c.is_pilot or settings.simulate_all_cities)}) for c in cities], "total": len(cities)}


@router.get("/cities/{city_id}")
def get_city(city: City = Depends(city_access)) -> dict[str, Any]:
    return city_dict(city)


def _indicator(id_: str, label: str, m: Measurement | None, *, unit: str | None = None, value: Any = None,
               domain: str, note: str | None = None, missing: str | None = None) -> dict[str, Any]:
    if m is None and value is None:
        return {"id": id_, "label": label, "domain": domain, "status": "unavailable", "value": None, "unit": unit,
                "note": missing or "No connected data source for this city"}
    v = value if value is not None else m.value  # type: ignore[union-attr]
    if isinstance(v, float):
        v = round(v, 2)
    return {"id": id_, "label": label, "domain": domain, "status": "available", "value": v,
            "unit": unit or (m.unit if m else None), "provenance": m.provenance if m else "derived",
            "kind": m.kind if m else "derived", "observed_at": iso(m.observed_at) if m else None,
            "fetched_at": iso(m.fetched_at) if m else None, "source_id": m.source_id if m else None, "note": note}


def _aqi(data: CityData) -> dict[str, Any] | None:
    conc: dict[str, float] = {}
    last: Series | None = None
    for p, h in NAQI_AVERAGING_H.items():
        ser = data.one_series(p, hours_back=h, category="air_quality")
        if ser and len(ser.points) >= max(4, h // 2):
            v = mean(ser.values)
            if v is not None:
                conc[p] = v / 1000 if p == "carbon_monoxide" else v
                last = ser
    res = naqi(conc) if conc else None
    if res and last:
        return {**res, "provenance": last.provenance, "observed_at": last.points[-1].t.isoformat(), "fetched_at": iso(last.fetched_at),
                "source_id": last.source_id}
    return None


@router.get("/cities/{city_id}/dashboard")
def dashboard(city: City = Depends(city_access), principal: Principal = Depends(current_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    now = utcnow()
    data = CityData(db, city, now)
    ind = []
    ind.append(_indicator("temperature", "Temperature", data.latest("temperature_2m", category="weather"), domain="environment",
                          note="Model analysis (Open-Meteo), not a station reading"))
    ind.append(_indicator("feels_like", "Feels like", data.latest("apparent_temperature", category="weather"), domain="environment"))
    fc = data.one_series("precipitation", hours_back=0, hours_forward=24, kinds=("forecast",), category="weather")
    ind.append({**_indicator("rain_24h", "Rain next 24 h", None, value=round(sum(fc.values), 1) if fc else None, unit="mm",
                             domain="environment", note="Forecast"), **({"provenance": fc.provenance, "kind": "forecast",
                                                                         "fetched_at": iso(fc.fetched_at), "source_id": fc.source_id} if fc else {})})
    aqi = _aqi(data)
    ind.append({"id": "aqi", "label": "Indicative AQI (NAQI)", "domain": "environment",
                **({"status": "available", "value": aqi["aqi"], "unit": aqi["category"], "provenance": aqi["provenance"],
                    "kind": "model_analysis", "observed_at": aqi["observed_at"], "fetched_at": aqi["fetched_at"], "source_id": aqi["source_id"],
                    "note": f"Dominant {aqi['dominant']}; from CAMS model concentrations, not CPCB stations"} if aqi else
                   {"status": "unavailable", "value": None, "note": "No air-quality data synchronised"})})
    corridors = data.entities("traffic", hours_back=6)
    if corridors:
        ratios: list[float] = []
        last = None
        for e in corridors:
            s = data.latest("speed_kmph", entity=e, category="traffic")
            f = data.latest("freeflow_speed_kmph", entity=e, category="traffic")
            if s and f and f.value:
                ratios.append(max(0.0, 1 - s.value / f.value))
                last = s
        ind.append(_indicator("congestion", "Congestion index", last, value=round(sum(ratios) / len(ratios), 2) if ratios else None,
                              unit="0–1", domain="mobility"))
    else:
        ind.append(_indicator("congestion", "Congestion index", None, domain="mobility", missing="No traffic feed connected"))
    ind.append(_indicator("power_demand", "Power demand", data.latest("power_demand_mw", category="energy"), domain="energy",
                          missing="No DISCOM/SLDC feed connected"))
    ind.append(_indicator("water_demand", "Water demand", data.latest("water_demand_m3h", category="water"), domain="water",
                          missing="No water utility telemetry connected"))

    open_q = select(Incident).where(Incident.city_id == city.id, Incident.status.in_(("open", "acknowledged", "in_progress")))
    open_incs = list(db.scalars(open_q.order_by(Incident.reported_at.desc())))
    by_sev = {s: sum(1 for i in open_incs if i.severity == s) for s in SEVERITIES}
    sr_open = db.scalar(select(func.count(ServiceRequest.id)).where(ServiceRequest.city_id == city.id, ServiceRequest.status == "open")) or 0
    ind.append({"id": "open_incidents", "label": "Open incidents", "domain": "safety", "status": "available", "value": len(open_incs),
                "unit": f"{by_sev['critical']} critical", "provenance": "mixed" if len({i.provenance for i in open_incs}) > 1 else
                (open_incs[0].provenance if open_incs else "operator"), "kind": "records"})
    ind.append({"id": "open_requests", "label": "Open service requests", "domain": "civic", "status": "available", "value": sr_open,
                "unit": "requests", "provenance": "records", "kind": "records"})
    counts = data.feature_counts()
    quakes = data.earthquakes_near(500, 7)
    usgs_ok = db.scalar(select(func.max(GeoFeature.fetched_at)).where(GeoFeature.source_id == "usgs-earthquakes"))
    ind.append({"id": "earthquakes", "label": "Earthquakes ≤500 km (7 d)", "domain": "safety",
                **({"status": "available", "value": len(quakes), "unit": f"max M{max(q[0].properties.get('magnitude') or 0 for q in quakes):.1f}" if quakes else "none M≥2.5",
                    "provenance": "live", "kind": "observation", "fetched_at": iso(usgs_ok), "source_id": "usgs-earthquakes",
                    "note": "USGS real-time feed; NCS India is the authoritative national source"} if usgs_ok else
                   {"status": "unavailable", "value": None, "note": "USGS feed not synchronised yet"})})

    tasks = list(db.scalars(select(AgentTask).where(AgentTask.city_id == city.id).order_by(AgentTask.created_at.desc()).limit(6)))
    exec_stats = dict(db.execute(
        select(AgentExecution.status, func.count(AgentExecution.id)).join(AgentTask, AgentTask.id == AgentExecution.task_id)
        .where(AgentTask.city_id == city.id, AgentTask.created_at >= now - timedelta(hours=24)).group_by(AgentExecution.status)).all())

    trends: dict[str, Any] = {}
    for key, metric, cat in (("temperature", "temperature_2m", "weather"), ("pm2_5", "pm2_5", "air_quality"),
                             ("power_demand", "power_demand_mw", "energy"), ("water_demand", "water_demand_m3h", "water")):
        ts = data.one_series(metric, hours_back=48, category=cat)
        trends[key] = None if ts is None else {"unit": ts.unit, "provenance": ts.provenance, "kind": ts.kind, "source_id": ts.source_id,
                                               "points": [[p.t.isoformat(), round(p.v, 2)] for p in ts.points[-48:]]}
    notes = list(db.scalars(select(Notification).where((Notification.city_id == city.id) | Notification.city_id.is_(None))
                            .order_by(Notification.created_at.desc()).limit(8)))
    audit_rows = []
    if principal.has_role("operator"):
        audit_rows = [{"at": iso(a.at), "actor": a.actor_email, "action": a.action, "target": a.target_id, "outcome": a.outcome}
                      for a in db.scalars(select(AuditLog).where(AuditLog.city_id == city.id).order_by(AuditLog.at.desc()).limit(8))]
    sources = city_source_status(db, city.id)
    return {
        "city": city_dict(city),
        "generated_at": now.isoformat(),
        "indicators": ind,
        "incidents": {"open": len(open_incs), "by_severity": by_sev,
                      "recent": [{"id": i.id, "title": i.title, "severity": i.severity, "status": i.status, "category": i.category,
                                  "reported_at": iso(i.reported_at), "provenance": i.provenance} for i in open_incs[:8]]},
        "infrastructure": {"osm_feature_counts": {k: v for k, v in counts.items() if not k.startswith("asset_")},
                           "earthquakes_7d_500km": len(quakes),
                           "strongest_quake": None if not quakes else {
                               "magnitude": max(q[0].properties.get("magnitude") or 0 for q in quakes)}},
        "agents": {"recent_tasks": [{"id": t.id, "prompt": t.prompt[:140], "status": t.status, "created_at": iso(t.created_at)} for t in tasks],
                   "executions_24h": exec_stats},
        "trends": trends,
        "alerts": [{"id": n.id, "level": n.level, "title": n.title, "body": n.body, "created_at": iso(n.created_at), "link": n.link} for n in notes],
        "source_health": sources,
        "sync_failures": [s for s in sources if s["last_run"] and s["last_run"]["status"] == "failed"],
        "audit": audit_rows,
    }


LAYER_META = {
    "incidents": ("Incidents", "civic"), "earthquake": ("Earthquakes ≤800 km (USGS, 7 days)", "safety"), "hospital": ("Hospitals", "health"),
    "clinic": ("Clinics", "health"), "fire_station": ("Fire stations", "safety"), "police": ("Police", "safety"),
    "school": ("Schools", "community"), "bus_station": ("Bus stations", "mobility"), "rail_station": ("Rail stations", "mobility"),
    "water_tower": ("Water towers", "water"), "substation": ("Substations", "energy"), "public_toilet": ("Public toilets", "community"),
    "asset_bridge": ("Flyovers/bridges", "infrastructure"), "asset_road": ("Road segments", "infrastructure"),
    "traffic_sensors": ("Traffic corridors", "mobility"), "water_sensors": ("Water telemetry", "water"),
}


@router.get("/cities/{city_id}/map/layers")
def map_layers(city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    data = CityData(db, city)
    out = []
    n_inc = db.scalar(select(func.count(Incident.id)).where(Incident.city_id == city.id, Incident.status.in_(("open", "acknowledged", "in_progress"))))
    out.append({"id": "incidents", "label": LAYER_META["incidents"][0], "group": "civic", "count": n_inc, "provenance": ["operator", "demo"]})
    provs = dict(db.execute(select(GeoFeature.layer, func.min(GeoFeature.provenance)).where(GeoFeature.city_id == city.id).group_by(GeoFeature.layer)).all())
    for layer, n in sorted(data.feature_counts().items()):
        label, group = LAYER_META.get(layer, (layer, "other"))
        out.append({"id": layer, "label": label, "group": group, "count": n, "provenance": [provs.get(layer) or "unknown"]})
    out.append({"id": "earthquake", "label": LAYER_META["earthquake"][0], "group": "safety", "count": len(data.earthquakes_near(800, 7)),
                "provenance": ["live"]})
    t = data.entities("traffic", hours_back=6)
    if t:
        out.append({"id": "traffic_sensors", "label": "Traffic corridors", "group": "mobility", "count": len(t), "provenance": ["demo"]})
    w = data.entities("water", hours_back=24) + data.entities("water_quality", hours_back=24)
    w = [e for e in w if e != "city"]
    if w:
        out.append({"id": "water_sensors", "label": "Water telemetry", "group": "water", "count": len(w), "provenance": ["demo"]})
    return {"layers": out, "note": "Only layers with synchronised data are listed. Base map tiles carry no sensor data."}


def _feature(lon: float, lat: float, props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


@router.get("/cities/{city_id}/map/features")
def map_features(layers: str = Query(..., max_length=400), city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    wanted = [x for x in layers.split(",") if x]
    unknown = [x for x in wanted if x not in LAYER_META]
    if unknown:
        raise bad_request(f"Unknown layers: {unknown}")
    data = CityData(db, city)
    feats: list[dict[str, Any]] = []
    if "incidents" in wanted:
        for i in data.incidents(days=30):
            if i.lat is not None and i.lon is not None:
                feats.append(_feature(i.lon, i.lat, {"layer": "incidents", "id": i.id, "title": i.title, "severity": i.severity,
                                                     "status": i.status, "category": i.category, "provenance": i.provenance,
                                                     "reported_at": iso(i.reported_at)}))
    if "earthquake" in wanted:
        for f, d in data.earthquakes_near(800, 7):
            feats.append(_feature(f.lon, f.lat, {"layer": "earthquake", "title": f.name, "magnitude": f.properties.get("magnitude"),
                                                 "distance_km": round(d, 1), "observed_at": iso(f.observed_at), "provenance": f.provenance,
                                                 "url": f.properties.get("url")}))
    geo_layers = [x for x in wanted if x not in ("incidents", "earthquake", "traffic_sensors", "water_sensors")]
    if geo_layers:
        for f in data.features(geo_layers):
            feats.append(_feature(f.lon, f.lat, {"layer": f.layer, "title": f.name or LAYER_META.get(f.layer, (f.layer,))[0],
                                                 "provenance": f.provenance, "source_id": f.source_id, "fetched_at": iso(f.fetched_at),
                                                 **{k: v for k, v in f.properties.items() if isinstance(v, str | int | float)}}))
    if "traffic_sensors" in wanted:
        for e in data.entities("traffic", hours_back=6):
            s = data.latest("speed_kmph", entity=e, category="traffic")
            fr = data.latest("freeflow_speed_kmph", entity=e, category="traffic")
            if s and s.lat is not None and s.lon is not None:
                feats.append(_feature(s.lon, s.lat, {"layer": "traffic_sensors", "title": e, "speed_kmph": round(s.value, 1),
                                                     "congestion_index": round(max(0, 1 - s.value / fr.value), 2) if fr else None,
                                                     "observed_at": iso(s.observed_at), "provenance": s.provenance}))
    if "water_sensors" in wanted:
        for cat, metric in (("water", "flow_m3h"), ("water_quality", "turbidity_ntu")):
            for e in data.entities(cat, hours_back=24):
                m = data.latest(metric, entity=e, category=cat)
                if m and m.lat is not None and m.lon is not None:
                    feats.append(_feature(m.lon, m.lat, {"layer": "water_sensors", "title": e, "metric": metric, "value": round(m.value, 2),
                                                         "unit": m.unit, "observed_at": iso(m.observed_at), "provenance": m.provenance}))
    return {"type": "FeatureCollection", "features": feats}


@router.get("/cities/{city_id}/series")
def series(metric: str = Query(..., max_length=64), hours: int = Query(48, ge=1, le=24 * 30), forward: int = Query(0, ge=0, le=96),
           entity: str = Query("city", max_length=120), city: City = Depends(city_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    data = CityData(db, city)
    kinds = ("observation", "model_analysis", "forecast") if forward else ("observation", "model_analysis")
    out = data.series(metric, hours_back=hours, hours_forward=forward, kinds=kinds, entity=entity)
    return {"metric": metric, "entity": entity, "series": [
        {**s.ref(), "unit": s.unit, "points": [[p.t.isoformat(), round(p.v, 3)] for p in s.points]} for s in out]}

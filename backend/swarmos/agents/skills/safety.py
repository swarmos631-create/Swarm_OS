"""Public safety & emergency response skills."""

from __future__ import annotations

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import (
    SEVERITY_WEIGHT,
    age_hours,
    feature_ref,
    finding,
    layer_ref,
    ok,
    records_ref,
    unavailable,
)
from swarmos.cities import haversine_km

CRITICAL_CATEGORIES = {"fire": 1.5, "flooding": 1.3, "structural": 1.4, "medical": 1.4, "hazmat": 1.6}


def _fmt_age(hours: float) -> str:
    return f"{hours * 60:.0f} min" if hours < 2 else f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


def _priority(ctx: AgentContext, inc) -> float:  # type: ignore[no-untyped-def]
    age = age_hours(ctx.now, inc.reported_at)
    unacked = 1.0 if inc.status == "open" else 0.4
    return SEVERITY_WEIGHT[inc.severity] * 20 * CRITICAL_CATEGORIES.get(inc.category, 1.0) + min(age, 12) * 2 * unacked


def emergency_triage(ctx: AgentContext) -> AgentResult:
    incs = ctx.data.incidents()
    if not incs:
        return ok("No open incidents.", confidence=0.85, method="rules:weighted_priority", provenance={"operator"},
                  metrics={"open": 0, "ranked": [], "critical": 0}, extra_refs=[records_ref("incidents", 0, ["operator"])])
    ranked = sorted(incs, key=lambda i: -_priority(ctx, i))
    actions = [
        ActionDraft(kind="escalate_incident", title=f"Acknowledge and dispatch: {i.title}",
                    rationale=f"{i.severity} {i.category} incident unacknowledged for {_fmt_age(age_hours(ctx.now, i.reported_at))}.",
                    payload={"incident_id": i.id, "to_status": "acknowledged"},
                    risk="moderate" if i.severity == "critical" else "low")
        for i in ranked if i.status == "open" and i.severity in ("high", "critical") and age_hours(ctx.now, i.reported_at) >= 0.5
    ][:6]
    critical = sum(1 for i in incs if i.severity == "critical")
    return ok(f"{len(incs)} open incidents ({critical} critical). Highest priority: {ranked[0].title}.", confidence=0.8,
              method="rules:weighted_priority", actions=actions,
              findings=[finding(i.title, f"{i.category} · {i.severity} · {i.status}", i.severity, i.lat, i.lon) for i in ranked[:6]],
              metrics={"open": len(incs), "critical": critical,
                       "ranked": [{"id": i.id, "title": i.title, "category": i.category, "severity": i.severity, "status": i.status,
                                   "lat": i.lat, "lon": i.lon, "priority": round(_priority(ctx, i), 1)} for i in ranked[:15]]},
              extra_refs=[records_ref("incidents", len(incs), [i.provenance for i in incs])], provenance={i.provenance for i in incs})


def incident_correlation(ctx: AgentContext) -> AgentResult:
    incs = [i for i in ctx.data.incidents(days=3) if i.lat is not None and i.lon is not None]
    if len(incs) < 2:
        return ok("Too few geolocated incidents to correlate.", confidence=0.7, method="spatiotemporal:union_find",
                  metrics={"clusters": []}, provenance={i.provenance for i in incs} or {"operator"})
    radius = float(ctx.spec.config.get("radius_km", 1.0))
    hours = float(ctx.spec.config.get("window_h", 6))
    parent = list(range(len(incs)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(incs)):
        for b in range(a + 1, len(incs)):
            ia, ib = incs[a], incs[b]
            dt = abs(age_hours(ctx.now, ia.reported_at) - age_hours(ctx.now, ib.reported_at))
            if dt <= hours and haversine_km(ia.lat, ia.lon, ib.lat, ib.lon) <= radius:  # type: ignore[arg-type]
                parent[find(a)] = find(b)
    groups: dict[int, list] = {}
    for i, inc in enumerate(incs):
        groups.setdefault(find(i), []).append(inc)
    clusters = [g for g in groups.values() if len(g) >= 2]
    clusters.sort(key=lambda g: -len(g))
    out = []
    findings = []
    for g in clusters:
        lat = sum(i.lat for i in g) / len(g)
        lon = sum(i.lon for i in g) / len(g)
        cats = sorted({i.category for i in g})
        out.append({"size": len(g), "categories": cats, "lat": round(lat, 5), "lon": round(lon, 5), "incident_ids": [i.id for i in g]})
        findings.append(finding(f"{len(g)} related incidents within {radius:g} km", f"Categories: {', '.join(cats)}",
                                "high" if len(g) >= 4 else "moderate", lat, lon))
    return ok(f"{len(clusters)} spatio-temporal incident cluster(s) found among {len(incs)} geolocated incidents.", confidence=0.65,
              method=f"spatiotemporal:union_find(r={radius}km,Δt={hours}h)", findings=findings, metrics={"clusters": out},
              extra_refs=[records_ref("incidents", len(incs), [i.provenance for i in incs])], provenance={i.provenance for i in incs},
              limitations=["Proximity suggests, but does not prove, a common cause."])


def resource_coordinator(ctx: AgentContext) -> AgentResult:
    triage = ctx.up("emergency_triage")
    if triage is None:
        return unavailable(["emergency triage results"], "Resource coordination needs triaged incidents.")
    facilities = ctx.data.features(["hospital", "fire_station", "police"])
    targets = [r for r in triage.metrics.get("ranked", []) if r["severity"] in ("high", "critical") and r.get("lat") is not None][:5]
    if not targets:
        return ok("No high-severity geolocated incidents need resource coordination.", confidence=0.75,
                  method="spatial:nearest_facility", metrics={"assignments": []}, provenance=set(triage.provenance))
    if not facilities:
        return AgentResult(status="degraded", summary="Incidents need coordination but no facility locations are synchronised (run the MapmyIndia or OpenStreetMap sync).",
                           confidence=0.3, method="none", provenance=triage.provenance,
                           missing=["hospital / fire station / police locations (MapmyIndia or OpenStreetMap)"],
                           metrics={"assignments": []})
    assignments, actions = [], []
    need = {"fire": ["fire_station", "hospital"], "medical": ["hospital"], "structural": ["fire_station", "hospital", "police"],
            "flooding": ["fire_station", "police"]}
    for t in targets:
        row = {"incident_id": t["id"], "title": t["title"], "nearest": {}}
        for layer in need.get(t["category"], ["police", "hospital"]):
            cands = [(haversine_km(t["lat"], t["lon"], f.lat, f.lon), f) for f in facilities if f.layer == layer]
            if cands:
                d, f = min(cands, key=lambda x: x[0])
                row["nearest"][layer] = {"name": f.name or f"Unnamed {layer}", "distance_km": round(d, 2), "lat": f.lat, "lon": f.lon}
        assignments.append(row)
        if row["nearest"]:
            desc = ", ".join(f"{k.replace('_', ' ')}: {v['name']} ({v['distance_km']} km)" for k, v in row["nearest"].items())
            actions.append(ActionDraft(kind="manual", title=f"Coordinate response for '{t['title']}'",
                                       rationale=f"Nearest mapped facilities — {desc}.",
                                       payload={"incident_id": t["id"], "facilities": row["nearest"]},
                                       risk="moderate" if t["severity"] == "critical" else "low"))
    counts = {layer: sum(1 for f in facilities if f.layer == layer) for layer in ("hospital", "fire_station", "police")}
    return ok(f"Nearest-facility plans prepared for {len(assignments)} high-severity incident(s).", confidence=0.6,
              method="spatial:nearest_facility_haversine", actions=actions, metrics={"assignments": assignments, "facility_counts": counts},
              extra_refs=[layer_ref(k, facilities) for k in counts],
              provenance=set(triage.provenance) | {f.provenance for f in facilities},
              limitations=["Straight-line distance, not road travel time; facility availability/capacity unknown."])


def disaster_impact(ctx: AgentContext) -> AgentResult:
    quakes = ctx.data.earthquakes_near(radius_km=float(ctx.spec.config.get("radius_km", 500)))
    weather = ctx.up("weather_impact_agent")
    flood = ctx.up("flood_risk_analyst")
    hazards, findings = [], []
    for f, d in quakes[:10]:
        mag = float(f.properties.get("magnitude") or 0)
        sig = mag >= 5 and d <= 300 or mag >= 6.5
        hazards.append({"type": "earthquake", "magnitude": mag, "distance_km": round(d, 1), "place": f.properties.get("place"),
                        "at": f.observed_at.isoformat() if f.observed_at else None, "significant": sig})
        if sig:
            findings.append(finding(f"M{mag:.1f} earthquake {d:.0f} km away", str(f.properties.get("place")), "high", f.lat, f.lon))
    if weather and weather.metrics.get("rain_24h_mm", 0) >= 115.6:
        hazards.append({"type": "extreme_rainfall", "rain_24h_mm": weather.metrics["rain_24h_mm"]})
        findings.append(finding("Very heavy rainfall hazard", f"{weather.metrics['rain_24h_mm']} mm forecast in 24 h.", "high"))
    if flood and flood.metrics.get("level") in ("elevated", "severe"):
        hazards.append({"type": "flooding", "index": flood.metrics.get("score")})
    # Official alerts (NDMA SACHET, GDACS) synchronised as live incidents for this city.
    alerts = [i for i in ctx.data.incidents(days=3) if i.provenance == "live" and ":" in (i.source or "")]
    for a in alerts[:10]:
        hazards.append({"type": "official_alert", "category": a.category, "severity": a.severity, "title": a.title,
                        "source": a.source.split(":", 1)[0], "at": a.reported_at.isoformat() if a.reported_at else None})
        if a.severity in ("high", "critical"):
            findings.append(finding(a.title, a.description[:300], a.severity, a.lat, a.lon))
    prov = {"live"} if quakes or alerts else set()
    prov |= set(weather.provenance) if weather else set()
    if not quakes and not weather and not flood and not alerts:
        return unavailable(["seismic feed or weather hazard assessment"], "No hazard data available for impact analysis.")
    lead = f"{len(alerts)} official alert(s) in force. " if alerts else ""
    return ok(f"{lead}{len(hazards)} hazard signal(s); {len(findings)} significant.", confidence=0.6, method="rules:hazard_screening",
              findings=findings, metrics={"hazards": hazards, "earthquakes_7d_500km": len(quakes), "official_alerts": len(alerts)},
              provenance=prov,
              extra_refs=[feature_ref("earthquake", len(quakes), "live", "usgs-earthquakes")],
              limitations=["Screening only — no exposure/vulnerability model (buildings, population) is applied."])


def safety_summarizer(ctx: AgentContext) -> AgentResult:
    present: dict[str, AgentResult] = {
        k: r for k in ("emergency_triage", "incident_correlation_agent", "disaster_impact_analyst") if (r := ctx.up(k)) is not None}
    if not present:
        return unavailable(["upstream safety analyses"], "No public-safety analyses completed.")
    tri = present.get("emergency_triage")
    cor = present.get("incident_correlation_agent")
    dis = present.get("disaster_impact_analyst")
    lines = []
    if tri:
        lines.append(f"{tri.metrics.get('open', 0)} open incidents, {tri.metrics.get('critical', 0)} critical.")
    if cor:
        lines.append(f"{len(cor.metrics.get('clusters', []))} incident cluster(s).")
    if dis:
        lines.append(f"{len(dis.metrics.get('hazards', []))} hazard signal(s).")
    text = " ".join(lines)
    method = "template"
    narrative = ctx.models.generate_text(
        "Summarise for a city duty officer in 3 sentences, using only these facts:\n" + text,
        system="You summarise structured facts. Never add facts that are not provided.", max_tokens=160)
    if narrative:
        text, method = narrative[0].strip(), f"local-llm:{narrative[1]}"
    sev_rank = {"critical": 4, "high": 3, "moderate": 2, "low": 1, "info": 0}
    worst = max((f.severity for r in present.values() for f in r.findings), key=lambda s: sev_rank[s], default="info")
    prov = set().union(*(set(r.provenance) for r in present.values()))
    return ok(text, confidence=min(r.confidence or 0.5 for r in present.values()), method=method, provenance=prov,
              metrics={"overall_severity": worst, "inputs": list(present)},
              findings=[f for r in present.values() for f in r.findings if f.severity in ("high", "critical")][:6])

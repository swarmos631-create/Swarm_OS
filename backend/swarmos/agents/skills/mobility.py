"""Mobility & traffic skills."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import (
    SEVERITY_WEIGHT,
    age_hours,
    finding,
    forecast_next_24h,
    ist,
    layer_ref,
    ok,
    records_ref,
    source_phrase,
    unavailable,
)
from swarmos.ml.features import floor_hour
from swarmos.services.data import Point, Series

MOBILITY_CATEGORIES = ("mobility", "road")


def _corridors(ctx: AgentContext) -> list[tuple[str, Series, float, dict]]:
    out = []
    for entity in ctx.data.entities("traffic"):
        speed = ctx.data.one_series("speed_kmph", entity=entity, hours_back=6)
        free = ctx.data.latest("freeflow_speed_kmph", entity=entity)
        if speed and speed.points and free and free.value > 0:
            out.append((entity, speed, free.value, free.meta or {}))
    return out


def traffic_flow(ctx: AgentContext) -> AgentResult:
    corridors = _corridors(ctx)
    if not corridors:
        return unavailable(["traffic sensor feed (corridor speeds)"],
                           f"No traffic speed feed is connected for {ctx.city.name}.",
                           ["Connect an authorised traffic data provider or municipal ATCS feed."])
    rows: list[dict[str, Any]] = []
    for entity, speed, free, meta in corridors:
        latest = speed.points[-1].v
        idx = max(0.0, 1 - latest / free)
        rows.append({"corridor": entity, "speed_kmph": round(latest, 1), "freeflow_kmph": round(free, 1),
                     "congestion_index": round(idx, 2), "lat": meta.get("lat"), "lon": meta.get("lon")})
    rows.sort(key=lambda r: -r["congestion_index"])
    city_idx = sum(r["congestion_index"] for r in rows) / len(rows)
    findings = [
        finding(f"Heavy congestion on {r['corridor']}",
                f"{r['speed_kmph']} km/h vs free-flow {r['freeflow_kmph']} km/h (index {r['congestion_index']}).",
                "high" if r["congestion_index"] >= 0.65 else "moderate", r["lat"], r["lon"])
        for r in rows if r["congestion_index"] >= 0.5
    ]
    return ok(
        f"City congestion index {city_idx:.2f} across {len(rows)} corridors; {len(findings)} heavily congested.",
        confidence=0.75, method="rules:speed_ratio", series=[c[1] for c in corridors],
        findings=findings, metrics={"city_congestion_index": round(city_idx, 2), "corridors": rows},
    )


def congestion_forecast(ctx: AgentContext) -> AgentResult:
    corridors = ctx.data.entities("traffic", hours_back=24 * 15, live_first=False)
    if not corridors:
        return unavailable(["traffic speed history"], "No traffic history to forecast from.")
    # City-level mean speed per hour across corridors
    buckets: dict = defaultdict(list)
    series_used = []
    for entity in corridors:
        s = ctx.data.one_series("speed_kmph", entity=entity, hours_back=24 * 15)
        if s:
            series_used.append(s)
            for p in s.points:
                buckets[floor_hour(p.t)].append(p.v)
    if not buckets:
        return unavailable(["traffic speed history"], "No traffic history to forecast from.")
    agg = Series("speed_kmph", "km/h", series_used[0].source_id, series_used[0].provenance, "observation", "city-mean",
                 [Point(t, sum(v) / len(v)) for t, v in sorted(buckets.items())], series_used[0].fetched_at)
    fc = forecast_next_24h(ctx, agg, "congestion_forecaster")
    if not fc["ok"]:
        return unavailable(["sufficient traffic history"], f"Cannot forecast congestion: {fc['reason']}")
    slowest_i = min(range(24), key=lambda i: fc["values"][i])
    lim = [fc["fallback_reason"]] if fc.get("degraded") else []
    return ok(
        f"Slowest expected city-mean speed {fc['values'][slowest_i]:.1f} km/h around {ist(fc['targets'][slowest_i])}.",
        confidence=0.45 if fc["degraded"] else 0.65, method=fc["method"], series=series_used, degraded=fc["degraded"],
        metrics={"forecast": [{"t": t.isoformat(), "speed_kmph": round(v, 1)} for t, v in zip(fc["targets"], fc["values"], strict=True)],
                 "slowest_at": fc["targets"][slowest_i].isoformat()},
        limitations=lim + ["City-mean speed across monitored corridors only."],
    )


def public_transit(ctx: AgentContext) -> AgentResult:
    feats = ctx.data.features(["bus_station", "rail_station"])
    if not feats:
        return unavailable(["transit facility locations (run the MapmyIndia or OpenStreetMap sync)"],
                           "No transit facility data has been synchronised for this city.")
    bus = [f for f in feats if f.layer == "bus_station"]
    rail = [f for f in feats if f.layer == "rail_station"]
    bbox = ctx.city.bbox
    n = 6
    cells = set()
    for f in feats:
        cx = int((f.lon - bbox[0]) / ((bbox[2] - bbox[0]) / n))
        cy = int((f.lat - bbox[1]) / ((bbox[3] - bbox[1]) / n))
        if 0 <= cx < n and 0 <= cy < n:
            cells.add((cx, cy))
    coverage = len(cells) / (n * n)
    metrics = {"bus_stations": len(bus), "rail_stations": len(rail), "grid_cell_coverage": round(coverage, 2)}
    if ctx.city.population:
        metrics["stations_per_lakh"] = round((len(bus) + len(rail)) / (ctx.city.population / 1e5), 2)
    findings = []
    if coverage < 0.35:
        findings.append(finding("Sparse mapped transit-station coverage",
                                f"Only {coverage:.0%} of the viewport grid has a mapped bus/rail station.", "moderate"))
    return ok(
        f"{len(bus)} bus stations and {len(rail)} rail stations {source_phrase(feats)}; {coverage:.0%} grid coverage.",
        confidence=0.6, method="spatial:grid_coverage", findings=findings, metrics=metrics,
        extra_refs=[layer_ref("bus_station", feats), layer_ref("rail_station", feats)],
        provenance={f.provenance for f in feats},
        limitations=["Counts bus *stations/terminals*, not individual stops; GTFS feeds would enable route-level analysis.",
                     "Map-data completeness varies by city."],
    )


def road_incident_triage(ctx: AgentContext) -> AgentResult:
    incs = ctx.data.incidents(categories=MOBILITY_CATEGORIES)
    if not incs:
        return ok("No open road or mobility incidents.", confidence=0.8, method="rules:priority_score",
                  metrics={"open": 0, "ranked": []}, extra_refs=[records_ref("incidents", 0, ["operator"], incident_ids=[])],
                  provenance={"operator"})
    ranked = []
    for i in incs:
        age_h = age_hours(ctx.now, i.reported_at)
        score = SEVERITY_WEIGHT[i.severity] * 20 + min(age_h, 24) * (1.5 if i.status == "open" else 0.5)
        ranked.append((score, i, age_h))
    ranked.sort(key=lambda x: -x[0])
    actions = [
        ActionDraft(kind="escalate_incident", title=f"Escalate unacknowledged incident: {i.title}",
                    rationale=f"Severity {i.severity}, open for {age:.1f} h without acknowledgement.",
                    payload={"incident_id": i.id, "to_status": "acknowledged"}, risk="low")
        for _, i, age in ranked if i.status == "open" and i.severity in ("high", "critical") and age >= 2
    ][:5]
    return ok(
        f"{len(incs)} open road incidents; top priority: {ranked[0][1].title}.",
        confidence=0.8, method="rules:priority_score", actions=actions,
        metrics={"open": len(incs), "ranked": [{"id": i.id, "title": i.title, "severity": i.severity, "status": i.status,
                                                "age_h": round(a, 1), "score": round(s, 1)} for s, i, a in ranked[:10]]},
        findings=[finding(i.title, f"{i.severity} · {i.status} · {a:.1f} h old", i.severity, i.lat, i.lon)
                  for _, i, a in ranked[:5]],
        extra_refs=[records_ref("incidents", len(incs), [i.provenance for i in incs], incident_ids=(i.id for i in incs))],
        provenance={i.provenance for i in incs},
    )


def mobility_optimization(ctx: AgentContext) -> AgentResult:
    flow = ctx.up("traffic_flow_analyst")
    triage = ctx.up("road_incident_triage")
    if flow is None:
        return unavailable(["traffic flow analysis"], "Mobility optimisation needs corridor congestion data.")
    corridors = [c for c in flow.metrics.get("corridors", []) if c["congestion_index"] >= 0.5]
    actions = []
    for c in corridors[:4]:
        actions.append(ActionDraft(
            kind="manual", title=f"Deploy traffic marshals / review signal timing on {c['corridor']}",
            rationale=f"Congestion index {c['congestion_index']} ({c['speed_kmph']} km/h vs {c['freeflow_kmph']} km/h free-flow).",
            payload={"corridor": c["corridor"], "department": "Traffic Police"}, risk="low"))
    n_inc = triage.metrics.get("open", 0) if triage else 0
    if triage and n_inc:
        actions.append(ActionDraft(kind="manual", title="Clear priority road incidents before evening peak",
                                   rationale=f"{n_inc} open road incidents are contributing to delays.",
                                   payload={"department": "Traffic Police"}, risk="low"))
    prov = set(flow.provenance) | (set(triage.provenance) if triage else set())
    return ok(
        f"{len(actions)} mobility interventions recommended for human review.", confidence=0.55,
        method="rules:intervention_playbook", actions=actions, provenance=prov,
        metrics={"congested_corridors": len(corridors), "open_road_incidents": n_inc},
        limitations=["Recommendations only — SwarmOS has no connection to signal controllers."],
    )

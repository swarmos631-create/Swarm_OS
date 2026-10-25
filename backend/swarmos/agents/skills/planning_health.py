"""Urban planning & infrastructure, and public health & community skills."""

from __future__ import annotations

from collections import Counter

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import (
    finding,
    layer_ref,
    ok,
    records_ref,
    source_phrase,
    unavailable,
)
from swarmos.cities import haversine_km

# ----------------------------------------------------------------------------- planning


def land_use(ctx: AgentContext) -> AgentResult:
    vals = {}
    used = []
    for v in ("residential", "commercial", "industrial", "retail"):
        s = ctx.data.one_series(f"osm_landuse_{v}_ways", hours_back=24 * 30, category="land_use")
        if s and s.points:
            vals[v] = s.points[-1].v
            used.append(s)
    if not vals or sum(vals.values()) == 0:
        return unavailable(["OpenStreetMap land-use features (run the MapmyIndia or OpenStreetMap sync)"], "No land-use data synchronised for this city.")
    total = sum(vals.values())
    shares = {k: round(v / total, 3) for k, v in vals.items()}
    return ok("Mapped land-use mix (by OSM way count): " + ", ".join(f"{k} {v:.0%}" for k, v in shares.items()) + ".",
              confidence=0.35, method="descriptive:osm_feature_counts", series=used, metrics={"way_counts": vals, "shares": shares},
              limitations=["Counts mapped polygons, not area; mapping density differs by neighbourhood. A LULC raster (e.g. ISRO Bhuvan) is needed for area-based analysis."])


def infrastructure_health(ctx: AgentContext) -> AgentResult:
    assets = ctx.data.features(["asset_bridge", "asset_road", "asset_building"])
    if not assets:
        return unavailable(["asset condition surveys / structural health monitoring"],
                           f"No asset condition data is connected for {ctx.city.name}.")
    ci: list[float] = [float(a.properties["condition_index"]) for a in assets if a.properties.get("condition_index") is not None]
    bands = Counter("poor" if c < 40 else "fair" if c < 70 else "good" for c in ci)
    by_layer = Counter(a.layer for a in assets)
    findings = []
    if bands.get("poor", 0):
        findings.append(finding(f"{bands['poor']} assets in poor condition", "Condition index below 40.", "moderate"))
    return ok(f"{len(assets)} assets: {bands.get('good', 0)} good, {bands.get('fair', 0)} fair, {bands.get('poor', 0)} poor.",
              confidence=0.6, method="descriptive:condition_bands", findings=findings,
              metrics={"bands": dict(bands), "by_type": dict(by_layer), "mean_condition": round(sum(ci) / len(ci), 1) if ci else None},
              provenance={a.provenance for a in assets})


def construction_impact(ctx: AgentContext) -> AgentResult:
    incs = ctx.data.incidents(categories=("construction",), days=30)
    reqs = ctx.data.service_requests(days=30, categories=("construction",))
    flow = ctx.up("traffic_flow_analyst")
    if not incs and not reqs:
        return ok("No construction-related incidents or complaints in the last 30 days.", confidence=0.6,
                  method="rules:complaint_aggregation", metrics={"incidents": 0, "complaints": 0}, provenance={"operator"})
    wards = Counter(r.ward or "unspecified" for r in reqs)
    findings = [finding(f"Construction complaints concentrated in {w}", f"{n} complaints in 30 days.", "low")
                for w, n in wards.most_common(3) if n >= 3 and w != "unspecified"]
    return ok(f"{len(incs)} construction incidents and {len(reqs)} complaints in 30 days.", confidence=0.55,
              method="rules:complaint_aggregation", findings=findings,
              metrics={"incidents": len(incs), "complaints": len(reqs), "top_wards": dict(wards.most_common(5)),
                       "city_congestion_index": flow.metrics.get("city_congestion_index") if flow else None},
              provenance={i.provenance for i in incs} | {r.provenance for r in reqs},
              limitations=["No permit database is connected, so construction sites are known only through reports."])


def urban_growth(ctx: AgentContext) -> AgentResult:
    if ctx.data.has_category("urban_growth", hours_back=24 * 365 * 10):
        series = ctx.data.one_series("built_up_area_km2", hours_back=24 * 365 * 10, category="urban_growth")
        if series and len(series.points) >= 2:
            first, last = series.points[0], series.points[-1]
            years = max(1e-6, (last.t - first.t).days / 365.25)
            cagr = (last.v / first.v) ** (1 / years) - 1 if first.v > 0 else None
            return ok(f"Built-up area changed from {first.v:.1f} to {last.v:.1f} km² ({cagr:.1%}/yr).", confidence=0.6,
                      method="descriptive:cagr", series=[series], metrics={"cagr": cagr, "first": first.v, "last": last.v})
    return unavailable(["multi-temporal built-up area dataset (e.g. GHSL, ISRO Bhuvan LULC)"],
                       "Urban growth analysis requires a multi-year land-cover time series, which is not connected.",
                       ["Load built-up area observations into category 'urban_growth' (metric built_up_area_km2)."])


def infrastructure_prioritization(ctx: AgentContext) -> AgentResult:
    health = ctx.up("infrastructure_health_analyst")
    maint = ctx.up("public_asset_maintenance_agent")
    sla = ctx.up("civic_resolution_tracker")
    if not (health or maint):
        return unavailable(["asset condition / maintenance backlog"], "Prioritisation needs asset condition data.")
    backlog = maint.metrics.get("backlog", []) if maint else []
    complaints = maint.metrics.get("related_complaints_30d", 0) if maint else 0
    breached = sla.metrics.get("open_breached", 0) if sla else 0
    ranked = []
    for a in backlog:
        ci = a.get("condition_index") or 100
        crit = {"asset_bridge": 1.0, "asset_road": 0.7, "asset_building": 0.6}.get(a["layer"], 0.5)
        score = round(0.6 * (100 - ci) + 25 * crit + min(15, complaints * 0.5), 1)
        ranked.append({**a, "priority_score": score})
    ranked.sort(key=lambda r: -r["priority_score"])
    actions = [ActionDraft(kind="manual", title=f"Include '{r['name']}' in next capital works cycle",
                           rationale=f"Priority score {r['priority_score']} (condition {r.get('condition_index')}).",
                           payload={"asset": r["name"], "department": "Engineering"}, risk="low") for r in ranked[:3]]
    prov = set().union(*(set(x.provenance) for x in (health, maint, sla) if x))
    return ok(f"{len(ranked)} assets ranked by condition, criticality and complaint pressure.", confidence=0.5,
              method="mcda:weighted_sum(condition .6, criticality .25, complaints .15)", actions=actions, provenance=prov,
              metrics={"ranked": ranked[:15], "sla_breaches": breached},
              limitations=["Weights are configurable defaults, not a validated policy."])


# ----------------------------------------------------------------------------- health & community

MIN_CELL = 10  # small-cell suppression threshold for aggregated health data


def aggregate_health(ctx: AgentContext) -> AgentResult:
    if not ctx.data.has_category("health_aggregate", hours_back=24 * 60):
        return unavailable(["aggregated public-health surveillance counts (e.g. IDSP/IHIP ward-level weekly counts)"],
                           "No aggregated health data is connected. SwarmOS accepts only aggregated counts — never individual records.",
                           [f"Counts below {MIN_CELL} are suppressed before display to reduce re-identification risk."])
    entities = ctx.data.entities("health_aggregate", hours_back=24 * 60)
    rows, suppressed, used = [], 0, []
    for ent in entities:
        s = ctx.data.one_series("weekly_cases", entity=ent, hours_back=24 * 60, category="health_aggregate")
        if not s or len(s.points) < 2:
            continue
        used.append(s)
        latest, prev = s.points[-1].v, s.points[-2].v
        if latest < MIN_CELL or prev < MIN_CELL:
            suppressed += 1
            continue
        rows.append({"area": ent, "latest": latest, "previous": prev, "change": round((latest - prev) / prev, 2) if prev else None})
    return ok(f"{len(rows)} areas reported; {suppressed} suppressed (small counts).", confidence=0.5, method="descriptive:week_on_week",
              series=used, metrics={"areas": rows, "suppressed": suppressed, "min_cell": MIN_CELL})


def facility_capacity(ctx: AgentContext) -> AgentResult:
    feats = ctx.data.features(["hospital", "clinic"])
    if not feats:
        return unavailable(["health facility locations (run the MapmyIndia or OpenStreetMap sync) or a facility registry"], "No health facilities synchronised.")
    hospitals = [f for f in feats if f.layer == "hospital"]
    clinics = [f for f in feats if f.layer == "clinic"]
    beds = [int(f.properties["beds"]) for f in hospitals if str(f.properties.get("beds", "")).isdigit()]
    metrics: dict = {"hospitals": len(hospitals), "clinics": len(clinics), "hospitals_with_bed_tag": len(beds),
                     "mapped_beds": sum(beds) if beds else None}
    if ctx.city.population:
        metrics["facilities_per_lakh"] = round((len(hospitals) + len(clinics)) / (ctx.city.population / 1e5), 2)
        metrics["population_source"] = ctx.city.population_source
    return ok(f"{len(hospitals)} hospitals and {len(clinics)} clinics {source_phrase(feats)}; {len(beds)} hospitals have bed counts.", confidence=0.45,
              method="descriptive:facility_counts", metrics=metrics, provenance={f.provenance for f in feats},
              extra_refs=[layer_ref("hospital", feats), layer_ref("clinic", feats)],
              limitations=["Capacity (beds, staff, occupancy) is rarely published in map data; connect a facility registry (e.g. HFR/NHRR) for capacity planning.",
                           "Population figure (if shown) is a 2011 census estimate."])


def community_needs(ctx: AgentContext) -> AgentResult:
    reqs = ctx.data.service_requests(days=30)
    if not reqs:
        return unavailable(["citizen service requests"], "No service requests to analyse community needs.")
    by_cat = Counter(r.category or "uncategorised" for r in reqs)
    by_ward = Counter(r.ward or "unspecified" for r in reqs)
    return ok(f"Top community concern: {by_cat.most_common(1)[0][0]} ({by_cat.most_common(1)[0][1]} of {len(reqs)} requests).",
              confidence=0.7, method="descriptive:aggregation", provenance={r.provenance for r in reqs},
              metrics={"by_category": dict(by_cat.most_common()), "by_ward": dict(by_ward.most_common(10)), "total_30d": len(reqs)},
              extra_refs=[records_ref("service_requests", len(reqs), {r.provenance for r in reqs})],
              limitations=["Complaint volume reflects reporting behaviour as well as need."])


def accessibility(ctx: AgentContext) -> AgentResult:
    feats = ctx.data.features(["hospital", "clinic", "school", "bus_station", "rail_station", "public_toilet", "police"])
    if not feats:
        return unavailable(["public facility data with accessibility tags (OSM)"], "No facility data synchronised.")
    per: dict[str, Counter] = {}
    for f in feats:
        per.setdefault(f.layer, Counter())[f.properties.get("wheelchair", "unknown")] += 1
    rows = {layer: {"total": sum(c.values()), "yes": c.get("yes", 0), "limited": c.get("limited", 0), "no": c.get("no", 0),
                    "unknown": c.get("unknown", 0)} for layer, c in per.items()}
    tagged = sum(r["total"] - r["unknown"] for r in rows.values())
    return ok(f"Wheelchair accessibility is tagged for {tagged} of {len(feats)} mapped facilities.", confidence=0.4,
              method="descriptive:osm_wheelchair_tag", metrics={"by_layer": rows, "tagged_share": round(tagged / len(feats), 3)},
              provenance={f.provenance for f in feats},
              limitations=["Most facilities have no accessibility tag; 'unknown' must not be read as inaccessible."])


COVERAGE_TARGETS_KM = {"hospital": 5.0, "fire_station": 7.0, "police": 3.0, "school": 1.5}


def service_coverage(ctx: AgentContext) -> AgentResult:
    feats = ctx.data.features(list(COVERAGE_TARGETS_KM))
    if not feats:
        return unavailable(["public facility locations (run the MapmyIndia or OpenStreetMap sync)"], "No facility data synchronised.")
    w, s, e, n = ctx.city.bbox
    steps = 12
    cells = [(s + (n - s) * (i + 0.5) / steps, w + (e - w) * (j + 0.5) / steps) for i in range(steps) for j in range(steps)]
    result = {}
    for layer, target in COVERAGE_TARGETS_KM.items():
        pts = [(f.lat, f.lon) for f in feats if f.layer == layer]
        if not pts:
            result[layer] = {"facilities": 0, "covered_share": None, "target_km": target}
            continue
        covered = sum(1 for la, lo in cells if min(haversine_km(la, lo, p[0], p[1]) for p in pts) <= target)
        result[layer] = {"facilities": len(pts), "covered_share": round(covered / len(cells), 2), "target_km": target}
    gaps = [k for k, v in result.items() if v["covered_share"] is not None and v["covered_share"] < 0.6]
    findings = [finding(f"Coverage gap: {g.replace('_', ' ')}",
                        f"Only {result[g]['covered_share']:.0%} of the viewport is within {result[g]['target_km']} km.", "moderate") for g in gaps]
    return ok(f"Straight-line coverage computed for {len(result)} facility types over a {steps}×{steps} grid.", confidence=0.4,
              method="spatial:grid_nearest_facility", findings=findings, metrics={"coverage": result, "grid": steps * steps},
              provenance={f.provenance for f in feats},
              limitations=["Grid includes uninhabited areas (water, forest); distance targets are configurable planning heuristics.",
                           "Uses straight-line distance and mapped facilities only."])

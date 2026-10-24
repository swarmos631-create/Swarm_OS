"""Governance & operations, and cross-domain intelligence skills."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import (
    SEVERITY_WEIGHT,
    age_hours,
    align,
    finding,
    ok,
    pearson,
    records_ref,
    unavailable,
)
from swarmos.agents.skills.environment import flood_inputs, flood_score, heat_assessment
from swarmos.ai.retrieval import search
from swarmos.models import AgentExecution, AgentTask, Incident, SyncRun

# ----------------------------------------------------------------------------- governance


def policy_documents(ctx: AgentContext) -> AgentResult:
    query = getattr(ctx.params, "text", None) or ctx.prompt
    hits = search(ctx.data.db, query, city_id=ctx.city.id, clearance=ctx.clearance, k=5)
    if not hits:
        return AgentResult(status="degraded", summary="No authorised documents matched this request.", confidence=0.2,
                           method="retrieval:bm25", missing=["relevant policy / SOP documents in the knowledge base"],
                           metrics={"passages": [], "query": query[:200]}, provenance=["operator"])
    passages = [h.as_dict() for h in hits]
    synthetic = any(h.document.is_synthetic for h in hits)
    return ok(f"{len(passages)} relevant passage(s) retrieved from {len({h.document.id for h in hits})} document(s).",
              confidence=0.6, method="retrieval:bm25", metrics={"passages": passages, "query": query[:200]},
              provenance={"synthetic"} if synthetic else {"operator"},
              extra_refs=[{"type": "document", "document_id": h.document.id, "title": h.document.title,
                           "classification": h.document.classification} for h in hits],
              limitations=["Passages are quoted source material, not conclusions; verify against the authoritative document."])


def compliance_checklist(ctx: AgentContext) -> AgentResult:
    reviews: list[dict[str, Any]] = []
    seen: set[str] = set()
    for agent_id, res in ctx.upstream.items():
        if res.status not in ("ok", "degraded"):
            continue
        for a in res.actions:
            if a.fingerprint() in seen:
                continue
            seen.add(a.fingerprint())
            checks = [
                {"check": "human_approval_required", "passed": True, "note": "All actions require explicit operator approval."},
                {"check": "role_matches_risk", "passed": not (a.risk == "high" and a.required_role != "admin"),
                 "note": "High-risk actions must be approved by an admin."},
                {"check": "evidence_not_synthetic", "passed": not ({"demo", "synthetic"} & set(res.provenance)),
                 "note": "Recommendation relies on synthetic demo data." if {"demo", "synthetic"} & set(res.provenance) else "Evidence is from live/operator data."},
                {"check": "confidence_threshold", "passed": (res.confidence or 0) >= 0.5,
                 "note": f"Originating agent confidence {res.confidence}."},
                {"check": "full_data_available", "passed": res.status == "ok",
                 "note": "Originating analysis ran in degraded mode." if res.status == "degraded" else "Full analysis."},
            ]
            recommended_role = "admin" if a.risk == "high" or not checks[3]["passed"] and a.risk == "moderate" else a.required_role
            reviews.append({"fingerprint": a.fingerprint(), "agent_id": agent_id, "title": a.title, "checks": checks,
                            "passed": sum(c["passed"] for c in checks), "total": len(checks),
                            "recommended_role": recommended_role})
    if not reviews:
        return ok("No proposed actions required compliance review.", confidence=0.9, method="rules:compliance_checklist",
                  metrics={"reviews": []}, provenance={"derived"})
    flagged = [r for r in reviews if r["passed"] < r["total"]]
    return ok(f"Reviewed {len(reviews)} proposed action(s); {len(flagged)} have checklist warnings.", confidence=0.9,
              method="rules:compliance_checklist", metrics={"reviews": reviews, "flagged": len(flagged)}, provenance={"derived"},
              findings=[finding(f"Checklist warnings: {r['title']}", "; ".join(c["note"] for c in r["checks"] if not c["passed"]), "low")
                        for r in flagged[:6]])


def budget_analysis(ctx: AgentContext) -> AgentResult:
    depts = ctx.data.entities("budget", hours_back=24 * 400)
    if not depts:
        return unavailable(["municipal budget and expenditure data"], f"No budget dataset is connected for {ctx.city.name}.")
    rows: list[dict[str, Any]] = []
    provs: set[str] = set()
    for d in depts:
        alloc = ctx.data.latest("budget_allocated_cr", entity=d, category="budget")
        spent = ctx.data.latest("budget_spent_cr", entity=d, category="budget")
        if alloc and spent and alloc.value > 0:
            provs |= {alloc.provenance, spent.provenance}
            rows.append({"department": d, "allocated_cr": alloc.value, "spent_cr": spent.value,
                         "utilisation": round(spent.value / alloc.value, 2)})
    if not rows:
        return unavailable(["allocation and expenditure pairs"], "Budget data incomplete.")
    rows.sort(key=lambda r: r["utilisation"])
    findings = [finding(f"Low utilisation: {r['department']}", f"{r['utilisation']:.0%} of ₹{r['allocated_cr']} cr spent.", "low")
                for r in rows if r["utilisation"] < 0.4][:5]
    findings += [finding(f"Overspend: {r['department']}", f"{r['utilisation']:.0%} of allocation.", "moderate") for r in rows if r["utilisation"] > 1.0]
    total_a = sum(r["allocated_cr"] for r in rows)
    total_s = sum(r["spent_cr"] for r in rows)
    return ok(f"Overall utilisation {total_s / total_a:.0%} of ₹{total_a:,.0f} cr across {len(rows)} departments.", confidence=0.7,
              method="descriptive:utilisation", findings=findings, provenance=provs,
              metrics={"departments": rows, "allocated_cr": round(total_a, 1), "spent_cr": round(total_s, 1)})


def operational_performance(ctx: AgentContext) -> AgentResult:
    db = ctx.data.db
    since = ctx.now - timedelta(days=7)
    execs = db.execute(
        select(AgentExecution.status, func.count(AgentExecution.id))
        .join(AgentTask, AgentTask.id == AgentExecution.task_id)
        .where(AgentTask.city_id == ctx.city.id, AgentTask.created_at >= since)
        .group_by(AgentExecution.status)
    ).all()
    exec_counts = {s: n for s, n in execs}
    incs = list(db.scalars(select(Incident).where(Incident.city_id == ctx.city.id, Incident.reported_at >= ctx.now - timedelta(days=30))))
    resolved = [i for i in incs if i.resolved_at]
    hours = [age_hours(i.resolved_at, i.reported_at) for i in resolved if i.resolved_at is not None]
    mttr = sum(hours) / len(hours) if hours else None
    syncs = db.execute(
        select(SyncRun.status, func.count(SyncRun.id)).where(SyncRun.started_at >= since,
                                                            (SyncRun.city_id == ctx.city.id) | SyncRun.city_id.is_(None))
        .group_by(SyncRun.status)
    ).all()
    sync_counts = {s: n for s, n in syncs}
    total_exec = sum(exec_counts.values())
    total_sync = sum(sync_counts.values())
    return ok(f"7-day agent success {exec_counts.get('succeeded', 0)}/{total_exec}; data-sync success "
              f"{sync_counts.get('succeeded', 0)}/{total_sync}; incident MTTR {mttr or 0:.1f} h.", confidence=0.85,
              method="descriptive:internal_operational_metrics", provenance={"derived"} | {i.provenance for i in incs},
              metrics={"agent_executions_7d": exec_counts, "sync_runs_7d": sync_counts, "incidents_30d": len(incs),
                       "resolved_30d": len(resolved), "mttr_h": round(mttr, 1) if mttr else None},
              extra_refs=[records_ref("agent_executions", total_exec, ["derived"]), records_ref("incidents", len(incs), {i.provenance for i in incs} or {"operator"})])


def resource_allocation(ctx: AgentContext) -> AgentResult:
    triage = ctx.up("emergency_triage")
    depts = ctx.data.entities("resources", hours_back=24 * 7)
    if not depts:
        return unavailable(["field resource inventory (crews/vehicles by department)"],
                           f"No resource inventory is connected for {ctx.city.name}.")
    inventory, provs = {}, set()
    for d in depts:
        m = ctx.data.latest("available_crews", entity=d, category="resources")
        if m:
            inventory[d] = int(m.value)
            provs.add(m.provenance)
    dept_for = {"fire": "Fire & Emergency", "medical": "Health", "flooding": "Storm Water Drains", "water": "Water Supply",
                "energy": "Electrical", "road": "Traffic Police", "mobility": "Traffic Police", "structural": "Engineering",
                "garbage": "Solid Waste Management", "construction": "Engineering"}
    remaining = dict(inventory)
    plan, unmet = [], []
    for r in (triage.metrics.get("ranked", []) if triage else []):
        dept = dept_for.get(r["category"], "Engineering")
        if remaining.get(dept, 0) > 0:
            remaining[dept] -= 1
            plan.append({"incident_id": r["id"], "title": r["title"], "department": dept, "severity": r["severity"]})
        else:
            unmet.append({"incident_id": r["id"], "title": r["title"], "department": dept})
    actions = [ActionDraft(kind="manual", title=f"Assign one {p['department']} crew to '{p['title']}'",
                           rationale=f"Greedy allocation by priority ({p['severity']}).", payload=p, risk="low") for p in plan[:6]]
    prov = provs | (set(triage.provenance) if triage else set())
    return ok(f"{len(plan)} incidents covered by available crews; {len(unmet)} unmet.", confidence=0.55,
              method="optimisation:greedy_priority_assignment", actions=actions, provenance=prov,
              findings=[finding(f"Insufficient {u['department']} crews", u["title"], "moderate") for u in unmet[:4]],
              metrics={"inventory": inventory, "remaining": remaining, "assignments": plan, "unmet": unmet})


# ----------------------------------------------------------------------------- cross-domain

SUMMARY_INPUTS = ("air_quality_analyst", "weather_impact_agent", "emergency_triage", "traffic_flow_analyst",
                  "utility_outage_triage_agent", "flood_risk_analyst")


def city_summary(ctx: AgentContext) -> AgentResult:
    present: dict[str, AgentResult] = {k: r for k in SUMMARY_INPUTS if (r := ctx.up(k)) is not None}
    missing = [k for k in SUMMARY_INPUTS if k not in present]
    if not present:
        return unavailable(["upstream domain analyses"], "No domain analyses completed; cannot summarise the city situation.")
    sev_rank = {"critical": 4, "high": 3, "moderate": 2, "low": 1, "info": 0}
    all_findings = sorted((f for r in present.values() for f in r.findings), key=lambda f: -sev_rank[f.severity])
    worst = all_findings[0].severity if all_findings else "info"
    level = {"critical": "Critical", "high": "Elevated", "moderate": "Watch", "low": "Normal", "info": "Normal"}[worst]
    bullets = [f"• {r.summary}" for r in present.values()]
    text = f"Situation level: {level}. " + " ".join(r.summary for r in present.values())
    method = "template:structured_rollup"
    gen = ctx.models.generate_text("Write a 4-sentence situation brief for a city operations chief using ONLY these facts:\n"
                                   + "\n".join(bullets), system="Never invent facts. Keep numbers exactly as given.", max_tokens=220)
    if gen:
        text, method = f"Situation level: {level}. {gen[0].strip()}", f"local-llm:{gen[1]} over structured rollup"
    prov = set().union(*(set(r.provenance) for r in present.values()))
    return ok(text[:3900], confidence=round(sum(r.confidence or 0 for r in present.values()) / len(present) * (len(present) / len(SUMMARY_INPUTS)) ** 0.3, 2),
              method=method, provenance=prov, findings=all_findings[:8],
              metrics={"situation_level": level, "domains_reporting": list(present), "domains_missing": missing,
                       "bullets": bullets},
              limitations=[f"No input from: {', '.join(missing)}."] if missing else [])


def cross_correlation(ctx: AgentContext) -> AgentResult:
    pairs = [("pm2_5", "air_quality", "wind_speed_10m", "weather"), ("pm2_5", "air_quality", "relative_humidity_2m", "weather"),
             ("pm2_5", "air_quality", "temperature_2m", "weather")]
    results: list[dict[str, Any]] = []
    used: list[Any] = []
    for m1, c1, m2, c2 in pairs:
        a = ctx.data.one_series(m1, hours_back=72, category=c1)
        b = ctx.data.one_series(m2, hours_back=72, category=c2)
        if not a or not b:
            continue
        xs, ys = align(a, b)
        r = pearson(xs, ys)
        if r is not None:
            used += [a, b]
            results.append({"x": m1, "y": m2, "pearson_r": round(r, 3), "n": len(xs)})
    traffic = ctx.data.entities("traffic", hours_back=72)
    if traffic:
        sp = ctx.data.one_series("speed_kmph", entity=traffic[0], hours_back=72)
        pr = ctx.data.one_series("precipitation", hours_back=72, category="weather")
        if sp and pr:
            xs, ys = align(sp, pr)
            r = pearson(xs, ys)
            if r is not None:
                used += [sp, pr]
                results.append({"x": f"speed_kmph[{traffic[0]}]", "y": "precipitation", "pearson_r": round(r, 3), "n": len(xs)})
    if not results:
        return unavailable(["overlapping hourly series across ≥2 domains"], "Not enough overlapping data to correlate domains.")
    strong = [r for r in results if abs(r["pearson_r"]) >= 0.5]
    return ok(f"{len(results)} cross-domain correlations computed; {len(strong)} with |r| ≥ 0.5.", confidence=0.5,
              method="statistical:pearson_72h", series=used, metrics={"correlations": results},
              findings=[finding(f"{r['x']} ↔ {r['y']}: r = {r['pearson_r']}", f"n = {r['n']} hourly points", "info") for r in strong],
              limitations=["Correlation over 72 h of hourly data does not establish causation; model-based series share meteorology."])


def anomaly_investigation(ctx: AgentContext) -> AgentResult:
    present: dict[str, AgentResult] = {
        k: r for k in ("environmental_anomaly_agent", "grid_anomaly_agent", "leak_detection_agent") if (r := ctx.up(k)) is not None}
    if not present:
        return unavailable(["anomaly detector outputs"], "No anomaly detectors produced results.")
    items = [{"from": k, "title": f.title, "severity": f.severity, "detail": f.detail} for k, v in present.items() for f in v.findings]
    domains = Counter(i["from"] for i in items)
    findings, actions = [], []
    if len(domains) >= 2:
        findings.append(finding("Concurrent anomalies across domains", ", ".join(f"{k}: {n}" for k, n in domains.items()), "moderate"))
    for i in sorted(items, key=lambda x: -SEVERITY_WEIGHT.get(x["severity"], 0))[:3]:
        actions.append(ActionDraft(kind="manual", title=f"Field-verify: {i['title']}", rationale=i["detail"][:500],
                                   payload={"source_agent": i["from"]}, risk="low"))
    prov = set().union(*(set(v.provenance) for v in present.values()))
    return ok(f"{len(items)} anomaly signal(s) from {len(present)} detector(s) consolidated.", confidence=0.55,
              method="rules:anomaly_consolidation", findings=findings, actions=actions, provenance=prov,
              metrics={"anomalies": items, "by_detector": dict(domains)})


def scenario_simulation(ctx: AgentContext) -> AgentResult:
    p = ctx.params
    extra_rain = float(getattr(p, "extra_rainfall_mm", 50.0))
    dtemp = float(getattr(p, "temperature_delta_c", 0.0))
    inp = flood_inputs(ctx)
    tmax = ctx.data.one_series("daily_temperature_2m_max", hours_back=0, hours_forward=72, kinds=("forecast",))
    if inp is None and tmax is None:
        return unavailable(["rainfall and temperature forecasts"], "Scenario simulation needs the current weather baseline.")
    metrics: dict[str, Any] = {"assumptions": {"extra_rainfall_mm_24h": extra_rain, "temperature_delta_c": dtemp}}
    findings = []
    if inp:
        base = flood_score(inp["rain_24"], inp["rain_72"], inp["past_48"], inp["reports"])
        scen = flood_score(inp["rain_24"] + extra_rain, inp["rain_72"] + extra_rain, inp["past_48"], inp["reports"])
        metrics["flood"] = {"baseline": base, "scenario": scen, "delta": round(scen["score"] - base["score"], 1)}
        if scen["level"] != base["level"]:
            findings.append(finding(f"Flood risk would rise from {base['level']} to {scen['level']}",
                                    f"With +{extra_rain:.0f} mm in 24 h the index moves {base['score']} → {scen['score']}.", "moderate"))
    if tmax:
        b = heat_assessment(tmax.values, [], None)
        s = heat_assessment([v + dtemp for v in tmax.values], [], None)
        metrics["heat"] = {"baseline": b, "scenario": s}
        if s["level"] != b["level"]:
            findings.append(finding(f"Heat level would change from {b['level']} to {s['level']}", f"Δ{dtemp:+.1f} °C applied.", "moderate"))
    return ok(f"What-if: +{extra_rain:.0f} mm rain, {dtemp:+.1f} °C. " + (f"Flood index Δ {metrics['flood']['delta']:+}." if inp else ""),
              confidence=0.4, method="simulation:deterministic_perturbation", findings=findings, metrics=metrics,
              series=inp["series"] if inp else [tmax] if tmax else [], provenance=inp["prov"] if inp else set(),
              limitations=["A deterministic perturbation of rule-based indices — not a physical or hydrological simulation."])


def operations_planning(ctx: AgentContext) -> AgentResult:
    actions_by_fp: dict[str, tuple[str, ActionDraft]] = {}
    for agent_id, res in ctx.upstream.items():
        if res.status in ("ok", "degraded"):
            for a in res.actions:
                actions_by_fp.setdefault(a.fingerprint(), (agent_id, a))
    if not ctx.upstream:
        return unavailable(["upstream agent results"], "Nothing to plan from.")
    risk_rank = {"high": 0, "moderate": 1, "low": 2}
    ordered = sorted(actions_by_fp.values(), key=lambda x: risk_rank[x[1].risk])
    horizons: dict[str, list[dict[str, Any]]] = {"immediate (0–2 h)": [], "today": [], "this week": []}
    for agent_id, a in ordered:
        key = "immediate (0–2 h)" if a.kind in ("escalate_incident", "notify_operators") or a.risk == "high" else \
              "today" if a.risk == "moderate" or a.kind == "create_incident" else "this week"
        horizons[key].append({"title": a.title, "kind": a.kind, "risk": a.risk, "from": agent_id})
    prov = set().union(*(set(r.provenance) for r in ctx.upstream.values())) if ctx.upstream else set()
    return ok(f"Operations plan with {len(ordered)} proposed action(s) across {sum(1 for v in horizons.values() if v)} horizons. "
              "Nothing executes without approval.", confidence=0.6, method="rules:horizon_planning", provenance=prov | {"derived"},
              metrics={"plan": horizons, "total_actions": len(ordered),
                       "agents_considered": len(ctx.upstream),
                       "agents_failed_or_unavailable": [k for k, v in ctx.upstream.items() if v.status in ("failed", "unavailable")]})

"""Water & sanitation skills."""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import finding, forecast_next_24h, ist, ok, peak, records_ref, unavailable
from swarmos.ml.features import IST

# BIS IS 10500:2012 drinking water specification (selected parameters)
PH_RANGE = (6.5, 8.5)
TURBIDITY_ACCEPTABLE, TURBIDITY_PERMISSIBLE = 1.0, 5.0
RESIDUAL_CHLORINE_MIN = 0.2


def water_demand(ctx: AgentContext) -> AgentResult:
    s = ctx.data.one_series("water_demand_m3h", hours_back=24 * 15, category="water")
    if not s:
        return unavailable(["city water demand / bulk-flow telemetry"],
                           f"No water demand telemetry is connected for {ctx.city.name}.")
    fc = forecast_next_24h(ctx, s, "water_demand_forecaster")
    if not fc["ok"]:
        return unavailable(["sufficient water demand history"], f"Cannot forecast water demand: {fc['reason']}")
    t, v = peak(fc["targets"], fc["values"])
    total = sum(fc["values"])
    return ok(f"Forecast 24 h demand {total:,.0f} m³, peak {v:,.0f} m³/h at {ist(t)}.",
              confidence=0.45 if fc["degraded"] else 0.7, method=fc["method"], series=[s], degraded=fc["degraded"],
              metrics={"total_24h_m3": round(total), "peak_m3h": round(v), "peak_at": t.isoformat(),
                       "forecast": [{"t": a.isoformat(), "m3h": round(b, 1)} for a, b in zip(fc["targets"], fc["values"], strict=True)]},
              limitations=[fc["fallback_reason"]] if fc.get("degraded") else [])


def water_quality(ctx: AgentContext) -> AgentResult:
    entities = ctx.data.entities("water_quality", hours_back=48)
    if not entities:
        return unavailable(["water quality sensor feed (pH, turbidity, residual chlorine)"],
                           f"No water quality telemetry is connected for {ctx.city.name}.")
    rows, findings, actions, used = [], [], [], []
    for ent in entities:
        vals = {}
        for metric in ("ph", "turbidity_ntu", "residual_chlorine_mgl"):
            m = ctx.data.latest(metric, entity=ent, category="water_quality")
            if m:
                vals[metric] = m.value
                used_s = ctx.data.one_series(metric, entity=ent, hours_back=6, category="water_quality")
                if used_s:
                    used.append(used_s)
        issues = []
        if "ph" in vals and not (PH_RANGE[0] <= vals["ph"] <= PH_RANGE[1]):
            issues.append(f"pH {vals['ph']:.2f} outside {PH_RANGE[0]}–{PH_RANGE[1]}")
        if vals.get("turbidity_ntu", 0) > TURBIDITY_PERMISSIBLE:
            issues.append(f"turbidity {vals['turbidity_ntu']:.1f} NTU above permissible {TURBIDITY_PERMISSIBLE}")
        elif vals.get("turbidity_ntu", 0) > TURBIDITY_ACCEPTABLE:
            issues.append(f"turbidity {vals['turbidity_ntu']:.1f} NTU above acceptable {TURBIDITY_ACCEPTABLE}")
        if "residual_chlorine_mgl" in vals and vals["residual_chlorine_mgl"] < RESIDUAL_CHLORINE_MIN:
            issues.append(f"residual chlorine {vals['residual_chlorine_mgl']:.2f} mg/L below {RESIDUAL_CHLORINE_MIN}")
        rows.append({"site": ent, **{k: round(v, 2) for k, v in vals.items()}, "issues": issues})
        if issues:
            serious = any("permissible" in i or "chlorine" in i for i in issues)
            findings.append(finding(f"Water quality exceedance at {ent}", "; ".join(issues), "high" if serious else "moderate"))
            if serious:
                actions.append(ActionDraft(kind="create_incident", title=f"Water quality exceedance at {ent}",
                                           rationale="; ".join(issues),
                                           payload={"category": "water", "severity": "high", "title": f"Water quality exceedance at {ent}",
                                                    "description": "; ".join(issues), "department": "Water Supply"},
                                           risk="moderate"))
    return ok(f"{len(entities)} monitoring sites checked against IS 10500; {len(findings)} with exceedances.",
              confidence=0.7, method="rules:bis_is_10500_2012", series=used, findings=findings, actions=actions,
              metrics={"sites": rows, "sites_with_issues": len(findings)})


def leak_detection(ctx: AgentContext) -> AgentResult:
    dmas = [e for e in ctx.data.entities("water", hours_back=24 * 8) if e.startswith("DMA")]
    if not dmas:
        return unavailable(["district metered area (DMA) flow telemetry"],
                           f"No DMA flow telemetry is connected for {ctx.city.name}.")
    rows, findings, actions, used = [], [], [], []
    for dma in dmas:
        s = ctx.data.one_series("flow_m3h", entity=dma, hours_back=24 * 8, category="water")
        if not s:
            continue
        used.append(s)
        nightly: dict = {}
        for p in s.points:
            local = p.t + IST
            if 1 <= local.hour < 4:  # minimum night flow window, 01:00–04:00 IST
                nightly.setdefault(local.date(), []).append(p.v)
        mnf = [(d, min(v)) for d, v in sorted(nightly.items()) if len(v) >= 2]
        if len(mnf) < 4:
            continue
        latest_d, latest = mnf[-1]
        baseline = statistics.median(v for _, v in mnf[:-1])
        ratio = latest / baseline if baseline > 0 else 1.0
        rows.append({"dma": dma, "latest_mnf_m3h": round(latest, 1), "baseline_mnf_m3h": round(baseline, 1),
                     "ratio": round(ratio, 2), "night": str(latest_d)})
        if ratio >= 1.3:
            meta = (ctx.data.latest("flow_m3h", entity=dma, category="water") or None)
            lat = meta.lat if meta else None
            lon = meta.lon if meta else None
            findings.append(finding(f"Suspected leak in {dma}", f"Minimum night flow up {ratio:.0%} vs 7-night median.", "moderate", lat, lon))
            actions.append(ActionDraft(kind="create_incident", title=f"Suspected leak in {dma}",
                                       rationale=f"MNF {latest:.0f} m³/h vs baseline {baseline:.0f} m³/h (×{ratio:.2f}).",
                                       payload={"category": "water", "severity": "moderate", "title": f"Suspected leak in {dma}",
                                                "description": f"Minimum night flow ×{ratio:.2f} of 7-night median.",
                                                "department": "Water Supply", "lat": lat, "lon": lon}, risk="low"))
    if not rows:
        return unavailable(["≥4 nights of DMA flow history"], "Insufficient night-flow history for leak detection.")
    return ok(f"{len(rows)} DMAs analysed by minimum night flow; {len(findings)} suspected leak(s).", confidence=0.6,
              method="rules:minimum_night_flow", series=used, findings=findings, actions=actions,
              metrics={"dmas": rows, "suspected": len(findings), "ratio_threshold": 1.3})


def drainage_risk(ctx: AgentContext) -> AgentResult:
    flood = ctx.up("flood_risk_analyst")
    reqs = ctx.data.service_requests(days=14, categories=("drainage", "water_logging"))
    if flood is None and not reqs:
        return unavailable(["flood risk assessment or drainage complaints"], "No drainage-related data available.")
    wards = Counter(r.ward or "unspecified" for r in reqs)
    hotspots: list[dict[str, Any]] = [{"ward": w, "reports": n} for w, n in wards.most_common(5)]
    actions = [ActionDraft(kind="manual", title=f"Inspect and desilt drains in {h['ward']}",
                           rationale=f"{h['reports']} drainage complaints in 14 days.",
                           payload={"ward": h["ward"], "department": "Storm Water Drains"}, risk="low")
               for h in hotspots[:3] if h["reports"] >= 3 and h["ward"] != "unspecified"]
    score = flood.metrics.get("score") if flood else None
    prov = {r.provenance for r in reqs} | (set(flood.provenance) if flood else set())
    return ok(f"{len(reqs)} drainage complaints in 14 days; flood index {score if score is not None else 'n/a'}.",
              confidence=0.55, method="rules:complaint_hotspots", actions=actions, provenance=prov,
              metrics={"complaints_14d": len(reqs), "hotspots": hotspots, "flood_index": score},
              extra_refs=[records_ref("service_requests", len(reqs), {r.provenance for r in reqs} or {"operator"})])


def wastewater(ctx: AgentContext) -> AgentResult:
    stps = ctx.data.entities("wastewater", hours_back=72)
    threshold = float(ctx.spec.config.get("bod_limit_mgl", 20.0))
    if not stps:
        return unavailable(["sewage treatment plant (STP) telemetry"], f"No STP telemetry is connected for {ctx.city.name}.")
    rows, findings, used = [], [], []
    for stp in stps:
        inflow = ctx.data.one_series("stp_inflow_mld", entity=stp, hours_back=24, category="wastewater")
        cap = ctx.data.latest("stp_capacity_mld", entity=stp, category="wastewater")
        bod = ctx.data.latest("effluent_bod_mgl", entity=stp, category="wastewater")
        if not inflow or not cap:
            continue
        used.append(inflow)
        util = (sum(inflow.values) / len(inflow.values)) / cap.value
        row = {"stp": stp, "utilisation": round(util, 2), "capacity_mld": cap.value, "effluent_bod_mgl": bod.value if bod else None}
        rows.append(row)
        if util > 1.0:
            findings.append(finding(f"{stp} over capacity", f"Average inflow at {util:.0%} of design capacity.", "high"))
        if bod and bod.value > threshold:
            findings.append(finding(f"{stp} effluent BOD above limit", f"{bod.value:.0f} mg/L vs configured limit {threshold:.0f} mg/L.", "moderate"))
    if not rows:
        return unavailable(["STP inflow and capacity"], "STP telemetry is incomplete.")
    return ok(f"{len(rows)} STPs analysed; {len(findings)} issue(s).", confidence=0.65, method="rules:capacity_and_effluent_limits",
              series=used, findings=findings, metrics={"plants": rows, "bod_limit_mgl": threshold},
              limitations=["BOD limit is configurable; verify the plant's consent-to-operate conditions."])

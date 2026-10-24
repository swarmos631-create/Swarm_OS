"""Energy & utilities skills."""

from __future__ import annotations

from datetime import datetime, timedelta

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import (
    SEVERITY_WEIGHT,
    age_hours,
    finding,
    forecast_next_24h,
    ist,
    ok,
    peak,
    records_ref,
    unavailable,
)
from swarmos.ai.runtime import ModelUnavailable
from swarmos.ml.features import anomaly_features

FREQ_BAND = (49.90, 50.05)  # Indian Electricity Grid Code operating band
VOLTAGE_BAND = (0.95, 1.05)  # per-unit, typical distribution planning limits


def energy_demand(ctx: AgentContext) -> AgentResult:
    s = ctx.data.one_series("power_demand_mw", hours_back=24 * 15, category="energy")
    if not s:
        return unavailable(["city electricity demand (SLDC / DISCOM SCADA)"],
                           f"No electricity demand feed is connected for {ctx.city.name}.")
    fc = forecast_next_24h(ctx, s, "energy_demand_forecaster")
    if not fc["ok"]:
        return unavailable(["sufficient demand history"], f"Cannot forecast demand: {fc['reason']}")
    t, v = peak(fc["targets"], fc["values"])
    mm = fc.get("model_metrics") or {}
    return ok(f"Forecast peak demand {v:,.0f} MW at {ist(t)}.", confidence=0.45 if fc["degraded"] else 0.72,
              method=fc["method"], series=[s], degraded=fc["degraded"],
              metrics={"peak_mw": round(v, 1), "peak_at": t.isoformat(), "model_holdout_mape": mm.get("mape"),
                       "forecast": [{"t": a.isoformat(), "mw": round(b, 1)} for a, b in zip(fc["targets"], fc["values"], strict=True)]},
              limitations=[fc["fallback_reason"]] if fc.get("degraded") else [])


def grid_anomaly(ctx: AgentContext) -> AgentResult:
    freq = ctx.data.one_series("grid_frequency_hz", hours_back=24, category="energy")
    feeders = [e for e in ctx.data.entities("energy", hours_back=24) if e.startswith("Feeder")]
    if not freq and not feeders:
        return unavailable(["grid frequency / feeder SCADA telemetry"], f"No grid telemetry is connected for {ctx.city.name}.")
    findings, used, events = [], [], []
    if freq:
        used.append(freq)
        out = [p for p in freq.points if not (FREQ_BAND[0] <= p.v <= FREQ_BAND[1])]
        if out:
            worst = min(out, key=lambda p: -abs(p.v - 50))
            findings.append(finding("Grid frequency outside IEGC band", f"{len(out)} hourly readings outside {FREQ_BAND}; worst {worst.v:.3f} Hz at {ist(worst.t)}.",
                                    "high" if abs(worst.v - 50) > 0.2 else "moderate"))
    model_label = None
    demand = ctx.data.one_series("power_demand_mw", hours_back=24, category="energy")
    for feeder in feeders:
        v = ctx.data.one_series("feeder_voltage_pu", entity=feeder, hours_back=24, category="energy")
        load = ctx.data.one_series("feeder_load_pct", entity=feeder, hours_back=24, category="energy")
        if not v or not load:
            continue
        used += [v, load]
        latest_v, latest_load = v.points[-1].v, load.points[-1].v
        rule_flag = not (VOLTAGE_BAND[0] <= latest_v <= VOLTAGE_BAND[1]) or latest_load > 100
        model_flag = None
        try:
            model = ctx.models.load("sensor_anomaly_detector")
            fq = freq.points[-1].v if freq else 50.0
            dd = 0.0
            if demand and len(demand.points) >= 2 and demand.points[-2].v:
                dd = (demand.points[-1].v - demand.points[-2].v) / demand.points[-2].v * 100
            model_flag = bool(model.estimator.predict([anomaly_features(fq, latest_v, latest_load, dd)])[0] == -1)
            model_label = model.label
        except ModelUnavailable:
            pass
        if rule_flag or model_flag:
            events.append({"feeder": feeder, "voltage_pu": round(latest_v, 3), "load_pct": round(latest_load, 1),
                           "rule_flag": rule_flag, "model_flag": model_flag})
            findings.append(finding(f"Anomaly on {feeder}", f"Voltage {latest_v:.3f} pu, load {latest_load:.0f}%"
                                    + (" (rule breach)" if rule_flag else "") + (" (model outlier)" if model_flag else ""),
                                    "high" if rule_flag else "moderate"))
    method = "rules:iegc_band+voltage_limits" + (f" + {model_label}" if model_label else "")
    return ok(f"{len(findings)} grid anomaly finding(s) across {len(feeders)} feeders.", confidence=0.7 if model_label else 0.6,
              method=method, series=used, findings=findings, metrics={"events": events, "feeders_checked": len(feeders)},
              limitations=[] if model_label else ["Anomaly model not promoted; rule checks only."])


def renewable_optimizer(ctx: AgentContext) -> AgentResult:
    ghi = ctx.data.one_series("shortwave_radiation", hours_back=0, hours_forward=48, kinds=("forecast",), category="weather")
    if not ghi:
        return unavailable(["solar irradiance forecast (Open-Meteo)"], "No irradiance forecast synchronised for this city.")
    pr = float(ctx.spec.config.get("performance_ratio", 0.75))
    by_day: dict = {}
    for p in ghi.points:
        local = p.t + timedelta(hours=5, minutes=30)
        by_day.setdefault(local.date(), []).append((local, p.v))
    days = []
    for d, pts in sorted(by_day.items()):
        kwh = sum(v for _, v in pts) / 1000 * pr
        strong = [t.hour for t, v in pts if v >= 500]
        window_txt = f"{min(strong):02d}:00–{max(strong) + 1:02d}:00 IST" if strong else "none"
        days.append({"date": str(d), "yield_kwh_per_kwp": round(kwh, 2), "high_output_window": window_txt})
    best = max(days, key=lambda x: x["yield_kwh_per_kwp"])
    actions = []
    if best["high_output_window"] != "none":
        actions.append(ActionDraft(kind="manual", title=f"Schedule flexible municipal loads (pumping) in {best['high_output_window']} on {best['date']}",
                                   rationale=f"Highest forecast solar yield ({best['yield_kwh_per_kwp']} kWh/kWp).",
                                   payload={"department": "Electrical / Water Supply"}, risk="low"))
    return ok(f"Best solar day {best['date']}: ~{best['yield_kwh_per_kwp']} kWh per kWp installed.", confidence=0.6,
              method=f"physics:ghi_integration_pr{pr}", series=[ghi], actions=actions, metrics={"days": days, "performance_ratio": pr},
              limitations=["Assumes horizontal-plane irradiance and a fixed performance ratio; no plant-specific data."])


def _local(t: datetime) -> datetime:
    return t + timedelta(hours=5, minutes=30)


def streetlights(ctx: AgentContext) -> AgentResult:
    days = ctx.data.forecast_rows("daily_daylight_hours", limit=2)
    reqs = ctx.data.service_requests(days=30, categories=("streetlight",))
    if not days and not reqs:
        return unavailable(["sunrise/sunset forecast or streetlight complaints"], "No streetlight-related data available.")
    offset = timedelta(minutes=int(ctx.spec.config.get("switch_offset_min", 10)))
    schedule = []
    for r in days:
        if "sunset_utc" not in r.meta or "sunrise_utc" not in r.meta:
            continue
        sunset = datetime.fromisoformat(r.meta["sunset_utc"])
        sunrise = datetime.fromisoformat(r.meta["sunrise_utc"])
        schedule.append({"date": str(_local(sunrise).date()),
                         "lights_off": _local(sunrise + offset).strftime("%H:%M IST"),
                         "lights_on": _local(sunset - offset).strftime("%H:%M IST")})
    open_reqs = [r for r in reqs if r.status not in ("resolved", "closed")]
    findings = []
    if len(open_reqs) >= 5:
        findings.append(finding("Streetlight fault backlog", f"{len(open_reqs)} open streetlight complaints.", "moderate"))
    prov = {r.provenance for r in reqs} | {d.provenance for d in days}
    return ok(f"{len(open_reqs)} open streetlight complaints; switching schedule derived from sunrise/sunset.", confidence=0.7,
              method="rules:astronomical_schedule", findings=findings, provenance=prov,
              metrics={"schedule": schedule, "open_complaints": len(open_reqs), "complaints_30d": len(reqs)},
              extra_refs=[records_ref("service_requests", len(reqs), {r.provenance for r in reqs} or {"operator"})],
              limitations=["Schedules assume photocell-free timer control; adjust offsets to local practice."])


def outage_triage(ctx: AgentContext) -> AgentResult:
    incs = ctx.data.incidents(categories=("energy", "water", "utility"))
    if not incs:
        return ok("No open utility outages.", confidence=0.8, method="rules:priority_score", provenance={"operator"},
                  metrics={"open": 0, "ranked": []}, extra_refs=[records_ref("incidents", 0, ["operator"])])
    ranked = sorted(incs, key=lambda i: (-SEVERITY_WEIGHT[i.severity], -age_hours(ctx.now, i.reported_at)))
    return ok(f"{len(incs)} open utility incidents; most severe: {ranked[0].title}.", confidence=0.75, method="rules:priority_score",
              metrics={"open": len(incs), "by_category": {c: sum(1 for i in incs if i.category == c) for c in {i.category for i in incs}},
                       "ranked": [{"id": i.id, "title": i.title, "severity": i.severity, "category": i.category,
                                   "age_h": round(age_hours(ctx.now, i.reported_at), 1)} for i in ranked[:10]]},
              findings=[finding(i.title, f"{i.category} · {i.severity}", i.severity, i.lat, i.lon) for i in ranked[:5]],
              extra_refs=[records_ref("incidents", len(incs), [i.provenance for i in incs])], provenance={i.provenance for i in incs})

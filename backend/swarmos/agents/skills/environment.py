"""Environment & climate skills."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import (
    NAQI_AVERAGING_H,
    finding,
    freshness_confidence,
    ist,
    mean,
    naqi,
    ok,
    records_ref,
    robust_z,
    unavailable,
    window,
)
from swarmos.services.data import Series

# IMD daily rainfall intensity classes (mm / 24 h)
RAIN_CLASSES = [(204.5, "Extremely heavy"), (115.6, "Very heavy"), (64.5, "Heavy"), (15.6, "Moderate"), (2.5, "Light")]
WEATHER_LIMITS = ["Model-based weather values (Open-Meteo), not IMD observations or warnings."]


def rain_class(mm: float) -> str:
    for lo, label in RAIN_CLASSES:
        if mm >= lo:
            return label
    return "Very light / none"


def air_quality(ctx: AgentContext) -> AgentResult:
    # Prefer CPCB station observations when the OGD feed is connected.
    station_entities = ctx.data.entities("air_quality_station", hours_back=6)
    used: list[Series] = []
    conc: dict[str, float] = {}
    method = "naqi:cpcb_2014_on_cams_model"
    if station_entities:
        method = "naqi:cpcb_2014_on_station_observations"
        for pollutant, hours in NAQI_AVERAGING_H.items():
            vals = []
            for ent in station_entities:
                s = ctx.data.one_series(pollutant, entity=ent, hours_back=hours, category="air_quality_station")
                if s:
                    used.append(s)
                    vals.extend(s.values)
            if vals:
                conc[pollutant] = sum(vals) / len(vals)
    if not conc:
        for pollutant, hours in NAQI_AVERAGING_H.items():
            s = ctx.data.one_series(pollutant, hours_back=hours, category="air_quality")
            if s and len(s.points) >= max(4, hours // 2):
                used.append(s)
                v = mean(s.values)
                if v is not None:
                    conc[pollutant] = v / 1000.0 if pollutant == "carbon_monoxide" else v  # CO µg/m³ → mg/m³
    if not conc:
        return unavailable(["air quality concentrations (Open-Meteo or CPCB)"],
                           "No recent air-quality data has been synchronised for this city.")
    result = naqi(conc)
    if result is None:
        return unavailable(["≥3 pollutants incl. PM2.5 or PM10"], "Insufficient pollutants to compute an Indian NAQI.")
    findings, actions = [], []
    if result["aqi"] > 200:
        sev = "critical" if result["aqi"] > 400 else "high" if result["aqi"] > 300 else "moderate"
        findings.append(finding(f"AQI {result['aqi']} — {result['category']}",
                                f"Dominant pollutant: {result['dominant']}. Sensitive groups should limit outdoor exertion.", sev))
    if result["aqi"] > 300:
        actions.append(ActionDraft(kind="notify_operators", title=f"Air quality advisory: {result['category']} ({result['aqi']})",
                                   rationale=f"Indicative NAQI {result['aqi']} driven by {result['dominant']}.",
                                   payload={"level": "warning", "body": f"Indicative AQI {result['aqi']} ({result['category']})."},
                                   risk="moderate"))
    conf = (0.8 if station_entities else 0.55) * freshness_confidence(used[0] if used else None, ctx.now)
    limitations = [] if station_entities else [
        "Computed from CAMS model concentrations (~40 km grid), not CPCB stations; official AQI may differ substantially."]
    return ok(f"Indicative AQI {result['aqi']} ({result['category']}), dominant {result['dominant']}.",
              confidence=conf, method=method, series=used, findings=findings, actions=actions,
              metrics={**result, "concentrations": {k: round(v, 2) for k, v in conc.items()}}, limitations=limitations)


def _forecast(ctx: AgentContext, metric: str, hours: float = 24) -> Series | None:
    return ctx.data.one_series(metric, hours_back=0, hours_forward=hours, kinds=("forecast",), category="weather")


def weather_impact(ctx: AgentContext) -> AgentResult:
    precip = _forecast(ctx, "precipitation")
    temp = _forecast(ctx, "apparent_temperature")
    gust = _forecast(ctx, "wind_gusts_10m")
    if not (precip or temp or gust):
        return unavailable(["weather forecast (Open-Meteo)"], "No weather forecast has been synchronised for this city.")
    rain24 = sum(precip.values) if precip else 0.0
    max_feel = max(temp.values) if temp else None
    max_gust = max(gust.values) if gust else None
    findings = []
    rc = rain_class(rain24)
    if rain24 >= 64.5:
        findings.append(finding(f"{rc} rainfall expected", f"{rain24:.0f} mm forecast over the next 24 h.", "high" if rain24 >= 115.6 else "moderate"))
    elif rain24 >= 15.6:
        findings.append(finding("Moderate rainfall expected", f"{rain24:.0f} mm over 24 h — expect localised waterlogging.", "low"))
    if max_feel is not None and max_feel >= 40:
        findings.append(finding("High heat stress", f"'Feels-like' temperature up to {max_feel:.0f} °C.", "high" if max_feel >= 45 else "moderate"))
    if max_gust is not None and max_gust >= 50:
        findings.append(finding("Strong wind gusts", f"Gusts up to {max_gust:.0f} km/h — tree-fall and hoarding risk.", "moderate"))
    used = [s for s in (precip, temp, gust) if s]
    return ok(f"Next 24 h: {rain24:.0f} mm rain ({rc.lower()}), feels-like max {max_feel or 0:.0f} °C, gusts {max_gust or 0:.0f} km/h.",
              confidence=0.65 * freshness_confidence(used[0], ctx.now), method="rules:imd_rainfall_classes", series=used,
              findings=findings, metrics={"rain_24h_mm": round(rain24, 1), "rain_class": rc,
                                          "max_apparent_temp_c": max_feel, "max_gust_kmh": max_gust},
              limitations=WEATHER_LIMITS)


def flood_score(rain_24h: float, rain_72h: float, past_48h: float, drainage_reports: int) -> dict[str, Any]:
    """Transparent additive index (0–100). Not a hydrological model."""
    components = {
        "forecast_24h": min(40.0, rain_24h / 115.6 * 40),
        "forecast_72h": min(20.0, rain_72h / 200 * 20),
        "antecedent_48h": min(20.0, past_48h / 100 * 20),
        "drainage_reports": min(20.0, drainage_reports * 2.0),
    }
    score = round(sum(components.values()), 1)
    return {"score": score, "components": {k: round(v, 1) for k, v in components.items()},
            "level": "severe" if score >= 70 else "elevated" if score >= 45 else "guarded" if score >= 20 else "low"}


def _drainage_reports(ctx: AgentContext) -> tuple[int, set[str]]:
    reqs = ctx.data.service_requests(days=7, categories=("drainage", "water_logging"))
    incs = ctx.data.incidents(categories=("flooding", "drainage"), days=7)
    return len(reqs) + len(incs), {r.provenance for r in reqs} | {i.provenance for i in incs}


def flood_inputs(ctx: AgentContext) -> dict[str, Any] | None:
    fc72 = ctx.data.one_series("precipitation", hours_back=0, hours_forward=72, kinds=("forecast",), category="weather")
    past = ctx.data.one_series("precipitation", hours_back=48, category="weather")
    if fc72 is None and past is None:
        return None
    rain_24 = sum(window(fc72, ctx.now, ctx.now + timedelta(hours=24))) if fc72 else 0.0
    rain_72 = sum(fc72.values) if fc72 else 0.0
    past_48 = sum(past.values) if past else 0.0
    reports, prov = _drainage_reports(ctx)
    return {"rain_24": rain_24, "rain_72": rain_72, "past_48": past_48, "reports": reports, "prov": prov,
            "series": [s for s in (fc72, past) if s]}


def flood_risk(ctx: AgentContext) -> AgentResult:
    inp = flood_inputs(ctx)
    if inp is None:
        return unavailable(["rainfall forecast and recent rainfall"], "No rainfall data synchronised for this city.")
    res = flood_score(inp["rain_24"], inp["rain_72"], inp["past_48"], inp["reports"])
    findings, actions = [], []
    if res["level"] in ("elevated", "severe"):
        findings.append(finding(f"Flood risk {res['level']} (index {res['score']})",
                                f"24 h forecast {inp['rain_24']:.0f} mm, 72 h {inp['rain_72']:.0f} mm, past 48 h {inp['past_48']:.0f} mm, "
                                f"{inp['reports']} drainage reports.", "high" if res["level"] == "severe" else "moderate"))
        actions.append(ActionDraft(kind="manual", title="Pre-position dewatering pumps at known waterlogging points",
                                   rationale=f"Flood risk index {res['score']} ({res['level']}).",
                                   payload={"department": "Storm Water Drains"}, risk="moderate"))
    return ok(f"Flood risk index {res['score']}/100 ({res['level']}).", confidence=0.5, method="rules:additive_flood_index",
              series=inp["series"], findings=findings, actions=actions, metrics=res,
              extra_refs=[records_ref("drainage_reports", inp["reports"], inp["prov"] or {"operator"})],
              provenance=inp["prov"],
              limitations=["Not a hydrological model — no terrain, drainage-network or river-gauge data is used.", *WEATHER_LIMITS])


def heat_assessment(max_temps: list[float], min_temps: list[float], max_feels: float | None) -> dict[str, Any]:
    tmax = max(max_temps) if max_temps else None
    level = "none"
    if tmax is not None:
        level = "severe_heatwave" if tmax >= 47 else "heatwave" if tmax >= 45 else "hot" if tmax >= 40 else "none"
    warm_night = bool(min_temps) and max(min_temps) >= 30
    return {"max_temp_c": tmax, "level": level, "warm_night": warm_night, "max_apparent_temp_c": max_feels}


def urban_heat(ctx: AgentContext) -> AgentResult:
    tmax = ctx.data.one_series("daily_temperature_2m_max", hours_back=0, hours_forward=72, kinds=("forecast",))
    tmin = ctx.data.one_series("daily_temperature_2m_min", hours_back=0, hours_forward=72, kinds=("forecast",))
    feels = _forecast(ctx, "apparent_temperature", 72)
    if not tmax:
        return unavailable(["daily temperature forecast"], "No temperature forecast synchronised for this city.")
    res = heat_assessment(tmax.values, tmin.values if tmin else [], max(feels.values) if feels else None)
    findings, actions = [], []
    if res["level"] != "none":
        sev = {"severe_heatwave": "critical", "heatwave": "high", "hot": "moderate"}[res["level"]]
        findings.append(finding(f"{res['level'].replace('_', ' ').title()} conditions forecast",
                                f"Max {res['max_temp_c']:.1f} °C over the next 3 days.", sev))
    if res["level"] in ("heatwave", "severe_heatwave"):
        actions.append(ActionDraft(kind="notify_operators", title="Activate Heat Action Plan measures",
                                   rationale=f"Forecast maximum {res['max_temp_c']:.1f} °C meets the IMD absolute heatwave criterion.",
                                   payload={"level": "warning", "body": "Open cooling centres; adjust outdoor work hours."}, risk="moderate"))
    if res["warm_night"]:
        findings.append(finding("Warm nights forecast", "Minimum temperatures ≥ 30 °C limit night-time recovery.", "moderate"))
    return ok(f"3-day max {res['max_temp_c']:.1f} °C — heat level: {res['level'].replace('_', ' ')}.", confidence=0.6,
              method="rules:imd_absolute_heatwave_criterion", series=[s for s in (tmax, tmin, feels) if s],
              findings=findings, actions=actions, metrics=res,
              limitations=["Uses only the IMD absolute criterion (≥45 °C); departure-from-normal criteria need climatological normals.",
                           *WEATHER_LIMITS])


def environmental_anomaly(ctx: AgentContext) -> AgentResult:
    checks = [("pm2_5", "air_quality"), ("pm10", "air_quality"), ("nitrogen_dioxide", "air_quality"),
              ("temperature_2m", "weather"), ("wind_speed_10m", "weather")]
    anomalies, used = [], []
    for metric, cat in checks:
        s = ctx.data.one_series(metric, hours_back=72, category=cat)
        if not s or len(s.points) < 24:
            continue
        used.append(s)
        z = robust_z(s.points[-1].v, s.values[:-1])
        if z is not None and abs(z) >= 3.5:
            anomalies.append({"metric": metric, "latest": round(s.points[-1].v, 2), "robust_z": round(z, 2),
                              "at": s.points[-1].t.isoformat(), "at_ist": ist(s.points[-1].t)})
    if not used:
        return unavailable(["≥24 h of environmental series"], "Not enough environmental history to test for anomalies.")
    findings = [finding(f"Unusual {a['metric']} reading", f"Latest {a['latest']} (robust z = {a['robust_z']}) at {a['at_ist']}.",
                        "moderate") for a in anomalies]
    return ok(f"{len(anomalies)} anomalous environmental signal(s) in the last 72 h across {len(used)} series.",
              confidence=0.6, method="statistical:robust_zscore_mad", series=used, findings=findings,
              metrics={"anomalies": anomalies, "series_tested": len(used), "threshold": 3.5})

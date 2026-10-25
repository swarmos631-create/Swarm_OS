"""Shared building blocks for agent skills: result builders, statistics, Indian NAQI, forecasting."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from swarmos.agents.contracts import AgentContext, AgentResult, Finding
from swarmos.ai.runtime import ModelUnavailable
from swarmos.ml.features import MIN_HISTORY_HOURS, floor_hour, forecast_row, seasonal_naive
from swarmos.services.data import Series


def unavailable(missing: Iterable[str], summary: str, limitations: Iterable[str] = ()) -> AgentResult:
    return AgentResult(status="unavailable", summary=summary, missing=list(missing), limitations=list(limitations),
                       method="none")


def ok(summary: str, *, confidence: float, method: str = "rules", series: Iterable[Series] = (),
       extra_refs: Iterable[dict[str, Any]] = (), provenance: Iterable[str] = (), degraded: bool = False,
       **kw: Any) -> AgentResult:
    series = list(series)
    refs = [s.ref() for s in series] + list(extra_refs)
    prov = {s.provenance for s in series} | set(provenance)
    return AgentResult(status="degraded" if degraded else "ok", summary=summary, confidence=round(confidence, 2),
                       method=method, data_refs=refs, provenance=sorted(prov), **kw)


def feature_ref(layer: str, n: int, provenance: str, source_id: str) -> dict[str, Any]:
    return {"type": "features", "layer": layer, "n": n, "provenance": provenance, "source_id": source_id}


def layer_ref(layer: str, feats: Iterable[Any]) -> dict[str, Any]:
    """Reference to the features of one layer, with provenance and source taken from the rows themselves."""
    rows = [f for f in feats if f.layer == layer]
    provs = sorted({f.provenance for f in rows}) or ["unavailable"]
    sources = sorted({f.source_id for f in rows})
    return {"type": "features", "layer": layer, "n": len(rows), "provenance": provs[0] if len(provs) == 1 else "mixed",
            "source_id": sources[0] if len(sources) == 1 else ",".join(sources),
            "feature_ids": [row.id for row in rows[-12:]], "feature_ids_truncated": len(rows) > 12}


def source_phrase(feats: Iterable[Any]) -> str:
    """'in OpenStreetMap' / 'by MapmyIndia' / '(simulated)' — so summaries never misstate where facilities came from."""
    srcs = {f.source_id for f in feats}
    if srcs and all(f.provenance == "demo" for f in feats):
        return "(simulated locations)"
    names = [n for sid, n in (("mappls-places", "MapmyIndia"), ("osm-overpass", "OpenStreetMap")) if sid in srcs]
    return f"from {' + '.join(names)}" if names else "mapped"


def records_ref(kind: str, n: int, provenances: Iterable[str], *, request_ids: Iterable[str] | None = None,
                incident_ids: Iterable[str] | None = None, execution_ids: Iterable[int] | None = None) -> dict[str, Any]:
    ref = {"type": "records", "kind": kind, "n": n, "provenance": sorted(set(provenances))}
    if request_ids is not None:
        ids = list(request_ids)
        ref["request_ids"] = ids[-12:]
        ref["request_ids_truncated"] = len(ids) > 12
    if incident_ids is not None:
        ids = list(incident_ids)
        ref["incident_ids"] = ids[-12:]
        ref["incident_ids_truncated"] = len(ids) > 12
    if execution_ids is not None:
        ids = list(execution_ids)
        ref["execution_ids"] = ids[-12:]
        ref["execution_ids_truncated"] = len(ids) > 12
    return ref


def window(series: Series, start: datetime, end: datetime) -> list[float]:
    return [p.v for p in series.points if start <= p.t <= end]


def mean(xs: Sequence[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def robust_z(latest: float, history: Sequence[float]) -> float | None:
    if len(history) < 12:
        return None
    med = statistics.median(history)
    mad = statistics.median([abs(x - med) for x in history]) or 1e-9
    return 0.6745 * (latest - med) / mad


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 12 or n != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def align(a: Series, b: Series) -> tuple[list[float], list[float]]:
    bm = {p.t: p.v for p in b.points}
    xs, ys = [], []
    for p in a.points:
        if p.t in bm:
            xs.append(p.v)
            ys.append(bm[p.t])
    return xs, ys


def age_hours(now: datetime, dt: datetime) -> float:
    dt = dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)
    return (now - dt).total_seconds() / 3600


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def freshness_confidence(series: Series | None, now: datetime, max_age_h: float = 6) -> float:
    """Confidence penalty for stale data: 1.0 when fresh, decaying to 0.4 at 4x the threshold."""
    if series is None or series.fetched_at is None:
        return 0.4
    age_h = (now - series.fetched_at).total_seconds() / 3600
    if age_h <= max_age_h:
        return 1.0
    return max(0.4, 1.0 - 0.6 * (age_h - max_age_h) / (3 * max_age_h))


# --------------------------------------------------------------------------- Indian National AQI (CPCB, 2014)
# Breakpoints: (conc_lo, conc_hi, index_lo, index_hi). The final "Severe" segment is open-ended in the standard;
# we cap the concentration upper bound (documented) so the index saturates at 500.
NAQI_BREAKPOINTS: dict[str, list[tuple[float, float, int, int]]] = {
    "pm10": [(0, 50, 0, 50), (51, 100, 51, 100), (101, 250, 101, 200), (251, 350, 201, 300), (351, 430, 301, 400), (431, 800, 401, 500)],
    "pm2_5": [(0, 30, 0, 50), (31, 60, 51, 100), (61, 90, 101, 200), (91, 120, 201, 300), (121, 250, 301, 400), (251, 500, 401, 500)],
    "nitrogen_dioxide": [(0, 40, 0, 50), (41, 80, 51, 100), (81, 180, 101, 200), (181, 280, 201, 300), (281, 400, 301, 400), (401, 800, 401, 500)],
    "ozone": [(0, 50, 0, 50), (51, 100, 51, 100), (101, 168, 101, 200), (169, 208, 201, 300), (209, 748, 301, 400), (749, 1000, 401, 500)],
    "carbon_monoxide": [(0, 1.0, 0, 50), (1.1, 2.0, 51, 100), (2.1, 10, 101, 200), (10.1, 17, 201, 300), (17.1, 34, 301, 400), (34.1, 50, 401, 500)],
    "sulphur_dioxide": [(0, 40, 0, 50), (41, 80, 51, 100), (81, 380, 101, 200), (381, 800, 201, 300), (801, 1600, 301, 400), (1601, 2400, 401, 500)],
}
NAQI_AVERAGING_H = {"pm10": 24, "pm2_5": 24, "nitrogen_dioxide": 24, "sulphur_dioxide": 24, "ozone": 8, "carbon_monoxide": 8}
NAQI_CATEGORIES = [(50, "Good"), (100, "Satisfactory"), (200, "Moderate"), (300, "Poor"), (400, "Very Poor"), (500, "Severe")]


def naqi_subindex(pollutant: str, conc: float) -> int | None:
    bps = NAQI_BREAKPOINTS.get(pollutant)
    if bps is None or conc < 0:
        return None
    for i, (c_lo, c_hi, i_lo, i_hi) in enumerate(bps):
        nxt_lo = bps[i + 1][0] if i + 1 < len(bps) else math.inf
        if conc <= c_hi or conc < nxt_lo:
            conc_c = min(conc, c_hi)
            return round(i_lo + (i_hi - i_lo) * (conc_c - c_lo) / (c_hi - c_lo)) if c_hi > c_lo else i_lo
    return 500


def naqi_category(index: int) -> str:
    for hi, label in NAQI_CATEGORIES:
        if index <= hi:
            return label
    return "Severe"


def naqi(concentrations: dict[str, float]) -> dict[str, Any] | None:
    """CPCB rule: at least three pollutants, one of which must be PM2.5 or PM10."""
    raw = {p: naqi_subindex(p, c) for p, c in concentrations.items()}
    subs: dict[str, int] = {p: s for p, s in raw.items() if s is not None}
    if len(subs) < 3 or not ({"pm2_5", "pm10"} & subs.keys()):
        return None
    dominant = max(subs, key=lambda p: subs[p])
    value = subs[dominant]
    return {"aqi": value, "category": naqi_category(value), "dominant": dominant, "subindices": subs}


# --------------------------------------------------------------------------- forecasting

def forecast_next_24h(ctx: AgentContext, series: Series, model_name: str) -> dict[str, Any]:
    """Forecast the next 24 hours with the promoted model; fall back to a seasonal-naive baseline."""
    hist = {floor_hour(p.t): p.v for p in series.points}
    # Anchor on the newest hour that has data, so a feed that lags by an hour or two still yields a model forecast.
    start = min(floor_hour(ctx.now), max(hist)) + timedelta(hours=1) if hist else floor_hour(ctx.now) + timedelta(hours=1)
    targets = [start + timedelta(hours=h) for h in range(24)]
    span_h = (max(hist) - min(hist)).total_seconds() / 3600 if hist else 0
    if span_h < MIN_HISTORY_HOURS - 24:
        return {"ok": False, "reason": f"Needs ≥{MIN_HISTORY_HOURS} h of history, have {span_h:.0f} h"}
    try:
        model = ctx.models.load(model_name)
        rows = [forecast_row(hist, t) for t in targets]
        if any(r is None for r in rows):
            raise ModelUnavailable("History has gaps; model features incomplete")
        preds = [float(v) for v in model.estimator.predict(rows)]
        return {"ok": True, "method": model.label, "targets": targets, "values": preds,
                "model_metrics": model.metrics, "degraded": False}
    except ModelUnavailable as exc:
        preds_opt = [seasonal_naive(hist, t, 168) or seasonal_naive(hist, t, 24) for t in targets]
        if any(v is None for v in preds_opt):
            return {"ok": False, "reason": f"{exc}; seasonal-naive fallback also lacks history"}
        return {"ok": True, "method": "baseline:seasonal_naive_168h", "targets": targets,
                "values": [float(v) for v in preds_opt if v is not None], "degraded": True,
                "fallback_reason": str(exc)}


def peak(targets: list[datetime], values: list[float]) -> tuple[datetime, float]:
    i = max(range(len(values)), key=lambda k: values[k])
    return targets[i], values[i]


def ist(t: datetime) -> str:
    return (t + timedelta(hours=5, minutes=30)).strftime("%d %b %H:%M IST")


def severity_from_score(score: float) -> str:
    return "critical" if score >= 80 else "high" if score >= 60 else "moderate" if score >= 35 else "low"


SEVERITY_WEIGHT = {"low": 1, "moderate": 2, "high": 3, "critical": 4}


def finding(title: str, detail: str = "", severity: str = "info", lat: float | None = None,
            lon: float | None = None) -> Finding:
    return Finding(title=title, detail=detail, severity=severity,  # type: ignore[arg-type]
                   location={"lat": lat, "lon": lon} if lat is not None and lon is not None else None)

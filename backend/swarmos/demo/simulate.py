"""Simulated city data for categories that have no connected feed: weather, air quality, facilities, land use,
urban growth and aggregated health counts.

Values are generated, but calibrated to each city's real climate: monthly means of temperature, rainfall,
humidity, wind, solar radiation and pollutant concentrations come from the Kaggle dataset
"India Daily Weather & Air Quality" (akshaykarthickms007, CC BY 4.0; 50 cities, 2020–2026), matched to the
nearest of those cities. Everything produced here is stored with provenance ``demo`` and shown as "Simulated".
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from swarmos.cities import haversine_km
from swarmos.integrations.base import NormalizedFeature, NormalizedMeasurement
from swarmos.models import City

IST = timedelta(hours=5, minutes=30)
FIELDS = ("tmax", "tmin", "rain", "rain_p", "rh", "wind", "sw", "pm25", "pm10", "no2", "so2", "o3", "co")
CLIMATE_SOURCE = "Kaggle: India Daily Weather & Air Quality (CC BY 4.0), monthly means by SwarmOS"


@lru_cache(maxsize=1)
def climatology() -> dict[str, Any]:
    return json.loads(resources.files("swarmos.demo").joinpath("climatology.json").read_text(encoding="utf-8"))


def nearest_climate(city: City) -> tuple[str, float]:
    """Name of the closest city in the climatology table and its distance in km."""
    best = min(climatology()["cities"].items(), key=lambda kv: haversine_km(city.lat, city.lon, kv[1]["lat"], kv[1]["lon"]))
    return best[0], round(haversine_km(city.lat, city.lon, best[1]["lat"], best[1]["lon"]), 1)


def month_normals(city: City, when: datetime) -> dict[str, float]:
    """Normals for the local date, interpolated between adjacent months so values do not jump at month ends."""
    name, _ = nearest_climate(city)
    months = climatology()["cities"][name]["m"]
    local = when + IST
    m0 = local.month - 1
    frac = (local.day - 1) / 30.0
    if frac < 0.5:
        a, b, w = months[(m0 - 1) % 12], months[m0], 0.5 + frac
    else:
        a, b, w = months[m0], months[(m0 + 1) % 12], frac - 0.5
    return {f: a[i] * (1 - w) + b[i] * w for i, f in enumerate(FIELDS)}


def daylight_hours(lat: float, when: datetime) -> float:
    doy = (when + IST).timetuple().tm_yday
    decl = 23.44 * math.sin(math.radians(360 / 365 * (doy - 81)))
    x = -math.tan(math.radians(lat)) * math.tan(math.radians(decl))
    return round(24 / math.pi * math.acos(max(-1.0, min(1.0, x))), 3)


def _diurnal(hour: float, peak: float, low: float) -> float:
    """0..1 shape with its maximum at `peak` and minimum at `low` (local hours)."""
    span = (peak - low) % 24
    pos = (hour - low) % 24
    if pos <= span:
        return 0.5 - 0.5 * math.cos(math.pi * pos / span)
    return 0.5 + 0.5 * math.cos(math.pi * (pos - span) / (24 - span))


def _bump(hour: float, mu: float, sigma: float) -> float:
    d = min(abs(hour - mu), 24 - abs(hour - mu))
    return math.exp(-(d ** 2) / (2 * sigma ** 2))


def environment(city: City, now: datetime, *, past_h: int = 96, weather_ahead_h: int = 72, aq_ahead_h: int = 48) -> list[NormalizedMeasurement]:
    """Hourly weather and air quality around `now` (past = model_analysis, future = forecast), plus daily summaries."""
    rng = random.Random(f"env:{city.id}:{now:%Y%m%d}")
    out: list[NormalizedMeasurement] = []
    start = now - timedelta(hours=past_h - 1)
    hours = [start + timedelta(hours=h) for h in range(past_h + weather_ahead_h)]
    days: dict[str, dict[str, float]] = {}
    for t in hours:
        key = (t + IST).strftime("%Y-%m-%d")
        if key not in days:
            n = month_normals(city, t)
            wet = rng.random() < n["rain_p"]
            days[key] = {
                "t_anom": rng.gauss(0, 1.3), "aq_anom": math.exp(rng.gauss(0, 0.22)), "wet": float(wet),
                "rain_total": rng.expovariate(1 / max(0.5, n["rain"] / max(n["rain_p"], 0.05))) if wet else 0.0,
                "rain_start": rng.uniform(13, 19), "rain_len": rng.uniform(2, 6), **n,
            }

    def add(cat: str, metric: str, unit: str, t: datetime, v: float, ahead_h: int) -> None:
        if t > now + timedelta(hours=ahead_h):
            return
        out.append(NormalizedMeasurement(category=cat, metric=metric, value=round(v, 3), unit=unit, observed_at=t,
                                         kind="forecast" if t > now else "model_analysis"))

    for t in hours:
        local = t + IST
        h = local.hour + local.minute / 60
        d = days[local.strftime("%Y-%m-%d")]
        tmax, tmin = d["tmax"] + d["t_anom"], d["tmin"] + d["t_anom"] * 0.7
        if d["wet"]:
            tmax -= 1.5
        temp = tmin + (tmax - tmin) * _diurnal(h, 14.5, 6.0) + rng.gauss(0, 0.3)
        rh = min(100.0, max(8.0, d["rh"] + 14 * (0.5 - _diurnal(h, 14.5, 6.0)) + (10 if d["wet"] else 0) + rng.gauss(0, 2)))
        wind = max(0.0, d["wind"] * (0.35 + 0.45 * _diurnal(h, 15, 5)) + rng.gauss(0, 0.8))
        gust = wind * rng.uniform(1.4, 1.8)
        e = rh / 100 * 6.105 * math.exp(17.27 * temp / (237.7 + temp))
        feels = temp + 0.33 * e - 0.70 * (wind / 3.6) - 4.0
        rain = 0.0
        if d["wet"]:
            pos = (h - d["rain_start"]) % 24
            if pos < d["rain_len"]:
                rain = d["rain_total"] / d["rain_len"] * (1.4 if pos < d["rain_len"] / 2 else 0.6)
        dl = daylight_hours(city.lat, t)
        sunrise = 12.2 - dl / 2
        sw = 0.0
        if sunrise < h < sunrise + dl:
            peak = d["sw"] * 1e6 / (dl * 3600) * math.pi / 2
            sw = peak * math.sin(math.pi * (h - sunrise) / dl) * (0.55 if d["wet"] else 1.0)
        add("weather", "temperature_2m", "°C", t, temp, weather_ahead_h)
        add("weather", "apparent_temperature", "°C", t, feels, weather_ahead_h)
        add("weather", "relative_humidity_2m", "%", t, rh, weather_ahead_h)
        add("weather", "precipitation", "mm", t, rain, weather_ahead_h)
        add("weather", "wind_speed_10m", "km/h", t, wind, weather_ahead_h)
        add("weather", "wind_gusts_10m", "km/h", t, gust, weather_ahead_h)
        add("weather", "shortwave_radiation", "W/m²", t, sw, weather_ahead_h)

        traffic = 0.55 + 0.45 * max(_bump(h, 9, 1.6), _bump(h, 19.5, 2.0))
        night = 0.75 + 0.5 * _bump(h, 23, 3.5) + 0.25 * _bump(h, 7.5, 2) - 0.25 * _bump(h, 15, 3)
        washout = 0.6 if rain > 1 else 1.0
        aq = d["aq_anom"] * washout
        add("air_quality", "pm2_5", "µg/m³", t, max(1.0, d["pm25"] * night * aq * rng.uniform(0.92, 1.08)), aq_ahead_h)
        add("air_quality", "pm10", "µg/m³", t, max(2.0, d["pm10"] * night * aq * rng.uniform(0.9, 1.1)), aq_ahead_h)
        add("air_quality", "nitrogen_dioxide", "µg/m³", t, max(0.5, d["no2"] * traffic * 1.25 * aq * rng.uniform(0.9, 1.1)), aq_ahead_h)
        add("air_quality", "sulphur_dioxide", "µg/m³", t, max(0.2, d["so2"] * aq * rng.uniform(0.85, 1.15)), aq_ahead_h)
        add("air_quality", "ozone", "µg/m³", t, max(2.0, d["o3"] * (0.25 + 0.75 * _diurnal(h, 14.5, 6.5)) * rng.uniform(0.9, 1.1)), aq_ahead_h)
        add("air_quality", "carbon_monoxide", "µg/m³", t, max(40.0, d["co"] * 0.8 * night * aq * rng.uniform(0.9, 1.1)), aq_ahead_h)

    today = (now + IST).replace(hour=0, minute=0, second=0, microsecond=0) - IST
    for i in range(-4, 3):
        day = today + timedelta(days=i)
        key = (day + IST).strftime("%Y-%m-%d")
        d = days.get(key) or {**month_normals(city, day), "t_anom": 0.0, "wet": 0.0, "rain_total": 0.0}
        kind: Literal["forecast", "model_analysis"] = "forecast" if day >= today else "model_analysis"
        for metric, unit, v in [
            ("daily_temperature_2m_max", "°C", d["tmax"] + d["t_anom"] - (1.5 if d["wet"] else 0)),
            ("daily_temperature_2m_min", "°C", d["tmin"] + d["t_anom"] * 0.7),
            ("daily_precipitation_sum", "mm", d["rain_total"]),
            ("daily_daylight_hours", "h", daylight_hours(city.lat, day + timedelta(hours=6))),
        ]:
            out.append(NormalizedMeasurement(category="weather", metric=metric, value=round(v, 2), unit=unit, observed_at=day, kind=kind))
    return out


# --------------------------------------------------------------------------- facilities, land use, growth, health

FACILITY_TYPES: dict[str, tuple[str, float, int]] = {
    # layer: (label, facilities per lakh residents, cap)
    "hospital": ("Hospital", 1.2, 90), "clinic": ("Clinic", 3.5, 160), "fire_station": ("Fire station", 0.35, 30),
    "police": ("Police station", 1.0, 80), "school": ("School", 9.0, 260), "bus_station": ("Bus stop", 6.0, 220),
    "rail_station": ("Railway/metro station", 0.4, 40), "public_toilet": ("Public toilet", 2.0, 120),
    "water_tower": ("Water tank", 0.6, 40), "substation": ("Power substation", 0.5, 40),
}
WHEELCHAIR = ["yes", "yes", "limited", "no", "unknown", "unknown"]


def _spread(city: City, rng: random.Random) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = city.bbox or [city.lon - 0.15, city.lat - 0.15, city.lon + 0.15, city.lat + 0.15]
    sx, sy = (max_lon - min_lon) / 5, (max_lat - min_lat) / 5
    lon = min(max_lon, max(min_lon, rng.gauss(city.lon, sx)))
    lat = min(max_lat, max(min_lat, rng.gauss(city.lat, sy)))
    return round(lat, 5), round(lon, 5)


def facilities(city: City, place: Callable[[float], tuple[float, float]] | None = None) -> list[NormalizedFeature]:
    """Facility counts scale with population; `place(spread)` returns a land point inside the city (see landmask.Placer)."""
    rng = random.Random(f"fac:{city.id}")
    lakhs = max(2.0, (city.population or 1_000_000) / 1e5)
    feats: list[NormalizedFeature] = []
    for layer, (label, per_lakh, cap) in FACILITY_TYPES.items():
        n = int(min(cap, max(3, round(per_lakh * lakhs * rng.uniform(0.8, 1.2)))))
        for i in range(n):
            lat, lon = place(0.2) if place else _spread(city, rng)
            props: dict[str, Any] = {"wheelchair": rng.choice(WHEELCHAIR), "simulated": True}
            if layer == "hospital":
                props["beds"] = str(rng.choice([30, 50, 75, 100, 150, 200, 300, 500]))
                props["operator_type"] = rng.choice(["government", "private", "private", "charitable"])
            feats.append(NormalizedFeature(layer=layer, external_id=f"sim-{layer}-{i}", name=f"{label} {i + 1}, {city.name}", lat=lat, lon=lon,
                                           properties=props))
    return feats


def land_use_and_growth(city: City, now: datetime) -> list[NormalizedMeasurement]:
    rng = random.Random(f"lu:{city.id}")
    lakhs = max(2.0, (city.population or 1_000_000) / 1e5)
    out = []
    for value, share in (("residential", 0.55), ("commercial", 0.14), ("industrial", 0.12), ("retail", 0.09)):
        ways = round(lakhs * 22 * share * rng.uniform(0.8, 1.2))
        out.append(NormalizedMeasurement(category="land_use", metric=f"osm_landuse_{value}_ways", value=float(ways), unit="ways",
                                         observed_at=now - timedelta(days=1)))
    area = lakhs * rng.uniform(0.9, 1.4)
    growth = rng.uniform(0.022, 0.048)
    for y in range(10, -1, -1):
        out.append(NormalizedMeasurement(category="urban_growth", metric="built_up_area_km2", value=round(area / (1 + growth) ** y, 2),
                                         unit="km²", observed_at=now - timedelta(days=365 * y + 1)))
    return out


def health_aggregates(city: City, now: datetime) -> list[NormalizedMeasurement]:
    """Weekly counts of a notifiable condition by zone (aggregated, never individual records)."""
    rng = random.Random(f"health:{city.id}")
    lakhs = max(2.0, (city.population or 1_000_000) / 1e5)
    out = []
    for z in range(1, 7):
        base = max(4.0, lakhs * rng.uniform(0.8, 3.0) / 6)
        trend = rng.uniform(-0.06, 0.12)
        for w in range(8, -1, -1):
            v = max(0, round(base * (1 + trend) ** (8 - w) * rng.uniform(0.85, 1.15)))
            out.append(NormalizedMeasurement(category="health_aggregate", metric="weekly_cases", value=float(v), unit="cases",
                                             observed_at=now - timedelta(days=7 * w + 1), entity=f"Zone {z}"))
    return out

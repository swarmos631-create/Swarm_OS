"""Concrete source adapters.

Only sources with a verified, documented access method are implemented as live adapters. Each adapter
normalizes to :class:`NormalizedMeasurement` / :class:`NormalizedFeature` and never fabricates values: a
missing or null upstream value is skipped, not filled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from swarmos.cities import approx_viewport
from swarmos.config import Settings
from swarmos.integrations.base import (
    Adapter,
    IntegrationError,
    NormalizedFeature,
    NormalizedMeasurement,
    SourceSpec,
    SyncPayload,
)
from swarmos.integrations.http import request_json
from swarmos.models import City

HOUR = 3600
DAY = 24 * HOUR


def _parse_gmt(ts: str) -> datetime:
    """Open-Meteo returns ISO local times without offset; we request timezone=GMT so they are UTC."""
    dt = datetime.fromisoformat(ts)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _require(obj: Any, key: str, source: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise IntegrationError("schema", f"{source}: response missing '{key}'")
    return obj[key]


# --------------------------------------------------------------------------- Open-Meteo weather

WEATHER_HOURLY = {
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
    "apparent_temperature": "°C",
    "precipitation": "mm",
    "wind_speed_10m": "km/h",
    "wind_gusts_10m": "km/h",
    "cloud_cover": "%",
    "shortwave_radiation": "W/m²",
}
WEATHER_DAILY = {
    "temperature_2m_max": "°C",
    "temperature_2m_min": "°C",
    "precipitation_sum": "mm",
    "uv_index_max": "index",
}


class OpenMeteoWeather(Adapter):
    spec = SourceSpec(
        id="open-meteo-weather",
        name="Open-Meteo Weather Forecast API",
        kind="api",
        categories=("weather",),
        coverage="Any coordinate in India (global model grid)",
        auth_required="none (non-commercial use; commercial use requires an Open-Meteo plan)",
        license="CC BY 4.0 (data); see open-meteo.com/en/terms",
        attribution="Weather data by Open-Meteo.com",
        homepage="https://open-meteo.com/en/docs",
        freshness_threshold_s=3 * HOUR,
        min_sync_interval_s=15 * 60,
        scope="city",
        limitations=(
            "Values are numerical weather model output (analysis and forecast), not station observations.",
            "Grid resolution is coarser than neighbourhood scale; local extremes may be under-represented.",
            "For official warnings, defer to India Meteorological Department (IMD) bulletins.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "hourly": ",".join(WEATHER_HOURLY),
            "daily": ",".join([*WEATHER_DAILY, "sunrise", "sunset"]),
            "timezone": "GMT",
            "past_days": 2,
            "forecast_days": 3,
            "wind_speed_unit": "kmh",
        }
        data = await request_json(client, "GET", settings.open_meteo_base_url, params=params,
                                  timeout=settings.http_timeout_seconds)
        return normalize_open_meteo_weather(data, datetime.now(UTC))


def normalize_open_meteo_weather(data: dict[str, Any], now: datetime) -> SyncPayload:
    out = SyncPayload()
    hourly = _require(data, "hourly", "open-meteo")
    times = [_parse_gmt(t) for t in _require(hourly, "time", "open-meteo")]
    for metric, unit in WEATHER_HOURLY.items():
        values = hourly.get(metric)
        if values is None:
            continue
        for t, v in zip(times, values, strict=False):
            if v is None:
                continue
            out.measurements.append(
                NormalizedMeasurement(
                    category="weather", metric=metric, value=float(v), unit=unit, observed_at=t,
                    kind="forecast" if t > now else "model_analysis",
                )
            )
    daily = data.get("daily") or {}
    days = [_parse_gmt(t) for t in daily.get("time", [])]
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for metric, unit in WEATHER_DAILY.items():
        for d, v in zip(days, daily.get(metric) or [], strict=False):
            if v is None:
                continue
            out.measurements.append(
                NormalizedMeasurement(
                    category="weather", metric=f"daily_{metric}", value=float(v), unit=unit, observed_at=d,
                    kind="forecast" if d >= today else "model_analysis",
                )
            )
    for d, sr, ss in zip(days, daily.get("sunrise") or [], daily.get("sunset") or [], strict=False):
        if sr and ss:
            sunrise, sunset = _parse_gmt(sr), _parse_gmt(ss)
            out.measurements.append(
                NormalizedMeasurement(
                    category="weather", metric="daily_daylight_hours",
                    value=round((sunset - sunrise).total_seconds() / 3600, 3), unit="h", observed_at=d,
                    kind="forecast" if d >= today else "model_analysis",
                    meta={"sunrise_utc": sunrise.isoformat(), "sunset_utc": sunset.isoformat()},
                )
            )
    return out


# --------------------------------------------------------------------------- Open-Meteo air quality

AQ_HOURLY = {
    "pm2_5": "µg/m³",
    "pm10": "µg/m³",
    "nitrogen_dioxide": "µg/m³",
    "sulphur_dioxide": "µg/m³",
    "ozone": "µg/m³",
    "carbon_monoxide": "µg/m³",
}


class OpenMeteoAirQuality(Adapter):
    spec = SourceSpec(
        id="open-meteo-air-quality",
        name="Open-Meteo Air Quality API (CAMS global)",
        kind="api",
        categories=("air_quality",),
        coverage="Any coordinate in India (CAMS global model, ~40 km grid)",
        auth_required="none (non-commercial use)",
        license="CC BY 4.0; contains modified Copernicus Atmosphere Monitoring Service information",
        attribution="Air quality data by Open-Meteo.com, based on Copernicus CAMS",
        homepage="https://open-meteo.com/en/docs/air-quality-api",
        freshness_threshold_s=6 * HOUR,
        min_sync_interval_s=30 * 60,
        scope="city",
        limitations=(
            "Modelled concentrations from a coarse global atmospheric model — not CPCB monitoring-station readings.",
            "Indian NAQI values derived from these concentrations are indicative only; official AQI is published by CPCB.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "hourly": ",".join(AQ_HOURLY),
            "timezone": "GMT",
            "past_days": 3,
            "forecast_days": 2,
        }
        data = await request_json(client, "GET", settings.open_meteo_aq_base_url, params=params,
                                  timeout=settings.http_timeout_seconds)
        return normalize_open_meteo_aq(data, datetime.now(UTC))


def normalize_open_meteo_aq(data: dict[str, Any], now: datetime) -> SyncPayload:
    out = SyncPayload()
    hourly = _require(data, "hourly", "open-meteo-aq")
    times = [_parse_gmt(t) for t in _require(hourly, "time", "open-meteo-aq")]
    for metric, unit in AQ_HOURLY.items():
        for t, v in zip(times, hourly.get(metric) or [], strict=False):
            if v is None:
                continue
            out.measurements.append(
                NormalizedMeasurement(
                    category="air_quality", metric=metric, value=float(v), unit=unit, observed_at=t,
                    kind="forecast" if t > now else "model_analysis",
                )
            )
    return out


# --------------------------------------------------------------------------- USGS earthquakes

INDIA_REGION = (60.0, 0.0, 100.0, 40.0)  # lon/lat box covering India and neighbouring seismic zones


class UsgsEarthquakes(Adapter):
    spec = SourceSpec(
        id="usgs-earthquakes",
        name="USGS Earthquake Hazards — real-time GeoJSON feed",
        kind="feed",
        categories=("seismic",),
        coverage="Global feed, filtered to the Indian region (60–100°E, 0–40°N)",
        auth_required="none",
        license="Public domain (U.S. Government work)",
        attribution="U.S. Geological Survey, Earthquake Hazards Program",
        homepage="https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php",
        freshness_threshold_s=2 * HOUR,
        min_sync_interval_s=10 * 60,
        scope="global",
        limitations=(
            "Magnitude ≥ 2.5 events from the past 7 days. National Center for Seismology (NCS, India) is the "
            "authoritative national source and may report events USGS does not.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        data = await request_json(client, "GET", settings.usgs_feed_url, timeout=settings.http_timeout_seconds)
        return normalize_usgs(data)


def normalize_usgs(data: dict[str, Any]) -> SyncPayload:
    out = SyncPayload()
    if data.get("type") != "FeatureCollection":
        raise IntegrationError("schema", "usgs: expected a GeoJSON FeatureCollection")
    min_lon, min_lat, max_lon, max_lat = INDIA_REGION
    for f in data.get("features", []):
        try:
            lon, lat, depth = f["geometry"]["coordinates"][:3]
            props = f["properties"]
        except (KeyError, TypeError, ValueError):
            continue
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if props.get("mag") is None or props.get("time") is None:
            continue
        out.features.append(
            NormalizedFeature(
                layer="earthquake",
                external_id=str(f.get("id")),
                name=props.get("place") or "Earthquake",
                lat=lat,
                lon=lon,
                observed_at=datetime.fromtimestamp(props["time"] / 1000, UTC),
                properties={
                    "magnitude": props.get("mag"),
                    "depth_km": depth,
                    "place": props.get("place"),
                    "url": props.get("url"),
                    "alert": props.get("alert"),
                    "tsunami": props.get("tsunami"),
                    "status": props.get("status"),
                },
            )
        )
    return out


# --------------------------------------------------------------------------- OpenStreetMap / Overpass

OSM_LAYERS: list[tuple[str, str]] = [
    ('nwr["amenity"="hospital"]', "hospital"),
    ('nwr["amenity"="clinic"]', "clinic"),
    ('nwr["amenity"="fire_station"]', "fire_station"),
    ('nwr["amenity"="police"]', "police"),
    ('nwr["amenity"="school"]', "school"),
    ('nwr["amenity"="bus_station"]', "bus_station"),
    ('nwr["railway"="station"]', "rail_station"),
    ('nwr["man_made"="water_tower"]', "water_tower"),
    ('nwr["power"="substation"]', "substation"),
    ('nwr["amenity"="toilets"]', "public_toilet"),
]
LANDUSE_VALUES = ("residential", "commercial", "industrial", "retail")


def _layer_for(tags: dict[str, str]) -> str | None:
    amenity = tags.get("amenity")
    mapping = {"hospital": "hospital", "clinic": "clinic", "fire_station": "fire_station", "police": "police",
               "school": "school", "bus_station": "bus_station", "toilets": "public_toilet"}
    if amenity in mapping:
        return mapping[amenity]
    if tags.get("railway") == "station":
        return "rail_station"
    if tags.get("man_made") == "water_tower":
        return "water_tower"
    if tags.get("power") == "substation":
        return "substation"
    return None


class OsmOverpass(Adapter):
    spec = SourceSpec(
        id="osm-overpass",
        name="OpenStreetMap via Overpass API — public facilities",
        kind="api",
        categories=("public_infrastructure", "land_use"),
        coverage="Any city viewport in India (community-mapped; completeness varies by city)",
        auth_required="none (fair-use policy; heavy use requires a self-hosted Overpass instance)",
        license="ODbL 1.0",
        attribution="© OpenStreetMap contributors",
        homepage="https://wiki.openstreetmap.org/wiki/Overpass_API",
        freshness_threshold_s=7 * DAY,
        min_sync_interval_s=DAY,
        scope="city",
        limitations=(
            "OSM completeness varies widely between Indian cities; absence of a facility in OSM does not mean it does not exist.",
            "Facility attributes (capacity, beds, operating status) are rarely mapped and are not inferred.",
            "Land-use values are counts of mapped OSM ways in the viewport, not measured land area.",
            "Public Overpass instances are rate-limited and occasionally unavailable.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        bbox = city.bbox or approx_viewport(city.lat, city.lon, city.zoom)
        s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
        b = f"({s},{w},{n},{e})"
        poi_query = "[out:json][timeout:60];(" + "".join(f"{sel}{b};" for sel, _ in OSM_LAYERS) + ");out center tags 6000;"
        count_query = "[out:json][timeout:40];" + "".join(
            f'way["landuse"="{v}"]{b};out count;' for v in LANDUSE_VALUES
        )
        last_err: IntegrationError | None = None
        for url in settings.overpass_urls:
            try:
                pois = await request_json(client, "POST", url, data={"data": poi_query}, timeout=75, retries=1)
                counts = await request_json(client, "POST", url, data={"data": count_query}, timeout=50, retries=1)
                return normalize_overpass(pois, counts, datetime.now(UTC))
            except IntegrationError as exc:
                last_err = exc
        assert last_err is not None
        raise last_err


def normalize_overpass(pois: dict[str, Any], counts: dict[str, Any] | None, now: datetime) -> SyncPayload:
    out = SyncPayload()
    elements = _require(pois, "elements", "overpass")
    for el in elements:
        tags = el.get("tags") or {}
        layer = _layer_for(tags)
        if layer is None:
            continue
        lat = el.get("lat") if "lat" in el else (el.get("center") or {}).get("lat")
        lon = el.get("lon") if "lon" in el else (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        props = {k: tags[k] for k in ("operator", "healthcare", "emergency", "wheelchair", "opening_hours",
                                       "addr:street", "addr:city", "beds", "operator:type") if k in tags}
        props["osm_type"] = el.get("type")
        out.features.append(
            NormalizedFeature(
                layer=layer, external_id=f"{el.get('type')}/{el.get('id')}", name=tags.get("name") or tags.get("name:en"),
                lat=lat, lon=lon, properties=props,
            )
        )
    if counts:
        count_elements = [e for e in counts.get("elements", []) if e.get("type") == "count"]
        for value, el in zip(LANDUSE_VALUES, count_elements, strict=False):
            ways = (el.get("tags") or {}).get("ways")
            if ways is None:
                continue
            out.measurements.append(
                NormalizedMeasurement(category="land_use", metric=f"osm_landuse_{value}_ways", value=float(ways),
                                      unit="count", observed_at=now.replace(minute=0, second=0, microsecond=0))
            )
    return out


# --------------------------------------------------------------------------- data.gov.in (OGD) — CPCB stations

POLLUTANT_MAP = {"PM2.5": "pm2_5", "PM10": "pm10", "NO2": "nitrogen_dioxide", "SO2": "sulphur_dioxide",
                 "OZONE": "ozone", "CO": "carbon_monoxide", "NH3": "ammonia"}
POLLUTANT_UNIT = {"carbon_monoxide": "mg/m³"}
IST = timedelta(hours=5, minutes=30)


class OgdCpcbAqi(Adapter):
    spec = SourceSpec(
        id="ogd-cpcb-aqi",
        name="CPCB real-time air quality (via Open Government Data Platform India)",
        kind="api",
        categories=("air_quality_station",),
        coverage="Cities with CPCB continuous ambient air quality monitoring stations (CAAQMS)",
        auth_required="data.gov.in API key (free registration) — SWARMOS_OGD_API_KEY",
        license="Government Open Data License – India (GODL)",
        attribution="Central Pollution Control Board, via data.gov.in",
        homepage="https://data.gov.in/",
        freshness_threshold_s=3 * HOUR,
        min_sync_interval_s=30 * 60,
        scope="city",
        limitations=(
            "Values are hourly station averages as published; the platform occasionally lags or omits stations.",
            "Field names of the OGD resource have changed historically; the adapter accepts both known schemas.",
        ),
        setup=(
            "1. Register at https://data.gov.in and generate an API key.\n"
            "2. Set SWARMOS_OGD_API_KEY in .env (never commit it).\n"
            "3. Restart the API and use 'Sync now' on the Integrations page."
        ),
    )

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        if not settings.ogd_api_key:
            return False, "SWARMOS_OGD_API_KEY is not set"
        return True, None

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        url = f"https://api.data.gov.in/resource/{settings.ogd_cpcb_resource_id}"
        records: list[dict[str, Any]] = []
        offset, limit = 0, 500
        for _ in range(10):  # bounded pagination
            data = await request_json(
                client, "GET", url,
                params={"api-key": settings.ogd_api_key, "format": "json", "limit": limit, "offset": offset,
                        "filters[city]": city.name},
                timeout=settings.http_timeout_seconds,
            )
            page = data.get("records") or []
            records.extend(page)
            total = int(data.get("total") or 0)
            offset += limit
            if not page or offset >= total:
                break
        return normalize_cpcb(records)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def normalize_cpcb(records: list[dict[str, Any]]) -> SyncPayload:
    out = SyncPayload()
    for r in records:
        metric = POLLUTANT_MAP.get(str(r.get("pollutant_id", "")).upper())
        avg = _num(r.get("avg_value", r.get("pollutant_avg")))
        if metric is None or avg is None:
            continue
        try:
            local = datetime.strptime(str(r.get("last_update")), "%d-%m-%Y %H:%M:%S")
        except ValueError:
            continue
        observed = (local - IST).replace(tzinfo=UTC)
        out.measurements.append(
            NormalizedMeasurement(
                category="air_quality_station", metric=metric, value=avg, unit=POLLUTANT_UNIT.get(metric, "µg/m³"),
                observed_at=observed, kind="observation", entity=str(r.get("station", "unknown"))[:120],
                lat=_num(r.get("latitude")), lon=_num(r.get("longitude")),
                meta={"min": _num(r.get("min_value", r.get("pollutant_min"))),
                      "max": _num(r.get("max_value", r.get("pollutant_max")))},
            )
        )
    return out


# --------------------------------------------------------------------------- Registry-only sources

class ImdAwaitingAccess(Adapter):
    spec = SourceSpec(
        id="imd",
        name="India Meteorological Department (IMD) data services",
        kind="api",
        categories=("weather", "warnings"),
        coverage="National",
        auth_required="Access by arrangement with IMD (IP allow-listing / registration)",
        license="As per IMD data policy",
        attribution="India Meteorological Department",
        homepage="https://mausam.imd.gov.in/",
        freshness_threshold_s=3 * HOUR,
        min_sync_interval_s=30 * 60,
        scope="none",
        limitations=("No public, documented, key-based API is available to SwarmOS by default. No adapter is active.",),
        setup=(
            "IMD APIs are provided on request. After access is granted, implement the response mapping in "
            "swarmos/integrations/adapters.py (ImdAwaitingAccess) against the documentation IMD provides, and "
            "set SWARMOS_IMD_API_BASE_URL."
        ),
    )

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        return False, "Awaiting IMD access; no adapter mapping is implemented without IMD's documentation"

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        raise IntegrationError("config", "IMD adapter is not available")


class DemoFixtures(Adapter):
    spec = SourceSpec(
        id="swarmos-demo",
        name="SwarmOS simulation (climate-calibrated generated data)",
        kind="fixture",
        categories=("traffic", "water", "energy", "wastewater", "budget", "assets", "incidents", "service_requests"),
        coverage="Every city without a live feed for a category (SWARMOS_SIMULATE_ALL_CITIES), otherwise pilot cities",
        auth_required="none",
        license="Generated by SwarmOS; climate normals derived from Kaggle 'India Daily Weather & Air Quality' (CC BY 4.0)",
        attribution="Simulated — SwarmOS (calibrated with Kaggle data, CC BY 4.0)",
        homepage=None,
        freshness_threshold_s=365 * DAY,
        min_sync_interval_s=DAY,
        scope="none",
        limitations=(
            "Every value from this source is SIMULATED: weather and air quality follow each city's monthly climate normals, "
            "and traffic, water, energy and incident records are scaled by city size. Real feeds always take precedence.",
            "Use for exploration and training only — not for operational decisions.",
        ),
        setup="Enabled by SWARMOS_DEMO_MODE=true. Data is generated automatically the first time a city is opened.",
    )

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        return (True, None) if settings.demo_mode else (False, "Simulation is disabled")

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        raise IntegrationError("config", "Simulated data is generated automatically when a city is opened, not synchronised")


class OperatorReports(Adapter):
    spec = SourceSpec(
        id="swarmos-operators",
        name="Operator-entered records (internal)",
        kind="internal",
        categories=("incidents", "service_requests"),
        coverage="All cities, per operator authorisation",
        auth_required="SwarmOS operator account",
        license="Internal",
        attribution="SwarmOS operators",
        homepage=None,
        freshness_threshold_s=365 * DAY,
        min_sync_interval_s=DAY,
        scope="none",
        limitations=("Accuracy depends on operator input; records are audited.",),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        raise IntegrationError("config", "Internal source; nothing to synchronise")


# --------------------------------------------------------------------------- MapmyIndia (Mappls) — nearby places

MAPPLS_LAYERS: tuple[tuple[str, str], ...] = (
    ("hospital", "hospital"),
    ("clinic", "clinic"),
    ("fire station", "fire_station"),
    ("police station", "police"),
    ("school", "school"),
    ("bus station", "bus_station"),
    ("railway station", "rail_station"),
    ("public toilet", "public_toilet"),
)
MAPPLS_ENDPOINTS = (
    "https://search.mappls.com/search/places/nearby/json",
    "https://atlas.mappls.com/api/places/nearby/json",
)


class MapplsPlaces(Adapter):
    spec = SourceSpec(
        id="mappls-places",
        name="MapmyIndia (Mappls) nearby places — public facilities",
        kind="api",
        categories=("public_infrastructure",),
        coverage="All Indian cities (commercial POI database)",
        auth_required="Mappls static access key — SWARMOS_MAPPLS_API_KEY",
        license="Mappls API terms of use",
        attribution="© MapmyIndia (Mappls)",
        homepage="https://about.mappls.com/api/",
        freshness_threshold_s=7 * DAY,
        min_sync_interval_s=DAY,
        scope="city",
        limitations=(
            "Results are limited to the search radius around the city centre and to a few pages per facility type.",
            "Some Mappls plans return place IDs (eLoc) without coordinates; such results are skipped.",
        ),
        setup=(
            "1. Create a project at https://auth.mappls.com/console and copy its static access key.\n"
            "2. Render → swarmos-api → Environment → add SWARMOS_MAPPLS_API_KEY.\n"
            "3. Use 'Sync now' on the Data Integrations page."
        ),
    )

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        if not settings.mappls_api_key:
            return False, "SWARMOS_MAPPLS_API_KEY is not set"
        return True, None

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        last_err: IntegrationError | None = None
        for url in MAPPLS_ENDPOINTS:
            try:
                pages: list[tuple[str, dict[str, Any]]] = []
                for keyword, layer in MAPPLS_LAYERS:
                    for page in (1, 2):
                        data = await request_json(
                            client, "GET", url,
                            params={"keywords": keyword, "refLocation": f"{city.lat},{city.lon}", "radius": settings.mappls_search_radius_m,
                                    "page": page, "region": "IND", "access_token": settings.mappls_api_key},
                            timeout=settings.http_timeout_seconds, retries=1,
                        )
                        pages.append((layer, data))
                        if len((data or {}).get("suggestedLocations") or []) < 10:
                            break
                payload = normalize_mappls(pages)
                if not payload.features:
                    raise IntegrationError("schema", "Mappls returned no places with coordinates for this key")
                return payload
            except IntegrationError as exc:
                last_err = exc
                if exc.kind == "auth":
                    break
        assert last_err is not None
        raise last_err


def _coord(item: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = _num(item.get(k))
        if v is not None and v != 0:
            return v
    return None


def normalize_mappls(pages: list[tuple[str, dict[str, Any]]]) -> SyncPayload:
    out = SyncPayload()
    seen: set[tuple[str, str]] = set()
    for layer, data in pages:
        if not isinstance(data, dict):
            raise IntegrationError("schema", "Mappls returned an unexpected response")
        for item in data.get("suggestedLocations") or []:
            lat = _coord(item, "latitude", "lat", "entryLatitude")
            lon = _coord(item, "longitude", "lng", "lon", "entryLongitude")
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            ext = str(item.get("eLoc") or item.get("mapplsPin") or f"{lat:.5f},{lon:.5f}")
            if (layer, ext) in seen:
                continue
            seen.add((layer, ext))
            props = {k: item[k] for k in ("placeAddress", "type", "distance", "keywords", "eLoc", "mapplsPin") if item.get(k) not in (None, "")}
            out.features.append(NormalizedFeature(layer=layer, external_id=f"mappls/{ext}", name=item.get("placeName"),
                                                  lat=lat, lon=lon, properties=props))
    return out


ADAPTERS: dict[str, Adapter] = {
    a.spec.id: a
    for a in (
        OpenMeteoWeather(),
        OpenMeteoAirQuality(),
        UsgsEarthquakes(),
        OsmOverpass(),
        OgdCpcbAqi(),
        MapplsPlaces(),
        ImdAwaitingAccess(),
        DemoFixtures(),
        OperatorReports(),
    )
}

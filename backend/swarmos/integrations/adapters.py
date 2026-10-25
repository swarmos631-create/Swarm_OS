"""Concrete source adapters.

Only sources with a verified, documented access method are implemented as live adapters. Each adapter
normalizes to :class:`NormalizedMeasurement` / :class:`NormalizedFeature` and never fabricates values: a
missing or null upstream value is skipped, not filled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from swarmos.cities import approx_viewport
from swarmos.config import Settings
from swarmos.integrations.base import (
    Adapter,
    IntegrationError,
    NormalizedFeature,
    NormalizedIncident,
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
        min_sync_interval_s=50 * 60,
        scope="batch",
        batch_size=25,
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

    async def fetch_batch(self, client: httpx.AsyncClient, settings: Settings, cities: list[City]) -> dict[str, SyncPayload]:
        # One request for many cities (Open-Meteo accepts comma-separated coordinates) keeps the whole catalogue
        # within the free API's per-IP limits.
        params = {
            "latitude": ",".join(f"{c.lat:.4f}" for c in cities),
            "longitude": ",".join(f"{c.lon:.4f}" for c in cities),
            "hourly": ",".join(WEATHER_HOURLY),
            "daily": ",".join([*WEATHER_DAILY, "sunrise", "sunset"]),
            "timezone": "GMT",
            "past_days": 2,
            "forecast_days": 3,
            "wind_speed_unit": "kmh",
        }
        data = await request_json(client, "GET", settings.open_meteo_base_url, params=params,
                                  timeout=max(30.0, settings.http_timeout_seconds), retries=1)
        rows = data if isinstance(data, list) else [data]
        if len(rows) != len(cities):
            raise IntegrationError("schema", f"open-meteo: expected {len(cities)} locations, got {len(rows)}")
        now = datetime.now(UTC)
        return {c.id: normalize_open_meteo_weather(row, now) for c, row in zip(cities, rows, strict=True)}


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


# --------------------------------------------------------------------------- MET Norway (backup weather)

MET_INSTANT = {  # MET Norway field -> (SwarmOS metric, unit, factor)
    "air_temperature": ("temperature_2m", "°C", 1.0),
    "apparent_air_temperature": ("apparent_temperature", "°C", 1.0),
    "relative_humidity": ("relative_humidity_2m", "%", 1.0),
    "wind_speed": ("wind_speed_10m", "km/h", 3.6),
    "wind_speed_of_gust": ("wind_gusts_10m", "km/h", 3.6),
    "cloud_area_fraction": ("cloud_cover", "%", 1.0),
}


class MetNorwayWeather(Adapter):
    spec = SourceSpec(
        id="met-norway-weather",
        name="MET Norway Locationforecast (backup weather)",
        kind="api",
        categories=("weather",),
        coverage="Any coordinate in India (global ECMWF-based forecast)",
        auth_required="none (identifying User-Agent required; see api.met.no terms)",
        license="CC BY 4.0 / NLOD 2.0 (MET Norway)",
        attribution="Weather forecast from the Norwegian Meteorological Institute (MET Norway)",
        homepage="https://api.met.no/weatherapi/locationforecast/2.0/documentation",
        freshness_threshold_s=3 * HOUR,
        min_sync_interval_s=55 * 60,
        scope="batch",
        batch_size=10,
        fallback_for="open-meteo-weather",
        limitations=(
            "Used only for cities where Open-Meteo could not be reached in the last two hours.",
            "Forecast model output, not station observations; no past hours and no solar radiation are provided.",
            "For official warnings, defer to India Meteorological Department (IMD) bulletins.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        data = await request_json(client, "GET", "https://api.met.no/weatherapi/locationforecast/2.0/complete",
                                  params={"lat": round(city.lat, 4), "lon": round(city.lon, 4)},
                                  timeout=settings.http_timeout_seconds, retries=1)
        return normalize_met_norway(data, datetime.now(UTC))

    async def fetch_batch(self, client: httpx.AsyncClient, settings: Settings, cities: list[City]) -> dict[str, SyncPayload]:
        out: dict[str, SyncPayload] = {}
        for c in cities:  # sequential: MET asks clients to stay well below 20 requests per second
            try:
                out[c.id] = await self.fetch(client, settings, c)
            except IntegrationError:
                continue
        if not out and cities:
            raise IntegrationError("network", "api.met.no returned no usable forecasts")
        return out


def normalize_met_norway(data: dict[str, Any], now: datetime) -> SyncPayload:
    out = SyncPayload()
    series = _require(_require(data, "properties", "met-norway"), "timeseries", "met-norway")
    hour = now.replace(minute=0, second=0, microsecond=0)
    daily_t: dict[datetime, list[float]] = {}
    daily_p: dict[datetime, list[float]] = {}
    for step in series:
        t = _parse_gmt(str(step.get("time", "")).replace("Z", "+00:00"))
        d = step.get("data") or {}
        nxt = (d.get("next_1_hours") or {}).get("details") or {}
        if "precipitation_amount" not in nxt:
            continue  # beyond ~2.5 days MET switches to 6-hour steps; keep the hourly part only
        kind: Literal["forecast", "model_analysis"] = "forecast" if t > hour else "model_analysis"
        details = (d.get("instant") or {}).get("details") or {}
        for field, (metric, unit, factor) in MET_INSTANT.items():
            v = details.get(field)
            if v is not None:
                out.measurements.append(NormalizedMeasurement(category="weather", metric=metric, value=round(float(v) * factor, 2),
                                                              unit=unit, observed_at=t, kind=kind))
        rain = float(nxt["precipitation_amount"])
        out.measurements.append(NormalizedMeasurement(category="weather", metric="precipitation", value=rain, unit="mm",
                                                      observed_at=t, kind=kind))
        day = t.replace(hour=0)
        if "air_temperature" in details:
            daily_t.setdefault(day, []).append(float(details["air_temperature"]))
        daily_p.setdefault(day, []).append(rain)
    today = hour.replace(hour=0)
    for day, temps in daily_t.items():
        if len(temps) < 20:
            continue  # only days the hourly forecast fully covers
        dkind: Literal["forecast", "model_analysis"] = "forecast" if day >= today else "model_analysis"
        for metric, value in (("daily_temperature_2m_max", max(temps)), ("daily_temperature_2m_min", min(temps)),
                              ("daily_precipitation_sum", round(sum(daily_p.get(day, [])), 1))):
            out.measurements.append(NormalizedMeasurement(category="weather", metric=metric, value=value,
                                                          unit="mm" if "precipitation" in metric else "°C",
                                                          observed_at=day, kind=dkind, meta={"derived_from": "hourly forecast"}))
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
        min_sync_interval_s=50 * 60,
        scope="batch",
        batch_size=25,
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

    async def fetch_batch(self, client: httpx.AsyncClient, settings: Settings, cities: list[City]) -> dict[str, SyncPayload]:
        params = {
            "latitude": ",".join(f"{c.lat:.4f}" for c in cities),
            "longitude": ",".join(f"{c.lon:.4f}" for c in cities),
            "hourly": ",".join(AQ_HOURLY),
            "timezone": "GMT",
            "past_days": 3,
            "forecast_days": 2,
        }
        data = await request_json(client, "GET", settings.open_meteo_aq_base_url, params=params,
                                  timeout=max(30.0, settings.http_timeout_seconds), retries=1)
        rows = data if isinstance(data, list) else [data]
        if len(rows) != len(cities):
            raise IntegrationError("schema", f"open-meteo-aq: expected {len(cities)} locations, got {len(rows)}")
        now = datetime.now(UTC)
        return {c.id: normalize_open_meteo_aq(row, now) for c, row in zip(cities, rows, strict=True)}


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
        scheduled=False,  # facilities now come from osm-vector-tiles; Overpass stays available on demand
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


# --------------------------------------------------------------------------- OpenStreetMap via vector tiles

OMT_LAYERS: dict[tuple[str, str | None], str] = {  # OpenMapTiles poi (class, subclass) -> SwarmOS layer
    ("hospital", "hospital"): "hospital",
    ("hospital", "clinic"): "clinic",
    ("hospital", "doctors"): "clinic",
    ("fire_station", None): "fire_station",
    ("police", None): "police",
    ("school", "school"): "school",
    ("bus", "bus_station"): "bus_station",
    ("railway", "station"): "rail_station",
    ("railway", "halt"): "rail_station",
    ("toilets", None): "public_toilet",
}
TILE_ZOOM = 14
TILE_RADIUS_DEG = 0.12  # ~13 km around the city centre (about 11 × 11 tiles)


def omt_layer(tags: dict[str, Any]) -> str | None:
    cls, sub = tags.get("class"), tags.get("subclass")
    return OMT_LAYERS.get((str(cls), str(sub))) or OMT_LAYERS.get((str(cls), None))


class OsmVectorTiles(Adapter):
    spec = SourceSpec(
        id="osm-vector-tiles",
        name="OpenStreetMap public facilities (OpenFreeMap vector tiles)",
        kind="api",
        categories=("public_infrastructure",),
        coverage="Any Indian city, about 13 km around the centre (community-mapped; completeness varies by city)",
        auth_required="none",
        license="ODbL 1.0 (OpenStreetMap data); tiles by OpenFreeMap",
        attribution="© OpenStreetMap contributors; tiles by OpenFreeMap (OpenMapTiles schema)",
        homepage="https://openfreemap.org",
        freshness_threshold_s=14 * DAY,
        min_sync_interval_s=7 * DAY,
        scope="city",
        limitations=(
            "OSM completeness varies between Indian cities; a facility missing from OSM may still exist.",
            "Covers about 13 km around the city centre; outer suburbs may be missing.",
            "Facility attributes such as beds or capacity are not part of the tile data.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        import asyncio

        from swarmos.integrations.mvt import point_features, tile_to_lonlat, tile_xy

        tilejson = await request_json(client, "GET", "https://tiles.openfreemap.org/planet", timeout=20)
        template = (tilejson.get("tiles") or [None])[0] if isinstance(tilejson, dict) else None
        if not template:
            raise IntegrationError("schema", "openfreemap: TileJSON has no tile URL")
        bbox = city.bbox or [city.lon - 0.2, city.lat - 0.2, city.lon + 0.2, city.lat + 0.2]
        west, east = max(bbox[0], city.lon - TILE_RADIUS_DEG), min(bbox[2], city.lon + TILE_RADIUS_DEG)
        south, north = max(bbox[1], city.lat - TILE_RADIUS_DEG), min(bbox[3], city.lat + TILE_RADIUS_DEG)
        x0, y0 = tile_xy(north, west, TILE_ZOOM)
        x1, y1 = tile_xy(south, east, TILE_ZOOM)
        tiles = [(x, y) for x in range(int(x0), int(x1) + 1) for y in range(int(y0), int(y1) + 1)]
        sem = asyncio.Semaphore(6)
        seen: set[str] = set()
        out = SyncPayload()
        failures = 0

        async def one(tx: int, ty: int) -> None:
            nonlocal failures
            async with sem:
                try:
                    r = await client.get(template.format(z=TILE_ZOOM, x=tx, y=ty), timeout=20)
                except httpx.HTTPError:
                    failures += 1
                    return
            if r.status_code == 204 or not r.content:
                return
            if r.status_code >= 400:
                failures += 1
                return
            feats, extent = point_features(r.content, "poi")
            for f in feats:
                layer = omt_layer(f.tags)
                if layer is None or not (0 <= f.x <= extent and 0 <= f.y <= extent):
                    continue  # skip buffer copies that belong to the neighbouring tile
                lon, lat = tile_to_lonlat(tx, ty, f.x, f.y, extent, TILE_ZOOM)
                name = f.tags.get("name:en") or f.tags.get("name_en") or f.tags.get("name")
                ext = f"omt/{layer}/{f.id}" if f.id else f"omt/{layer}/{round(lat, 5)},{round(lon, 5)}"
                if ext in seen:
                    continue
                seen.add(ext)
                out.features.append(NormalizedFeature(layer=layer, external_id=ext, name=str(name) if name else None,
                                                      lat=round(lat, 6), lon=round(lon, 6),
                                                      properties={"osm_class": f.tags.get("class"), "osm_subclass": f.tags.get("subclass")}))

        await asyncio.gather(*(one(x, y) for x, y in tiles))
        if failures > len(tiles) // 2:
            raise IntegrationError("network", f"openfreemap: {failures} of {len(tiles)} tiles could not be downloaded")
        out.notes.append(f"{len(out.features)} facilities from {len(tiles)} tiles")
        return out


# --------------------------------------------------------------------------- Official alerts (NDMA SACHET, GDACS)

CAP_SEVERITY = {"Extreme": "critical", "Severe": "high", "Moderate": "moderate", "Minor": "low", "Unknown": "low"}
CAP_NS = "{urn:oasis:names:tc:emergency:cap:1.2}"


def _alert_category(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("flood", "heavy rain", "very heavy", "extremely heavy", "inundat", "dam", "river")):
        return "flooding"
    if any(k in t for k in ("cyclone", "depression", "storm surge")):
        return "cyclone"
    if any(k in t for k in ("earthquake", "tremor")):
        return "earthquake"
    if any(k in t for k in ("fire", "forest fire")):
        return "fire"
    if any(k in t for k in ("heat", "hot", "warm night")):
        return "heat"
    if any(k in t for k in ("landslide", "avalanche")):
        return "landslide"
    return "weather"


def city_mentioned(city: City, text: str) -> bool:
    """True when the alert area names this city (or one of its aliases) as a whole word."""
    import re

    names = [city.name, *(city.aliases or [])]
    return any(re.search(r"(?<![a-z])" + re.escape(n.lower()) + r"(?![a-z])", text.lower()) for n in names if n and len(n) > 2)


class NdmaSachetAlerts(Adapter):
    spec = SourceSpec(
        id="ndma-sachet",
        name="NDMA SACHET — official disaster alerts (CAP)",
        kind="feed",
        categories=("alerts",),
        coverage="All of India (alerts from IMD, CWC, state disaster management authorities and others)",
        auth_required="none",
        license="Public domain (Government of India public alert feed)",
        attribution="Alerts from the National Disaster Management Authority's SACHET Common Alerting Protocol platform",
        homepage="https://sachet.ndma.gov.in",
        freshness_threshold_s=3 * HOUR,
        min_sync_interval_s=25 * 60,
        scope="batch",
        batch_size=1000,
        limitations=(
            "Alerts are matched to a city when the alert area names the city or its district; district-wide alerts are shown at the city centre.",
            "Only English-language alert versions are used; regional-language duplicates are skipped.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        return (await self.fetch_batch(client, settings, [city]))[city.id]

    async def fetch_batch(self, client: httpx.AsyncClient, settings: Settings, cities: list[City]) -> dict[str, SyncPayload]:
        import asyncio
        from email.utils import parsedate_to_datetime

        from defusedxml import ElementTree as SafeET

        from swarmos.integrations.http import request_bytes

        rss = SafeET.fromstring(await request_bytes(client, "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml",
                                                    timeout=25))
        now = datetime.now(UTC)
        links: list[str] = []
        for item in rss.iter("item"):
            pub, link = item.findtext("pubDate"), item.findtext("link")
            try:
                when = parsedate_to_datetime(pub) if pub else None
            except (TypeError, ValueError):
                when = None
            if link and when and now - when.astimezone(UTC) < timedelta(hours=24):
                links.append(link)
        sem = asyncio.Semaphore(4)
        alerts: list[dict[str, Any]] = []

        async def one(url: str) -> None:
            async with sem:
                try:
                    doc = SafeET.fromstring(await request_bytes(client, url, timeout=20, retries=0))
                except Exception:
                    return
            if (doc.findtext(f"{CAP_NS}status") or "") != "Actual" or (doc.findtext(f"{CAP_NS}msgType") or "") == "Cancel":
                return
            infos = doc.findall(f"{CAP_NS}info")
            info = next((i for i in infos if (i.findtext(f"{CAP_NS}language") or "").lower().startswith("en")), None)
            if info is None:
                return
            area = " ".join(a.findtext(f"{CAP_NS}areaDesc") or "" for a in info.findall(f"{CAP_NS}area"))
            headline = (info.findtext(f"{CAP_NS}headline") or info.findtext(f"{CAP_NS}event") or "").strip()
            if not headline or not area:
                return
            alerts.append({
                "id": doc.findtext(f"{CAP_NS}identifier") or url.rsplit("=", 1)[-1],
                "sender": doc.findtext(f"{CAP_NS}sender") or "NDMA SACHET",
                "event": info.findtext(f"{CAP_NS}event") or "",
                "headline": headline,
                "severity": CAP_SEVERITY.get(info.findtext(f"{CAP_NS}severity") or "Unknown", "low"),
                "instruction": info.findtext(f"{CAP_NS}instruction") or "",
                "area": area,
                "sent": _iso(doc.findtext(f"{CAP_NS}sent")) or now,
                "expires": _iso(info.findtext(f"{CAP_NS}expires")),
            })

        await asyncio.gather(*(one(u) for u in links[:80]))
        out: dict[str, SyncPayload] = {}
        for c in cities:
            p = SyncPayload(incidents=[])
            for a in alerts:
                if city_mentioned(c, a["area"]):
                    assert p.incidents is not None
                    p.incidents.append(NormalizedIncident(
                        external_id=str(a["id"])[:60], title=f"Official alert: {a['headline']}"[:200],
                        description=(f"{a['event']} — issued by {a['sender']}. Area: {a['area'][:600]}. "
                                     f"{a['instruction']} District-wide alert shown at the city centre.").strip(),
                        category=_alert_category(f"{a['event']} {a['headline']}"), severity=a["severity"],
                        lat=c.lat, lon=c.lon, reported_at=a["sent"], expires_at=a["expires"], department="Disaster management"))
            out[c.id] = p
        return out


def _iso(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


GDACS_RADIUS_KM = {"FL": 200.0, "TC": 350.0, "EQ": 300.0, "VO": 100.0, "WF": 60.0, "DR": 250.0}
GDACS_CATEGORY = {"FL": "flooding", "TC": "cyclone", "EQ": "earthquake", "VO": "volcano", "WF": "fire", "DR": "drought"}
GDACS_SEVERITY = {"Green": "low", "Orange": "high", "Red": "critical"}


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


class GdacsAlerts(Adapter):
    spec = SourceSpec(
        id="gdacs",
        name="GDACS — Global Disaster Alert and Coordination System (India events)",
        kind="api",
        categories=("alerts",),
        coverage="Floods, tropical cyclones, earthquakes, droughts and wildfires affecting India",
        auth_required="none",
        license="Free to use with attribution (European Commission JRC / UN OCHA)",
        attribution="Disaster events from GDACS (European Commission Joint Research Centre and UN OCHA)",
        homepage="https://www.gdacs.org",
        freshness_threshold_s=6 * HOUR,
        min_sync_interval_s=55 * 60,
        scope="batch",
        batch_size=1000,
        limitations=(
            "Events are matched to cities by distance from the event's reported location (radius depends on the hazard).",
            "GDACS alert levels estimate humanitarian impact, not local severity.",
        ),
    )

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        return (await self.fetch_batch(client, settings, [city]))[city.id]

    async def fetch_batch(self, client: httpx.AsyncClient, settings: Settings, cities: list[City]) -> dict[str, SyncPayload]:
        since = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
        data = await request_json(client, "GET", "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
                                  params={"country": "India", "fromdate": since}, timeout=30, retries=1)
        feats = (data or {}).get("features") or [] if isinstance(data, dict) else []
        now = datetime.now(UTC)
        out = {c.id: SyncPayload(incidents=[]) for c in cities}
        for f in feats:
            p = f.get("properties") or {}
            coords = (f.get("geometry") or {}).get("coordinates") or []
            etype = str(p.get("eventtype") or "")
            if etype not in GDACS_RADIUS_KM or len(coords) < 2:
                continue
            ends = _iso(p.get("todate"))
            current = str(p.get("iscurrent")).lower() == "true"
            expires = None if current else (ends or now)
            if expires is not None and expires < now - timedelta(days=3):
                continue  # ended more than three days ago: history, not a live alert
            for c in cities:
                if _km(c.lat, c.lon, float(coords[1]), float(coords[0])) > GDACS_RADIUS_KM[etype]:
                    continue
                incs = out[c.id].incidents
                assert incs is not None
                incs.append(NormalizedIncident(
                    external_id=f"{etype}-{p.get('eventid')}-{p.get('episodeid')}"[:60],
                    title=f"GDACS {p.get('alertlevel', '')} alert: {p.get('name') or p.get('eventname') or etype}"[:200],
                    description=f"{p.get('htmldescription') or p.get('description') or ''} Source: {p.get('source') or 'GDACS'}. "
                                f"Report: {(p.get('url') or {}).get('report', '')}".strip(),
                    category=GDACS_CATEGORY.get(etype, "weather"),
                    severity=GDACS_SEVERITY.get(str(p.get("alertlevel")), "low"),  # type: ignore[arg-type]
                    lat=c.lat, lon=c.lon,  # region-wide event: shown at the city centre, epicentre in the report
                    reported_at=_iso(p.get("fromdate")) or now, expires_at=expires, department="Disaster management"))
        return out


# --------------------------------------------------------------------------- TomTom traffic flow

TOMTOM_PROBES = (  # (label, km north, km east) from the city centre; TomTom snaps each to the nearest major road
    ("City centre", 0.0, 0.0), ("North approach", 4.0, 0.0), ("South approach", -4.0, 0.0),
    ("East approach", 0.0, 4.0), ("West approach", 0.0, -4.0), ("North-east", 7.0, 7.0),
    ("South-west", -7.0, -7.0), ("North-west", 7.0, -7.0), ("South-east", -7.0, 7.0),
    ("Outer north", 10.0, 0.0), ("Outer south", -10.0, 0.0), ("Outer east", 0.0, 10.0),
)


class TomTomTraffic(Adapter):
    spec = SourceSpec(
        id="tomtom-traffic",
        name="TomTom Traffic Flow — live road speeds",
        kind="api",
        categories=("traffic",),
        coverage="Major roads in any Indian city (sampled probe points around the centre)",
        auth_required="TomTom developer API key — SWARMOS_TOMTOM_API_KEY (free tier about 2,500 requests/day)",
        license="TomTom developer terms (display with attribution; no redistribution of raw data)",
        attribution="Traffic data © TomTom",
        homepage="https://developer.tomtom.com/traffic-api/documentation/traffic-flow/flow-segment-data",
        freshness_threshold_s=2 * HOUR,
        min_sync_interval_s=60 * 60,
        scope="city",
        limitations=(
            "Speeds are sampled at a few probe points around the city centre, snapped to the nearest major road — not every corridor.",
            "Synchronised hourly for cities that are opened, to stay within the free daily request quota.",
        ),
        setup=(
            "1. Sign up at https://developer.tomtom.com and create an API key (the free tier is enough).\n"
            "2. Render → swarmos-api → Environment → add SWARMOS_TOMTOM_API_KEY and save.\n"
            "3. Open a city; its live road speeds sync automatically within a minute."
        ),
    )

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        return (True, None) if settings.tomtom_api_key else (False, "SWARMOS_TOMTOM_API_KEY is not set")

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        import math

        assert city is not None
        out = SyncPayload()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        seen: set[tuple[float, float]] = set()
        errors: list[IntegrationError] = []
        for label, dn, de in TOMTOM_PROBES[: settings.tomtom_points_per_city]:
            lat = city.lat + dn / 111.0
            lon = city.lon + de / (111.0 * max(0.2, math.cos(math.radians(city.lat))))
            try:
                data = await request_json(
                    client, "GET", "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json",
                    params={"point": f"{lat:.5f},{lon:.5f}", "unit": "KMPH", "key": settings.tomtom_api_key},
                    timeout=settings.http_timeout_seconds, retries=1)
            except IntegrationError as exc:
                if exc.kind == "auth":
                    raise
                errors.append(exc)
                continue
            seg = (data or {}).get("flowSegmentData") or {}
            cur, free = _num(seg.get("currentSpeed")), _num(seg.get("freeFlowSpeed"))
            coords = ((seg.get("coordinates") or {}).get("coordinate") or [{}])
            mid = coords[len(coords) // 2] if coords else {}
            plat, plon = _num(mid.get("latitude")) or lat, _num(mid.get("longitude")) or lon
            key = (round(plat, 3), round(plon, 3))
            if cur is None or not free or key in seen:
                continue  # no road nearby, or the same segment as another probe
            seen.add(key)
            entity = f"{label} (TomTom FRC{str(seg.get('frc', '')).replace('FRC', '')})"
            meta = {"lat": plat, "lon": plon, "confidence": seg.get("confidence"), "road_closure": seg.get("roadClosure")}
            out.measurements.append(NormalizedMeasurement(category="traffic", metric="speed_kmph", value=cur, unit="km/h",
                                                          observed_at=now, entity=entity, lat=plat, lon=plon, meta=meta))
            out.measurements.append(NormalizedMeasurement(category="traffic", metric="freeflow_speed_kmph", value=free, unit="km/h",
                                                          observed_at=now, entity=entity, lat=plat, lon=plon, meta=meta))
        if not out.measurements:
            raise errors[0] if errors else IntegrationError("schema", "TomTom returned no road segments near this city")
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
            "2. Render → swarmos-api → Environment → add SWARMOS_OGD_API_KEY and save (never commit it).\n"
            "3. Open any city; its CPCB stations sync automatically (or use 'Sync now' on the Integrations page)."
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
        # CPCB spells some cities differently (e.g. Bengaluru/Bangalore); try the name, then each alias.
        for name in [city.name, *(city.aliases or [])]:
            offset, limit = 0, 500
            for _ in range(10):  # bounded pagination
                data = await request_json(
                    client, "GET", url,
                    params={"api-key": settings.ogd_api_key, "format": "json", "limit": limit, "offset": offset,
                            "filters[city]": name},
                    timeout=settings.http_timeout_seconds,
                )
                page = data.get("records") or []
                records.extend(page)
                total = int(data.get("total") or 0)
                offset += limit
                if not page or offset >= total:
                    break
            if records:
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


_mappls_token: tuple[float, str] | None = None


async def _mappls_bearer(client: httpx.AsyncClient, settings: Settings) -> str:
    """OAuth client-credentials token for Mappls REST APIs (cached until shortly before it expires)."""
    import time

    global _mappls_token
    if _mappls_token and _mappls_token[0] > time.time() + 60:
        return _mappls_token[1]
    data = await request_json(client, "POST", "https://outpost.mappls.com/api/security/oauth/token",
                              data={"grant_type": "client_credentials", "client_id": settings.mappls_client_id,
                                    "client_secret": settings.mappls_client_secret}, timeout=20, retries=1)
    token = (data or {}).get("access_token") if isinstance(data, dict) else None
    if not token:
        raise IntegrationError("auth", "Mappls did not issue an access token for these client credentials")
    _mappls_token = (time.time() + float(data.get("expires_in") or 3600), str(token))
    return str(token)


class MapplsPlaces(Adapter):
    spec = SourceSpec(
        id="mappls-places",
        name="MapmyIndia (Mappls) nearby places — public facilities",
        kind="api",
        categories=("public_infrastructure",),
        coverage="All Indian cities (commercial POI database)",
        auth_required="Mappls OAuth client (SWARMOS_MAPPLS_CLIENT_ID + SWARMOS_MAPPLS_CLIENT_SECRET) or a server REST key (SWARMOS_MAPPLS_API_KEY)",
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
            "1. In the Mappls console (https://auth.mappls.com/console) open your project → Credentials.\n"
            "2. Copy the Client ID and Client Secret (preferred), or create a REST key without a website-domain restriction.\n"
            "3. Render → swarmos-api → Environment → add SWARMOS_MAPPLS_CLIENT_ID and SWARMOS_MAPPLS_CLIENT_SECRET "
            "(or SWARMOS_MAPPLS_API_KEY) and save; Render restarts the service.\n"
            "4. The browser key VITE_MAPPLS_KEY stays as it is — it is only used by the website map."
        ),
    )

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        if settings.mappls_client_id and settings.mappls_client_secret:
            return True, None
        if settings.mappls_api_key:
            return True, None
        return False, "Set SWARMOS_MAPPLS_CLIENT_ID + SWARMOS_MAPPLS_CLIENT_SECRET (or SWARMOS_MAPPLS_API_KEY) on the server"

    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload:
        assert city is not None
        attempts: list[tuple[str, dict[str, Any], dict[str, str]]]
        if settings.mappls_client_id and settings.mappls_client_secret:
            token = await _mappls_bearer(client, settings)
            attempts = [("https://atlas.mappls.com/api/places/nearby/json", {}, {"Authorization": f"bearer {token}"})]
        else:
            attempts = [(url, {"access_token": settings.mappls_api_key}, {}) for url in MAPPLS_ENDPOINTS]
        last_err: IntegrationError | None = None
        for url, auth_params, headers in attempts:
            try:
                pages: list[tuple[str, dict[str, Any]]] = []
                for keyword, layer in MAPPLS_LAYERS:
                    for page in (1, 2):
                        data = await request_json(
                            client, "GET", url,
                            params={"keywords": keyword, "refLocation": f"{city.lat},{city.lon}", "radius": settings.mappls_search_radius_m,
                                    "page": page, "region": "IND", **auth_params},
                            headers=headers or None, timeout=settings.http_timeout_seconds, retries=1,
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
        if last_err.kind == "auth":
            raise IntegrationError("auth", f"{last_err} — the key is probably restricted to the website domain; "
                                           "use the project's Client ID/Secret or a server key (see setup)")
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
        MetNorwayWeather(),
        OpenMeteoAirQuality(),
        UsgsEarthquakes(),
        NdmaSachetAlerts(),
        GdacsAlerts(),
        OsmVectorTiles(),
        OsmOverpass(),
        OgdCpcbAqi(),
        MapplsPlaces(),
        TomTomTraffic(),
        ImdAwaitingAccess(),
        DemoFixtures(),
        OperatorReports(),
    )
}

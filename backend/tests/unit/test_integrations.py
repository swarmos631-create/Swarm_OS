import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from swarmos.config import get_settings
from swarmos.integrations.adapters import (
    OgdCpcbAqi,
    normalize_cpcb,
    normalize_open_meteo_aq,
    normalize_open_meteo_weather,
    normalize_overpass,
    normalize_usgs,
)
from swarmos.integrations.base import IntegrationError
from swarmos.integrations.http import request_json
from swarmos.integrations.sync import freshness
from swarmos.models import DataSource

FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def load(name):
    return json.loads((FIX / name).read_text())


def test_open_meteo_weather_normalisation_separates_forecast_from_analysis():
    p = normalize_open_meteo_weather(load("open_meteo_weather.json"), NOW)
    temps = [m for m in p.measurements if m.metric == "temperature_2m"]
    assert {m.kind for m in temps} == {"model_analysis", "forecast"}
    assert all(m.observed_at.tzinfo is not None for m in p.measurements)
    # null upstream values are skipped, never filled
    assert not any(m.metric == "wind_gusts_10m" and m.observed_at == datetime(2026, 10, 24, 11, tzinfo=UTC) for m in p.measurements)
    daylight = [m for m in p.measurements if m.metric == "daily_daylight_hours"]
    assert daylight and "sunrise_utc" in daylight[0].meta


def test_open_meteo_aq_normalisation():
    p = normalize_open_meteo_aq(load("open_meteo_aq.json"), NOW)
    assert {m.metric for m in p.measurements} >= {"pm2_5", "pm10"}
    assert all(m.unit == "µg/m³" for m in p.measurements)


def test_open_meteo_missing_keys_is_schema_error():
    with pytest.raises(IntegrationError) as e:
        normalize_open_meteo_weather({"latitude": 1}, NOW)
    assert e.value.kind == "schema"


def test_usgs_filters_to_indian_region():
    p = normalize_usgs(load("usgs.json"))
    assert [f.external_id for f in p.features] == ["us7000inside"]
    assert p.features[0].properties["magnitude"] == 4.6


def test_overpass_normalisation_layers_and_counts():
    p = normalize_overpass(load("overpass.json"), load("overpass_counts.json"), NOW)
    layers = sorted(f.layer for f in p.features)
    assert layers == ["fire_station", "hospital", "school"]
    hospital = next(f for f in p.features if f.layer == "hospital")
    assert hospital.properties["wheelchair"] == "yes" and hospital.lat == 12.95
    assert {m.metric: m.value for m in p.measurements}["osm_landuse_residential_ways"] == 1200


def test_overpass_normalisation_keeps_community_mapped_street_lamps():
    payload = {"elements": [{
        "type": "node", "id": 777, "lat": 12.97, "lon": 77.59,
        "tags": {"highway": "street_lamp", "lit": "yes", "lamp_type": "LED", "ref": "pole-7"},
    }]}
    result = normalize_overpass(payload, None, NOW)
    assert len(result.features) == 1
    assert result.features[0].layer == "street_light"
    assert result.features[0].external_id == "node/777"
    assert result.features[0].properties["lit"] == "yes"
    assert result.features[0].properties["lamp_type"] == "LED"


def test_cpcb_normalisation_accepts_both_schemas_and_converts_ist():
    p = normalize_cpcb(load("cpcb.json")["records"])
    assert len(p.measurements) == 2
    pm = next(m for m in p.measurements if m.metric == "pm2_5")
    assert pm.observed_at == datetime(2026, 10, 24, 6, 30, tzinfo=UTC)
    assert pm.entity.startswith("Silk Board")


def test_cpcb_requires_key():
    ok, reason = OgdCpcbAqi().configured(get_settings())
    assert not ok and "SWARMOS_OGD_API_KEY" in reason


async def test_http_retries_then_succeeds_and_classifies_errors():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await request_json(c, "GET", "https://example.test/x", backoff_s=0) == {"ok": True}
    assert calls["n"] == 2

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as c:
        with pytest.raises(IntegrationError) as e:
            await request_json(c, "GET", "https://example.test/x", backoff_s=0)
        assert e.value.kind == "auth"

    def timeout(request):
        raise httpx.ReadTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as c:
        with pytest.raises(IntegrationError) as e:
            await request_json(c, "GET", "https://example.test/x", retries=1, backoff_s=0)
        assert e.value.kind == "timeout"


def test_freshness_states():
    src = DataSource(id="x", freshness_threshold_s=3600)
    assert freshness(src, None, NOW)["state"] == "never"
    assert freshness(src, datetime(2026, 10, 24, 11, 30, tzinfo=UTC), NOW)["state"] == "fresh"
    assert freshness(src, datetime(2026, 10, 24, 9, 0, tzinfo=UTC), NOW)["state"] == "stale"


def test_normalize_mappls_keeps_only_places_with_coordinates():
    from swarmos.integrations.adapters import normalize_mappls

    pages = [
        ("hospital", {"suggestedLocations": [
            {"eLoc": "AB12CD", "placeName": "City Hospital", "placeAddress": "MI Road", "latitude": 26.91, "longitude": 75.78, "distance": 900},
            {"eLoc": "AB12CD", "placeName": "City Hospital", "latitude": 26.91, "longitude": 75.78},  # duplicate page
            {"eLoc": "NOCOORD", "placeName": "No coordinates"},
        ]}),
        ("police", {"suggestedLocations": [{"mapplsPin": "XY9", "placeName": "Thana", "entryLatitude": "26.9", "entryLongitude": "75.8"}]}),
    ]
    out = normalize_mappls(pages)
    assert [(f.layer, f.external_id, f.name) for f in out.features] == [
        ("hospital", "mappls/AB12CD", "City Hospital"), ("police", "mappls/XY9", "Thana")]
    assert out.features[0].properties["placeAddress"] == "MI Road"


def test_mappls_adapter_requires_key():
    from swarmos.config import Settings
    from swarmos.integrations.adapters import ADAPTERS

    a = ADAPTERS["mappls-places"]
    assert a.configured(Settings(mappls_api_key=None))[0] is False
    assert a.configured(Settings(mappls_api_key="k"))[0] is True


def test_met_norway_normalisation_hourly_and_daily():
    from swarmos.integrations.adapters import normalize_met_norway

    steps = []
    for h in range(30):
        t = datetime(2026, 10, 24, 0, tzinfo=UTC).timestamp() + h * 3600
        steps.append({"time": datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "data": {"instant": {"details": {"air_temperature": 20 + h % 10, "relative_humidity": 70, "wind_speed": 2.0}},
                               "next_1_hours": {"details": {"precipitation_amount": 0.5}}}})
    out = normalize_met_norway({"properties": {"timeseries": steps}}, NOW)
    metrics = {m.metric for m in out.measurements}
    assert {"temperature_2m", "relative_humidity_2m", "wind_speed_10m", "precipitation", "daily_temperature_2m_max"} <= metrics
    wind = next(m for m in out.measurements if m.metric == "wind_speed_10m")
    assert wind.value == 7.2 and wind.unit == "km/h"  # m/s converted
    assert any(m.kind == "forecast" for m in out.measurements) and any(m.kind == "model_analysis" for m in out.measurements)


def test_alert_city_matching_and_categories():
    from swarmos.integrations.adapters import _alert_category, city_mentioned
    from swarmos.models import City

    chennai = City(id="chennai", name="Chennai", lat=13.08, lon=80.27, aliases=["Madras"])
    assert city_mentioned(chennai, "Chengalpattu, Chennai, Coimbatore districts of Tamil Nadu")
    assert not city_mentioned(chennai, "Chengalpattu, Coimbatore districts of Tamil Nadu")
    assert _alert_category("Very Heavy Rain") == "flooding" and _alert_category("Thunderstorm and lightning") == "weather"


def test_omt_poi_layer_mapping():
    from swarmos.integrations.adapters import omt_layer

    assert omt_layer({"class": "hospital", "subclass": "hospital"}) == "hospital"
    assert omt_layer({"class": "hospital", "subclass": "clinic"}) == "clinic"
    assert omt_layer({"class": "police", "subclass": "police"}) == "police"
    assert omt_layer({"class": "bus", "subclass": "bus_stop"}) is None


def test_openaq_requires_key():
    from swarmos.integrations.adapters import OpenAqStations

    ok, reason = OpenAqStations().configured(get_settings())
    assert not ok and "SWARMOS_OPENAQ_API_KEY" in reason


def test_openaq_unit_conversion():
    from swarmos.integrations.adapters import openaq_value

    assert openaq_value("pm2_5", 41.5, "µg/m³") == 41.5
    assert openaq_value("nitrogen_dioxide", 0.02, "ppm") == 37.6
    assert openaq_value("carbon_monoxide", 1200, "µg/m³") == 1.2
    assert openaq_value("carbon_monoxide", 1.0, "ppm") == 1.145
    assert openaq_value("pm10", -3, "µg/m³") is None
    assert openaq_value("pm10", 5, "particles/cm³") is None


async def test_openaq_fetch_picks_fresh_stations_and_normalises():
    from types import SimpleNamespace

    from swarmos.integrations.adapters import OpenAqStations

    recent = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    locations = {"results": [
        {"id": 1, "name": "Anand Vihar, Delhi - DPCC", "coordinates": {"latitude": 28.65, "longitude": 77.31},
         "datetimeLast": {"utc": recent}, "provider": {"name": "CPCB"},
         "sensors": [{"id": 11, "parameter": {"name": "pm25", "units": "µg/m³"}},
                     {"id": 12, "parameter": {"name": "no2", "units": "ppm"}},
                     {"id": 13, "parameter": {"name": "temperature", "units": "c"}}]},
        {"id": 2, "name": "Dead station", "coordinates": {"latitude": 28.6, "longitude": 77.2},
         "datetimeLast": {"utc": "2019-01-01T00:00:00Z"},
         "sensors": [{"id": 21, "parameter": {"name": "pm25", "units": "µg/m³"}}]},
    ]}
    latest = {"results": [
        {"sensorsId": 11, "value": 182.0, "datetime": {"utc": recent}},
        {"sensorsId": 12, "value": 0.05, "datetime": {"utc": recent}},
        {"sensorsId": 13, "value": 24.0, "datetime": {"utc": recent}},
    ]}
    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert request.headers["X-API-Key"] == "k"
        return httpx.Response(200, json=latest if request.url.path.endswith("/latest") else locations)

    settings = get_settings().model_copy(update={"openaq_api_key": "k"})
    city = SimpleNamespace(id="delhi", name="Delhi", lat=28.61, lon=77.21)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await OpenAqStations().fetch(c, settings, city)
    assert seen == ["/v3/locations", "/v3/locations/1/latest"]  # the stale station is skipped
    got = {m.metric: m.value for m in out.measurements}
    assert got == {"pm2_5": 182.0, "nitrogen_dioxide": 94.0}
    assert all(m.category == "air_quality_station" and m.entity.startswith("Anand Vihar") for m in out.measurements)

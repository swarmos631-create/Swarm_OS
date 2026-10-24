# Data integrations

Adapters live in `backend/swarmos/integrations/adapters.py`. Each one has a `SourceSpec` (coverage, licence,
attribution, auth, freshness threshold, minimum sync interval, scope, limitations, setup guide) and normalises data
into validated `NormalizedMeasurement` / `NormalizedFeature` records. Upstream nulls are skipped, never filled.

| Source | Status out of the box | Data | Notes |
|---|---|---|---|
| Open-Meteo Weather | **Live** (no key) | Hourly temperature, humidity, apparent temp, precipitation, wind, gusts, cloud, shortwave radiation; daily max/min, rain, UV, sunrise/sunset | Numerical model output, labelled `model_analysis` / `forecast`. Non-commercial terms. |
| Open-Meteo Air Quality (CAMS) | **Live** (no key) | PM2.5, PM10, NO₂, SO₂, O₃, CO | ~40 km model grid, **not** CPCB stations. The NAQI shown is "indicative". |
| USGS Earthquakes | **Live** (no key) | M ≥ 2.5, 7 days, filtered to 60–100°E / 0–40°N | NCS India is the authoritative national source. |
| OpenStreetMap / Overpass | **Live** (no key; fair use) | Hospitals, clinics, fire, police, schools, bus/rail stations, water towers, substations, toilets; land-use way counts | Synced at most daily per city; mirrors tried in turn; completeness varies by city. |
| CPCB via data.gov.in | **Awaiting credentials** | Station-level hourly pollutants | Set `SWARMOS_OGD_API_KEY`; paginated; tolerant of both known field schemas. |
| IMD | **Awaiting access** | Weather, warnings | No public key-based API. Registry entry only; no adapter is guessed. |
| SwarmOS demo fixtures | **Demo** | Traffic, water, energy, wastewater, budget, resources, assets, incidents, complaints | Pilot cities only; every row is tagged `demo`. |
| Operator records | Internal | Incidents, service requests | Audited. |

## Synchronisation

* `swarmos sync <city>` runs once; the worker schedules `SWARMOS_SYNC_CITIES` every `SWARMOS_SYNC_INTERVAL_MINUTES`.
* Operators can trigger **Sync now** in the UI. Requests faster than a source's `min_sync_interval_s` are skipped
  unless forced. Manual triggers are de-duplicated in 30 s buckets.
* The HTTP client (`integrations/http.py`) applies timeouts and bounded retries with exponential backoff and
  jitter, honours `Retry-After`, and classifies errors (`timeout`, `network`, `http_error`, `rate_limited`, `auth`, `schema`).
* Every attempt writes a `sync_runs` row. Source status becomes `connected`, `degraded` (has succeeded before, now
  failing) or `failing`. Freshness is `fresh` / `stale` / `never` against the source threshold, per city.
* An integration failure never breaks the dashboard; the affected cards show *Not available* or *Stale*.

## Connecting real city data

1. **CPCB stations**: register at data.gov.in, set `SWARMOS_OGD_API_KEY`, restart, then click *Sync now* on CPCB.
   Air Quality Analyst automatically prefers station observations over the model.
2. **Municipal feeds** (traffic ATCS, SCADA, DMA flow meters, STP telemetry, grievance portals): write an adapter
   that emits the same categories, metrics and entities the demo fixtures use (`traffic/speed_kmph` per corridor,
   `water/flow_m3h` per `DMA-xx`, `energy/power_demand_mw`, `wastewater/stp_inflow_mld`, …), with `provenance="live"`.
   Agents prefer live series over demo automatically (`CityData.one_series`).
3. Set `SWARMOS_DEMO_MODE=false` once real feeds cover a city, and remove demo rows by re-running seeding with
   `--no-demo` after deleting them (a small, explicit migration rather than an automatic wipe).

## Adding an adapter

```python
class MyFeed(Adapter):
    spec = SourceSpec(id="my-feed", name="…", kind="api", categories=("traffic",), coverage="…",
                      auth_required="API key (SWARMOS_MY_FEED_KEY)", license="…", attribution="…",
                      homepage="…", freshness_threshold_s=900, min_sync_interval_s=300, scope="city",
                      limitations=("…",), setup="1. … 2. …")

    def configured(self, settings): ...            # no network I/O
    async def fetch(self, client, settings, city): # return SyncPayload(measurements=[…], features=[…])
```

Register it in `ADAPTERS` and add a fixture test like those in `tests/unit/test_integrations.py`. Never hard-code
keys, scrape restricted systems, or mark a source live before a real fetch succeeds.

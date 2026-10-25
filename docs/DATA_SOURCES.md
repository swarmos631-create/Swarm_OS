# Data sources and credentials

SwarmOS distinguishes live observations, model analyses/forecasts, cached records, operator reports, derived values, and simulated demo data. A provider returning a response does not automatically make that value a ground-truth observation.

## Enabled without a key

| Provider | What it contributes | Important limits |
|---|---|---|
| Open-Meteo Forecast API | Weather forecast/model analysis series | Model output, not an IMD station observation or warning; CC BY 4.0 attribution applies. |
| Open-Meteo Air Quality / CAMS | Modelled pollutant concentrations | Not CPCB station readings; derived Indian AQI is indicative. |
| MET Norway | Weather fallback | Forecast/model output; identify source and data time. |
| USGS GeoJSON | Recent earthquake events | Event feed and geographic radius filters; earthquake coverage only. |
| OpenStreetMap via Overpass | Mapped public facilities, bus/rail stations, street-lamp points and land-use counts; stored facility records may include `wheelchair=*`, and lamp rows may include `lit`/lamp-type tags used as nearby SakhiRoute context | Community mapping is incomplete and uneven, and tags are not independently audited. A mapped `lit` tag does not establish that a lamp works or illuminates a route. Do not treat missing points or missing tags as absence/accessibility. Respect fair-use limits and ODbL attribution. |
| [OpenFreeMap](https://openfreemap.org/) / OpenMapTiles | Keyless vector-map fallback using OpenStreetMap data | Public service is free but has no SLA; availability and styles may change ([terms](https://openfreemap.org/tos/)). MapLibre supplies required visible attribution. Each tile request reveals the viewed map area to the tile service. |
| NDMA SACHET / GDACS | Disaster alerts | Alert coverage and severity semantics differ; alert proximity is not a local impact determination. |
| FOSSGIS OSRM routing | Best-effort route alternatives for foot, bike and car | Provider may log coordinates, has no uptime guarantee and requests max one per second. SwarmOS does not persist route history. |
| Mappls Place Search (optional) | Indian address and place suggestions for SakhiRoute | Uses the existing domain-restricted browser map key when Place Search is enabled for that key. Search text is sent to Mappls; manual coordinates and map clicks remain available. |
| Mappls map SDK (optional) | Primary interactive base map when its browser key and SDK are available | Browser map key only. The keyless OpenFreeMap fallback renders route geometry and supports map-click endpoint selection when Mappls is unavailable. |

## Optional credentials for real observations and services

The adapters already support these environment variables. No credentials are committed and none is embedded in the browser bundle.

| Variable | Purpose | Setup |
|---|---|---|
| SWARMOS_OGD_API_KEY | CPCB continuous air-monitoring records via data.gov.in | Create a free account/key on data.gov.in, then set this secret in Railway's backend service variables. The adapter syncs CPCB after registry refresh. |
| SWARMOS_TOMTOM_API_KEY | TomTom traffic flow readings | Use only within the developer account's current quota, attribution, and storage terms. |
| SWARMOS_MAPPLS_CLIENT_ID and SWARMOS_MAPPLS_CLIENT_SECRET, or SWARMOS_MAPPLS_API_KEY | Mappls nearby facilities | Server-side credentials only. Do not reuse a domain-restricted browser map token. |
| IMD_API_BASE_URL | IMD data service endpoint | IMD access is by arrangement; no public keyless endpoint is assumed. |

Set backend secrets in Railway service variables, never in Git or Vite-prefixed browser variables. Existing browser-only map key settings are for rendering that map SDK; they must never be used as backend credentials.

## How to increase real-data coverage safely

1. Configure SWARMOS_SYNC_CITIES to the cities you operate.
2. Set only credentials you have obtained and whose use is permitted by the provider.
3. Trigger a source sync from Integrations and inspect its last-run status, record count, attribution and freshness.
4. Check each metric's kind (observation, model analysis, or forecast) and provenance before comparing it.
5. Leave demo mode off for operational production. Demo data exists for onboarding/testing and must not be used to fill a missing live feed.

No-key feeds are subject to their own quotas and terms. Public map, routing and Overpass services are not a substitute for a contracted, self-hosted, or managed production service at high volume.

## Route-specific data gaps

SakhiRoute has no verified street-light operations feed. NDMC publicly describes individually controlled street lighting within its command-centre operations, but its public description does not provide a downloadable or documented consumer API; access would need to be arranged with the responsible agency. See the [NDMC command-centre description](https://smartcity.ndmc.gov.in/content/projects/project-details/integrated-command-and-control-center) and [NDMC street-light maintenance/GIS description](https://ndmc.gov.in/departments/it_achievements.aspx). OSM lamp points and tags remain community-mapped infrastructure evidence, not current condition or illumination observations.

Delhi Open Transit Data is not connected. The official bus static GTFS portal reports stops, routes, trips and stop-times last updated June 20, 2024, and warns that stop times are rough estimates. Static downloads request purpose/user details and terms acceptance; DoT reserves permission rights. Real-time feeds require an authorized private key. Do not present this 2024 timetable as current service. See the [static-data page](https://otd.delhi.gov.in/data/static/), [GTFS documentation](https://otd.delhi.gov.in/documentation/), and [terms](https://otd.delhi.gov.in/terms).

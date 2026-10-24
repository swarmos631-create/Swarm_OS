# Architecture

SwarmOS is a **modular monolith**: one Python API process (FastAPI) with an embedded or standalone background
worker, one database, and a static React PWA served by the API from the same origin. There are no microservices.
The modules have clear boundaries, so any of them can be extracted later if load requires it.

```
frontend/ (React + TS + Vite PWA)  ──HTTP /api/v1──▶  backend/swarmos
                                                     ├── api/          routers (auth, cities, incidents, agents, platform)
                                                     ├── core/         security, errors, logging, rate limit, audit
                                                     ├── cities/       city registry (data/cities.json → DB)
                                                     ├── integrations/ adapters, HTTP client, sync + freshness
                                                     ├── services/     CityData read facade, task + approval workflows
                                                     ├── agents/       contracts, 50-agent registry, skills, planner, orchestrator
                                                     ├── ai/           ModelRuntime (sklearn / Ollama / llama.cpp / rules), retrieval
                                                     ├── ml/           features, synthetic data, datasets, training pipeline
                                                     ├── jobs/         DB-backed queue + worker + scheduler
                                                     └── demo/         bootstrap: catalog, registry, dev accounts, fixtures
ml/ (synthetic datasets, local copies of artifacts and reports; training configs live in backend/swarmos/ml/configs)
```

On Vercel the same code runs as two services: the PWA as static files and the API as a Python function behind
`/api/*` (see [DEPLOYMENT.md](DEPLOYMENT.md#vercel)).

## Request path

1. Middleware assigns a correlation ID (`X-Request-ID`), adds security headers, and sets `Cache-Control: no-store` on every API response.
2. `current_principal` loads the server-side session from the `swarmos_session` cookie (the DB stores only a SHA-256 of the token) and checks the CSRF double-submit header on mutating requests.
3. `city_access` validates the `cityId` path parameter against the catalog **and** the caller's city scope. Every city-scoped route depends on it, and records from other cities return 404, never 403, so they cannot be enumerated.
4. `require_role` enforces viewer < operator < admin on the server.
5. Errors use one format: `{"error": {"code", "message", "details", "correlation_id"}}`.

## Data model (selected)

| Table | Purpose |
|---|---|
| `cities` | Catalog (87 Indian cities). `bbox` is an **approximate viewport**, not an administrative boundary. |
| `data_sources`, `sync_runs` | Source registry, connection status, every sync attempt with duration, record count and classified error |
| `measurements` | Time series. `kind` ∈ observation / model_analysis / forecast; `provenance` ∈ live / demo / …; unique on (city, source, metric, entity, kind, time) |
| `geo_features` | Point features (OSM facilities, USGS events, demo assets). Global features have `city_id = NULL` and are queried by distance. |
| `incidents`, `incident_events` | Incident workflow with an allowed-transition matrix and an optimistic-concurrency `version` |
| `service_requests` | Citizen complaints with classifier label, confidence and `classified_by` |
| `agent_tasks`, `agent_executions`, `agent_handoffs` | Orchestration state, per-agent I/O, errors, durations, and hashed handoff payloads |
| `proposed_actions` | Recommendations awaiting human decision, with evidence and compliance review |
| `jobs` | Durable queue with leases, retries, dedupe keys and correlation IDs |
| `model_versions` | Model registry: metrics, baseline, dataset manifest, artifact checksum, gate result, status |
| `documents`, `document_chunks` | Knowledge base with classification (public / internal / restricted) and provenance |
| `audit_log`, `notifications`, `users`, `user_cities`, `auth_sessions` | Governance and identity |

Timestamps are stored and returned as timezone-aware UTC (`UTCDateTime` type) and displayed in IST.

### SQLite (development) vs PostgreSQL (production)

The schema is written with portable SQLAlchemy types and managed by Alembic (`backend/migrations`). Set
`SWARMOS_DATABASE_URL=postgresql+psycopg://…` to use PostgreSQL (the psycopg driver is a core dependency; plain
`postgres://` URLs, and the `DATABASE_URL` / `POSTGRES_URL` variables injected by hosting platforms, are accepted and
normalised). Differences:

* SQLite runs in WAL mode with a busy timeout. One API process with an embedded worker is the supported dev topology.
* Upserts use dialect-specific `INSERT … ON CONFLICT` (both supported).
* Geospatial queries use indexed lat/lon bounding boxes plus haversine in Python. On PostgreSQL, add a PostGIS
  `geography(Point)` column with a GiST index in a new migration, and switch `CityData.earthquakes_near` /
  `features` to `ST_DWithin`; the domain model does not change.

## Background work

`jobs/queue.py` claims jobs with a compare-and-set `UPDATE … WHERE status='queued'`, which is safe for several workers
on PostgreSQL. Leases expire after 5 minutes, and a heartbeat extends them for long jobs; expired leases are re-queued.
Job kinds: `agent_task`, `sync`, `sync_city`, `train_model`. A scheduler tick in the worker enqueues periodic syncs
with time-bucketed dedupe keys, so restarts never double-schedule. User requests never wait for long work; they
return `202` with a job or task ID.

`SWARMOS_JOB_MODE` selects who runs the queue (`jobs/dispatch.py`):

* `worker` (default off Vercel): the embedded worker thread or `swarmos worker` claims jobs as above.
* `inline` (default on Vercel): after enqueueing, the request schedules the job with FastAPI `BackgroundTasks`, so
  it runs in the same invocation after the `202` is sent (`worker.run_job_now`, which still claims the job through
  the queue, so leases and retries apply). `GET /api/internal/cron`, protected by `CRON_SECRET`, runs the scheduler
  tick and drains leftover or expired-lease jobs within a time budget.

Trained model artifacts are stored in the database (`model_versions.artifact_blob`), not only on disk, so no
shared filesystem is needed between processes or instances.

## Orchestration

See [AGENTS.md](AGENTS.md). In short: deterministic planner → dependency DAG → stage execution under a semaphore
(`SWARMOS_AGENT_MAX_CONCURRENCY`) → each agent in its own thread and DB session, with a timeout, bounded retries for
transient errors, and cooperative cancellation → results validated against `AgentResult` → handoffs persisted as
SHA-256 digests → a consolidated result whose status is `succeeded` only if **every** agent succeeded.

## Frontend

React 19 + TypeScript + Vite. TanStack Query for server state (polling while tasks run), React Router with the city
in the URL, hand-built SVG charts (no chart library), MapLibre GL loaded lazily on the map route only. Design tokens
are in `src/styles/global.css`. The PWA is built with `vite-plugin-pwa` (Workbox `generateSW`):

| Resource | Strategy |
|---|---|
| App shell, JS/CSS, fonts, icons | Precache (versioned, old caches cleaned up) |
| `/api/**` | **NetworkOnly**: operator data is never written to Cache Storage |
| Map style/sprites/glyphs | StaleWhileRevalidate, 7 days |
| Map tiles | CacheFirst, 600 entries, 7 days |

When offline, the shell loads, a banner states that the data shown is the last copy fetched in this session, and all
mutating controls are disabled. No offline action queue exists by design: queued civic or emergency actions without
designed idempotency and authorization semantics would be unsafe.

## API

Interactive OpenAPI docs are at `/api/docs` (disabled in production). Main resources, all under `/api/v1`:
`auth/*`, `cities`, `cities/{id}/dashboard|series|map/layers|map/features|incidents|service-requests|tasks|tasks/plan|actions|integrations|documents|analytics`,
`agents`, `integrations`, `models/*`, `notifications`, `system/status|audit|users`. Health: `/api/health/live`, `/api/health/ready`.
List endpoints share `limit`/`offset` pagination and return `{items, total, limit, offset}`.

# SwarmOS

**An intelligent operating system for Indian cities.** It combines a city operations center, live and clearly labelled
data, an India-wide city catalog, a geospatial operations map, 50 coordinated AI agents, and a fully local
ML pipeline, all delivered as an installable progressive web app (PWA).

> **Honesty first.** Every value in SwarmOS carries a provenance tag: `live`, `operator`, `derived` or
> `demo`/`synthetic`. Demo data is generated only for pilot cities and is always labelled. Integrations without
> credentials show *awaiting credentials*, never "live". Agents that lack data return a typed *unavailable* result
> instead of a guess. No action executes without human approval.

| Area | What you get |
|---|---|
| Command Center | City KPIs with provenance and timestamps, incidents, source health, trends, agent activity, alerts and audit |
| Cities | 87 Indian cities (searchable, URL-scoped `/c/:cityId/...`); the server validates the city id and the caller's city scope on every request |
| Operations Map | MapLibre GL + OpenFreeMap (OSM) tiles, clustered layers, feature details, search, 3D OSM buildings mode, and a fallback when WebGL is missing |
| Agents | 50 registered agents in 10 domains, one shared runtime, a DAG orchestrator (concurrency, timeouts, retries, cancellation, idempotency, persisted handoffs) |
| AI Workspace | Plan preview, live execution graph, consolidated result with confidence, freshness, limitations, and an approval workflow for proposed actions |
| Local AI | scikit-learn models trained in-repo; optional Ollama / llama.cpp on loopback; deterministic fallbacks; **no hosted LLM APIs** |
| Model Lab | Reproducible training on synthetic datasets, evaluation gates vs baselines, registry, promote and rollback |
| Integrations | Open-Meteo weather and air quality, USGS earthquakes, OpenStreetMap/Overpass (live, no key); CPCB via data.gov.in (key needed); IMD (access by arrangement) |
| Security | Argon2id, server-side sessions, CSRF, RBAC (viewer/operator/admin) plus city scope, rate limits, audit log, CSP and security headers |
| PWA | Manifest, icons, Workbox service worker, offline app shell, stale-data warnings, update prompt; API responses are never cached |

## Quick start (macOS / Linux)

Requirements: Python ≥ 3.11 and Node ≥ 20. Docker, PostgreSQL, a GPU and an LLM are **not** required.

```bash
make install        # Python venv + backend deps, npm deps
make init           # migrate DB, seed 87 cities, sources, dev accounts, sample docs, demo fixtures
make train          # train + evaluate the 5 local models on synthetic data (~20 s, CPU)
make build          # build the PWA into frontend/dist
make run            # API + embedded worker + PWA on http://127.0.0.1:8000
```

Sign in with a development account. `make init` writes the passwords to `var/dev-credentials.txt` (mode 600,
git-ignored). They are never printed to the console or the logs:

| Account | Role | City scope |
|---|---|---|
| `admin@swarmos.local` | admin | all cities |
| `operator@swarmos.local` | operator | all cities |
| `viewer@swarmos.local` | viewer | pilot cities only |

Pull live data for a city immediately (otherwise the worker syncs `SWARMOS_SYNC_CITIES` every 30 minutes):

```bash
backend/.venv/bin/swarmos sync bengaluru
```

### Development mode (hot reload)

```bash
make run-api        # terminal 1 — API on :8000
make dev-web        # terminal 2 — Vite on :5173, proxies /api to :8000
```

## Commands

| Command | Purpose |
|---|---|
| `make test` | Backend unit and integration tests (pytest) |
| `make lint` | ruff, mypy, ESLint, `tsc` |
| `make e2e` | Playwright end-to-end tests (isolated DB, no network sync; first run: `npx playwright install --only-shell chromium`) |
| `swarmos worker` | Standalone background worker (set `SWARMOS_WORKER_EMBEDDED=false` for the API) |
| `swarmos ml list / train NAME / train-all / promote NAME VERSION / rollback NAME` | Model lifecycle |
| `swarmos create-user EMAIL ROLE [--all-cities]` | Provision an account (password read from a prompt) |

## Documentation

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, data model, orchestration, API, SQLite vs PostgreSQL
* [docs/AGENTS.md](docs/AGENTS.md): the 50 agents, contracts, and how to add one
* [docs/ML.md](docs/ML.md): datasets, training, evaluation, registry, local LLM setup
* [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md): data sources, credentials, adding an adapter
* [docs/SECURITY.md](docs/SECURITY.md): threat model, controls, production hardening
* [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): production deployment, PostgreSQL/PostGIS, HTTPS, troubleshooting
* [docs/PLAN.md](docs/PLAN.md): implementation plan and checklist

## Licences and attribution

Map data © OpenStreetMap contributors (ODbL), tiles by OpenFreeMap. Weather and air quality by
Open-Meteo.com (CC BY 4.0; CAMS). Earthquake data from USGS (public domain). The demo fixtures, the sample documents
and the training datasets are synthetic and generated by SwarmOS.

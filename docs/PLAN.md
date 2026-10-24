# SwarmOS — Implementation Plan

## 1. Repository assessment (2026-10-24)

| Item | Finding |
|---|---|
| Local checkout | Empty. Branch `main`, no commits. |
| Remote | `origin` → `github.com/swarmos631-create/Swarm_OS` — public, **empty** (no refs). |
| Existing code to preserve | None. |
| Toolchain on dev machine | Node 26, npm 11, Python 3.13, Homebrew. **No** Docker, PostgreSQL, Ollama, or GPU. 16 GB RAM, Apple Silicon. |
| Reachable public data sources (verified with HTTP checks) | Open-Meteo forecast + air-quality (no key), USGS earthquake GeoJSON feed (no key), OpenFreeMap vector tiles (no key). Overpass API reachable intermittently (timeouts observed). data.gov.in requires an API key. |

Because there is nothing to preserve, a coherent architecture is established from scratch, chosen for
operational simplicity on this machine and a clean path to production.

## 2. Architecture decisions

**Modular monolith, two deployables:** a Python API (with an embedded or separate background worker) and a
static React PWA.

| Concern | Choice | Why |
|---|---|---|
| Backend | Python 3.13, FastAPI, Pydantic v2 | Typed contracts and OpenAPI for free; the same language as the ML pipeline, so agents can call trained models in-process. |
| Persistence | SQLAlchemy 2 + Alembic. SQLite (WAL) in development, PostgreSQL (+PostGIS) in production via `DATABASE_URL` | No database server is installed locally. SQLAlchemy keeps the schema portable. Differences are documented in `docs/ARCHITECTURE.md`. |
| Background jobs | Database-backed job queue with leases (`jobs` table) and a worker loop that runs embedded in the API process or standalone (`python -m swarmos.worker`) | Durable, no Redis needed, works on both SQLite and Postgres. |
| Auth | Local accounts, Argon2id hashes, server-side sessions in an HttpOnly cookie, CSRF double-submit token, RBAC (viewer/operator/admin) plus per-city scope | Works without an external identity provider. An OIDC path is documented. |
| AI runtime | Provider-independent `ModelRuntime`: Ollama and llama.cpp adapters (loopback-only by default), scikit-learn artifact adapter, deterministic rules fallback. **No hosted LLM adapters exist.** | Required by the brief. Most agents need classical ML or rules rather than LLMs. |
| Agents | 50 declarative `AgentSpec`s over a shared runtime and a library of domain "skills"; DAG orchestrator with bounded concurrency, timeouts, retries, cancellation, idempotency, persisted traces | Avoids 50 duplicated classes. |
| Frontend | React 18 + TypeScript + Vite, TanStack Query, React Router, MapLibre GL (lazy-loaded), hand-built SVG charts, vite-plugin-pwa (Workbox) | A small dependency surface; lazy loading keeps the shell light. |
| Map tiles | OpenFreeMap (OSM data, no key), configurable via `VITE_MAP_STYLE_URL` | Free, no rate-limit keys, includes OSM building heights for the 3D view. |
| Tests | pytest (unit + integration with temp DB and mocked HTTP), Vitest, Playwright | |

## 3. Data honesty model

Every data-bearing record and API payload carries a `provenance` of `live`, `cached`, `demo`, `operator`,
`derived` or `unavailable`, plus `observed_at` / `fetched_at`. The UI renders a provenance badge everywhere
values appear. Demo data is seeded only for three pilot cities (Bengaluru, Mumbai, Delhi), only when
`SWARMOS_DEMO_MODE=true`, and is always tagged `demo`.

## 4. Task checklist

- [x] Phase 1: Repository assessment and plan
- [x] Phase 2: Backend foundation: config, DB, migrations, auth, RBAC, audit, errors, logging, health
- [x] Phase 3: City registry (India catalog) and city-scoped APIs; 2D map; 3D buildings mode
- [x] Phase 4: Integration framework: Open-Meteo weather, Open-Meteo air quality, USGS earthquakes, OSM Overpass POIs, data.gov.in CPCB (key required), IMD (access required), demo fixtures; sync jobs; source health
- [x] Phase 5: 50-agent registry, skills, planner, DAG orchestrator, persistence, approvals
- [x] Phase 6: Model runtime, training pipeline (complaint classifier, demand forecaster, anomaly detector), registry, promotion/rollback, local retrieval
- [x] Phase 7: Incidents workflow, dashboard, analytics
- [x] Phase 8: PWA manifest/icons/service worker/offline, responsive, accessibility
- [x] Phase 9: Lint, type-check, unit/integration/e2e tests, builds
- [x] Phase 10: Runtime verification and final report

## Status (2026-10-24)

All phases implemented and verified locally. Remaining work and known gaps are listed in the final report and in
docs/SECURITY.md ("Known gaps"): native OIDC sign-in, MFA, a PostGIS migration, real (non-synthetic) training data,
CPCB key configuration, and a Dockerfile verified on a machine with Docker.

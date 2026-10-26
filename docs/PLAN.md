# SwarmOS implementation plan

This plan reflects the repository state inspected on 2026-10-25. The full feature audit and the distinction between implemented and remaining work are in IMPLEMENTATION_ROADMAP.md.

## Existing foundation

- React 19, TypeScript, Vite, TanStack Query, city-scoped routing and PWA.
- FastAPI, SQLAlchemy, Alembic and SQLite/PostgreSQL.
- Durable database-backed jobs, 50 local rule/ML agents, orchestration graph, action approvals and model registry.
- City-level authentication/authorization, audit events, CSRF, rate limits, provenance, data-source sync and freshness.
- Public no-key weather, air-quality model, earthquake, OSM/Overpass, OpenFreeMap and disaster-alert integrations; optional CPCB/TomTom/Mappls credentials.

## Delivered in the current local feature branch

- Mission Control for persisted task states, DAG, agent outputs, handoffs and recommendations.
- Evidence links attached to incidents, including role/city enforcement, provenance, freshness, limitations and audit.
- Saved rainfall/temperature scenario runs backed by existing agent jobs and parameters.
- Deterministic same-task disagreement review using exact shared metrics and matching finding titles; no confidence voting.
- Anonymous text-only citizen intake, opaque reference status lookup, local classification, and operator status triage.
- SafeRoute using best-effort FOSSGIS/OSRM route alternatives and local mapped context, with no safety rating or SwarmOS route-history storage.
- Persisted situation reports with a bounded city snapshot, agent review, scenario comparisons, recommendations, outcome summaries, city-scoped history and Markdown/print export.
- Audited operator outcome records for executed/manual actions, with optional links to non-demo observed measurements and explicit causal limitations.
- City Analytics now includes historical forecast evaluation and exact-match cross-task signal review with source provenance and caveats.
- MapLibre/OpenFreeMap fallback provides a keyless, attributed base map with route overlays and click-to-set endpoints when the optional Mappls SDK is unavailable; manual coordinate entry remains available.

## Remaining work

- Richer city-wide recommendation review with linked evidence details.
- Address search, lighting/transport layers, accessibility checks, and validation of a routing provider suitable for production scale.
- Full regression and UI workflows against a staging deployment; local changes have not been deployed.

## Data and model policy

Use public data with documented terms and required attribution. Keep API credentials server-side and out of version control. Do not invent keys. Keep locally owned SwarmOS models and rules as the only agent/model runtime; do not add hosted LLM providers or their credentials. Label modelled/forecast, derived, operator, cached, demo and unavailable data distinctly.

## Validation

Run Alembic migrations on a disposable SQLite test database and production-like PostgreSQL, backend unit/integration tests, frontend type-check/lint/tests/build, then manual browser checks for operator and anonymous flows. Do not deploy without explicit user authorization.

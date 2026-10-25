# Deployment and operations

## Railway deployment

The operator has identified Railway as the project's sole deployment target. This guide describes the repository's
Railway topology; it does not attest that any particular Railway service, variable, database, domain, or deployment
revision is currently healthy.

The repository-root `Dockerfile` builds the React PWA and FastAPI backend with local ML extras, serves the frontend
from the API origin, and runs the durable job worker in the API process. `railway.json` configures the Docker build,
readiness check, and restart policy. Railway injects `PORT`. Production state requires PostgreSQL; SQLite on an
ephemeral container filesystem is not a durable production database.

1. Connect this GitHub repository to the Railway project. Keep the service root at the repository root so Railway
   finds `railway.json` and `Dockerfile`.
2. Add a Railway PostgreSQL service. Set the app service's `SWARMOS_DATABASE_URL` to a Railway reference to that
   database's `DATABASE_URL` (for example `${{Postgres.DATABASE_URL}}`, using the actual service name).
3. Configure production variables in Railway before exposing the public domain:
   * `SWARMOS_ENV=production`
   * `SWARMOS_AUTH_DISABLED=false`
   * `SWARMOS_DEMO_MODE=false` after real feeds are configured; otherwise retain clearly labelled demo mode.
   * `SWARMOS_JOB_MODE=worker` and `SWARMOS_WORKER_EMBEDDED=true`
   * `SWARMOS_TRUSTED_HOSTS=<generated Railway hostname>`
   * `SWARMOS_CRON_SECRET=<generated random secret>` for bootstrap and the internal scheduler endpoint.
   * Optional data credentials such as `SWARMOS_OGD_API_KEY`, stored as Railway secrets only. SwarmOS's project-owned
     agent runtime does not need hosted LLM provider credentials.
4. The container runs `swarmos migrate` before serving. Wait for `/api/health/ready`, then initialize the first
   admin through `POST /api/internal/bootstrap` with `Authorization: Bearer $SWARMOS_CRON_SECRET` and the admin
   credentials in the JSON body. Never put the password in Git or public deployment logs.
5. Verify sign-in, data integrations, scenario jobs, incident evidence, citizen intake, reports, and SakhiRoute on
   the Railway domain. Keep PostgreSQL private to the project.

The embedded worker suits a single app instance. With multiple API replicas, set
`SWARMOS_WORKER_EMBEDDED=false` and run one or more dedicated `swarmos worker` services against the same
PostgreSQL database. PostgreSQL-backed public endpoint limits are shared across replicas; the FOSSGIS pacing guard
is best-effort per process. Public APIs may have quotas or require separately registered provider credentials.
Confirm Railway service variables, the bootstrap admin, public domain, and readiness check before treating a
deployment as operational.

## Local production checks

From the repository root:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
python3 -m venv backend/.venv
backend/.venv/bin/pip install -e "backend[ml]"
SWARMOS_DATABASE_URL=sqlite:////tmp/swarmos-migration-check.db backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
```

The SQLite command checks the migration chain on a disposable local database; it does not verify Railway's
PostgreSQL schema or production data. PostgreSQL migration validation requires an explicitly provided disposable
PostgreSQL database. Back up production data and use a reviewed migration plan before any production schema change.

The repository includes the root Dockerfile for Railway's Docker builder. Container builds should be run in CI or a
local Docker/Podman environment; a frontend build and backend test suite alone do not prove the image starts.

## Health and troubleshooting

* Liveness: `GET /api/health/live`.
* Readiness: `GET /api/health/ready` (database, migrations, and agent registry).
* Dashboard cards say *Not available*: sync data for the city and inspect Data Integrations for errors.
* OSM sync times out: public Overpass instances may be busy. Retry later or configure an approved self-hosted
  `SWARMOS_OVERPASS_URLS`.
* Base map tiles fail: check network reachability or configure `VITE_MAP_STYLE_URL` and rebuild. The public
  OpenFreeMap service has no SLA.
* Forecast agents use `baseline:seasonal_naive`: no trained model is promoted. Train and review a model using
  the local Model Lab workflow.
* SQLite reports `database is locked`: use one process for local SQLite or use PostgreSQL for multi-process work.
* Startup rejects a non-loopback model URL: local model endpoint restrictions are active. Prefer loopback; only set
  `SWARMOS_ALLOW_REMOTE_MODEL_HOSTS=true` for a server you administer and trust.
* Lost development passwords: recreate the development database or use `swarmos create-user`; never reset or
  delete production data with development procedures.

## Migrations

Alembic manages schema changes in `backend/migrations`; `swarmos migrate` runs `alembic upgrade head`. Generate
a candidate revision from `backend` with `.venv/bin/alembic revision --autogenerate -m "…"`, then review it
before applying. Migrations should preserve user data. Destructive changes require a separately reviewed,
recoverable migration and an explicit production change plan.

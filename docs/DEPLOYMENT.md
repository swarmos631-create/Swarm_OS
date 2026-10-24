# Deployment and operations

## Topologies

* **Single node (default):** `swarmos serve` runs the API, the embedded worker and the PWA (from `frontend/dist`) on one port. Put HTTPS in front of it.
* **Scaled:** N API replicas with `SWARMOS_WORKER_EMBEDDED=false`, plus one or more `swarmos worker` processes, all on PostgreSQL. The job queue's compare-and-set claim and leases make several workers safe.
* **Vercel (serverless):** the PWA as a static `frontend` service and the API as a Python function `backend` service, on a managed PostgreSQL. No long-running worker: jobs run inline after the request, and a cron drains the queue. See [Vercel](#vercel).

## Production steps

```bash
python3 -m venv /opt/swarmos/venv && /opt/swarmos/venv/bin/pip install -e backend
npm --prefix frontend ci && npm --prefix frontend run build
export SWARMOS_ENV=production SWARMOS_DATABASE_URL=postgresql+psycopg://swarmos@db/swarmos SWARMOS_TRUSTED_HOSTS=ops.example.gov.in
/opt/swarmos/venv/bin/swarmos migrate
/opt/swarmos/venv/bin/swarmos seed --no-demo
/opt/swarmos/venv/bin/swarmos create-user admin@example.gov.in admin --all-cities
/opt/swarmos/venv/bin/swarmos ml train-all --promote-if-better     # or ship approved artifacts
/opt/swarmos/venv/bin/swarmos serve --host 127.0.0.1 --port 8000   # behind nginx/Caddy with TLS
```

Example systemd units: `swarmos-api.service` (`swarmos serve`) and `swarmos-worker.service` (`swarmos worker`). Both
handle SIGTERM gracefully: the worker stops claiming jobs and drains in-flight jobs for up to 30 s; expired leases
are re-queued automatically.

Health checks: liveness `GET /api/health/live`; readiness `GET /api/health/ready` (database, migrations, agent registry).

Docker is not required. A container image only needs Python 3.11+, the backend package and `frontend/dist`. This
repository does not ship a Dockerfile, because the target machine had no Docker to verify one.

## Vercel

`vercel.json` defines two [Vercel Services](https://vercel.com/docs/services) in one project:

| Service | Root | What it is |
|---|---|---|
| `frontend` | `frontend/` | Vite build of the PWA (`dist/`), SPA fallback to `index.html`, security headers, immutable `/assets` cache |
| `backend` | `backend/` | FastAPI app `swarmos.main:app` as a Python function (Python from `backend/.python-version`, max duration 300 s) |

Routing is same-origin: `/api/*` goes to `backend`, everything else to `frontend`. The browser only ever calls
`/api/v1/...` on its own origin, so no CORS settings or service bindings are needed. Nothing is internal-only;
`/api/internal/cron` is public but rejects any request without `Authorization: Bearer $CRON_SECRET`.

Serverless mode switches on automatically when `VERCEL=1` (set by the platform):

* **Database:** SQLite is refused at startup, because function disks are ephemeral and not shared. Add a Postgres
  database from the Vercel Marketplace (for example Neon); the injected `DATABASE_URL` or `POSTGRES_URL` is picked
  up and rewritten to the `postgresql+psycopg://` driver. `SWARMOS_DATABASE_URL` takes precedence if set.
* **Jobs:** `SWARMOS_JOB_MODE=inline`. A request that enqueues a job (agent task, sync, training) returns `202`
  immediately, then runs the job in the same function invocation after the response is sent. The cron at
  `/api/internal/cron` schedules periodic syncs and drains any queued or expired-lease jobs within
  `SWARMOS_CRON_BUDGET_SECONDS` (default 240 s, kept under the function's max duration).
* **ML:** training configs ship inside the package; datasets and scratch files go to `/tmp/swarmos-ml`. Model
  artifacts and reports are stored in the database (`model_versions.artifact_blob`, `report_md`) and loaded with
  checksum verification, so every instance sees the promoted model.

### Steps

1. Import the repository in Vercel. Vercel reads the services from `vercel.json`; leave the project root as the
   repository root.
2. Add a Postgres integration (Storage → Marketplace, for example Neon) and connect it to the project.
3. Set environment variables (Production, and Preview if you use previews):
   * `SWARMOS_ENV=production`
   * `CRON_SECRET`: a long random value (`openssl rand -hex 32`). Vercel sends it on cron calls.
   * `SWARMOS_DEMO_MODE=false` once real feeds are configured (or keep `true` for a labelled demo).
   * Optional: `SWARMOS_OGD_API_KEY`, `SWARMOS_SYNC_CITIES`, and `SWARMOS_TRUSTED_HOSTS=<your-domain>` (Production
     only; preview URLs would be rejected).
4. Initialise the database **from your machine**, against the production URL (migrations do not run in the build):

   ```bash
   export SWARMOS_ENV=production SWARMOS_DATABASE_URL='postgresql+psycopg://…'   # from the Neon dashboard
   backend/.venv/bin/swarmos migrate
   backend/.venv/bin/swarmos seed --no-demo
   backend/.venv/bin/swarmos create-user admin@example.gov.in admin --all-cities
   backend/.venv/bin/swarmos ml train-all --promote-if-better   # optional; artifacts are stored in the DB
   ```

   Repeat `swarmos migrate` before deploying any commit that adds a migration.
5. Deploy. Check `GET /api/health/ready` on the deployment URL.

### Limits to check for your plan

* **Cron frequency:** `vercel.json` schedules the cron every 30 minutes. The Hobby plan allows only daily crons,
  so change it to, for example, `"0 6 * * *"` there; data then refreshes on *Sync now* or once a day.
* **Function duration:** 300 s needs Fluid compute (the default for new projects). Lower `maxDuration` and
  `SWARMOS_CRON_BUDGET_SECONDS` together if your plan caps it lower.
* **Bundle size:** scikit-learn, SciPy and NumPy make the Python bundle large. Vercel's Python limit is 500 MB
  uncompressed; `.vercelignore` keeps tests, local venvs, ML artifacts and node_modules out of CLI uploads.
* **Local AI:** Ollama / llama.cpp must run on loopback, which a Vercel function does not have. On Vercel, agents
  use the trained scikit-learn models and the deterministic fallbacks.

## Migrations

Alembic manages the schema (`backend/migrations`). `swarmos migrate` = `alembic upgrade head`. New migrations:
`cd backend && .venv/bin/alembic revision --autogenerate -m "…"`, then review the file before applying it.
Migrations never delete user data. Destructive changes need an explicit, reviewed migration.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard cards say *Not available* | That city has no synchronised data yet. Run `swarmos sync <city>` or *Sync now*; check Data Integrations for errors. |
| OSM sync fails with `timeout` | Public Overpass instances are busy. Retry later or set `SWARMOS_OVERPASS_URLS` to a self-hosted instance. |
| Map shows "Base map tiles could not be loaded" | Offline, or tiles.openfreemap.org is unreachable. Set `VITE_MAP_STYLE_URL` to another style and rebuild. |
| "3D graphics unavailable" | The browser or GPU lacks WebGL. All other pages work. |
| Forecast agents say `baseline:seasonal_naive` | No promoted model. Run `swarmos ml train-all --promote-if-better`. |
| Vercel function fails with "SQLite cannot be used on Vercel" | No Postgres is connected. Add one from the Marketplace or set `SWARMOS_DATABASE_URL`. |
| Vercel: readiness says migrations are pending | Run `swarmos migrate` against the production database (step 4 above). |
| `database is locked` on SQLite | Use one process with the embedded worker, or move to PostgreSQL. |
| Startup error about a non-loopback model URL | The local-only policy is working. Use `127.0.0.1`, or set `SWARMOS_ALLOW_REMOTE_MODEL_HOSTS=true` for your own server. |
| Lost dev passwords | `rm var/swarmos.db && swarmos init` (dev only) or `swarmos create-user`. |

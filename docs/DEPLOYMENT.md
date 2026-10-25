# Deployment and operations

## Railway: complete single-service deployment

The repository-root `Dockerfile` builds the React PWA, installs the FastAPI backend with its local ML extras, serves
the frontend from that API origin, and runs the persistent job worker in the API process. `railway.json` configures
the Docker build, readiness check, and restart policy. Railway injects `PORT`; the container listens on it. The app
requires PostgreSQL for durable production state; do not use SQLite on an ephemeral container filesystem.

1. In the Railway project, connect the Swarm_OS GitHub repository or deploy this repository from the Railway CLI.
   Keep the repository root as the service root so Railway can find `railway.json` and `Dockerfile`.
2. Add a Railway PostgreSQL service. On the app service, set `SWARMOS_DATABASE_URL` to a Railway reference to that
   database's `DATABASE_URL` (for example `${{Postgres.DATABASE_URL}}`; use the actual Railway service name).
3. Configure these service variables before exposing the public domain:
   * `SWARMOS_ENV=production`
   * `SWARMOS_AUTH_DISABLED=false`
   * `SWARMOS_DEMO_MODE=false`
   * `SWARMOS_JOB_MODE=worker` and `SWARMOS_WORKER_EMBEDDED=true`
   * `SWARMOS_TRUSTED_HOSTS=<the generated Railway hostname>`
   * `SWARMOS_CRON_SECRET=<a generated random secret>` for one-time bootstrap and the internal scheduler endpoint
   * Optional free data credentials such as `SWARMOS_OGD_API_KEY`, stored as Railway variables only. No hosted LLM
     provider keys are needed or supported for the project-owned agent runtime.
4. Deploy and wait for `/api/health/ready` to pass. The container runs `swarmos migrate` before serving. Set up the
   first admin by POSTing `/api/internal/bootstrap` with `Authorization: Bearer $SWARMOS_CRON_SECRET` and a
   strong admin password in the JSON body. Do not place that password in Git or a public deployment log. The
   bootstrap endpoint migrates and seeds the database without demo data and creates the admin idempotently.
5. Sign in through the generated Railway domain and verify dashboard, data integrations, scenario jobs, incident
   evidence, citizen intake, reports, and SakhiRoute. Keep PostgreSQL private to the project; only the app needs
   database access.

The embedded worker suits a first single-instance Railway deployment. For multiple API replicas, turn off the
embedded worker and add a dedicated `swarmos worker` service. PostgreSQL-backed public endpoint limits are shared;
the provider-specific FOSSGIS pacing guard remains best-effort per process. Railway billing and resource limits depend on the selected plan. A free API provider may still require the
operator to register and enter its own key. No deployment can be considered complete until the database variables,
bootstrap admin, public domain, and health check have been verified in Railway.

## Topologies

* **Single node (default):** `swarmos serve` runs the API, the embedded worker and the PWA (from `frontend/dist`) on one port. Put HTTPS in front of it.
* **Scaled:** N API replicas with `SWARMOS_WORKER_EMBEDDED=false`, plus one or more `swarmos worker` processes, all on PostgreSQL. The job queue's compare-and-set claim and leases make several workers safe.
* **Render API + Vercel frontend (current):** the API as a Render web service, the PWA on Vercel proxying `/api/*`
  to it. See [Render API + Vercel frontend](#render-api--vercel-frontend).
* **Vercel (serverless):** the PWA as a static `frontend` service and the API as a Python function `backend` service, on a managed PostgreSQL. No long-running worker: jobs run inline after the request, and a cron drains the queue. See [Vercel](#vercel).

## Production steps

```bash
python3 -m venv /opt/swarmos/venv && /opt/swarmos/venv/bin/pip install -e "backend[ml]"
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

Docker is not required for direct deployments. The repository includes a root `Dockerfile` for Railway's Docker
builder, which builds the PWA and installs the FastAPI backend. It has not been container-built in this environment
because Docker and Podman are unavailable; the frontend production bundle and backend tests are verified separately.

## Render API + Vercel frontend

This is how https://swarmos-ebon.vercel.app runs.

| Part | Where | Config |
|---|---|---|
| API (FastAPI + embedded worker, full `ml` extra) | Render web service `swarmos-api`, Singapore | `render.yaml` |
| PWA | Vercel project `swarm_os` (static build of `frontend/`) | `vercel.json` |
| Database | Neon Postgres, AWS ap-southeast-1 (Singapore) | `SWARMOS_DATABASE_URL` on Render |

`vercel.json` rewrites `/api/*` to `https://swarmos-api.onrender.com/api/*`, so the browser only talks to its own
origin: no CORS settings, and the session/CSRF cookies and the Content-Security-Policy work as before. If the
Render service gets another URL, change that rewrite.

Steps:

1. Render → **New → Blueprint**, pick this repository. Render reads `render.yaml` and asks for
   `SWARMOS_DATABASE_URL`: paste the Neon connection string (Neon console → Connect; the `postgresql://…` URL with
   `sslmode=require`).
2. Render builds with `pip install -e ".[ml]"` and starts with `swarmos migrate && swarmos serve`, so migrations
   run on every deploy. Seed once if the database is new: run the bootstrap endpoint (below) or
   `swarmos seed --no-demo` against the database.
3. Vercel deploys the frontend on every push; nothing to configure there besides `vercel.json`.
4. Check `https://swarmos-ebon.vercel.app/api/health/ready` (proxied to Render).

Notes:

* **Sign-in:** `SWARMOS_AUTH_DISABLED=true` (set in `render.yaml`) removes the sign-in screen: every visitor acts
  as one built-in admin account (`guest@swarmos.local`), so anyone with the URL can do everything. Set it to
  `false` on Render to require accounts again; existing accounts keep working either way.
* **Free plan:** the instance sleeps after 15 idle minutes and the first request then takes about a minute.
  Switch the service to the Starter plan in Render to keep it awake (and the embedded worker running).
* **Jobs:** the embedded worker runs syncs, agent tasks and training in the same process. No Vercel cron is used.
* **Bootstrap endpoint:** `POST /api/internal/bootstrap` (below) works on Render too if you set `CRON_SECRET` there.
* **Schema updates:** this branch adds Alembic revisions `0003` (incident evidence links), `0004` (saved scenario runs), `0005` (situation reports), `0006` (intervention outcomes), and `0007` (shared rate-limit counters). Render and the Railway Docker start command run `swarmos migrate` before serving. For manual or separate-worker deployments, back up PostgreSQL, deploy the code, run `cd backend && swarmos migrate`, then check `/api/health/ready` before routing users to it.
* **Public endpoints:** citizen intake/reference lookup and SakhiRoute use hashed-client-key counters in PostgreSQL, shared across API replicas. Fixed-window limits may allow a boundary burst; an edge/Redis sliding-window limiter is stronger for high-traffic public exposure.
* **Optional data key:** `SWARMOS_OGD_API_KEY` enables the data.gov.in CPCB feed. Obtain the free key directly from data.gov.in and set it as a Render secret; do not place it in Vercel or Git. See [DATA_SOURCES.md](DATA_SOURCES.md).

## Vercel (API and frontend together, previous setup)

This section describes running the API as a Vercel function. The current `vercel.json` no longer does that; to go
back, restore the `services` configuration from git history (commit `afdb1da`).


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
  up and rewritten to the `postgresql+psycopg://` driver. `SWARMOS_DATABASE_URL` takes precedence if set, then
  `SWARMOS_DATABASE_DATABASE_URL`, which is what Neon injects when connected with the custom environment variable
  prefix `SWARMOS_DATABASE`. Use that prefix to switch to a new database while an old one is still connected.
* **Jobs:** `SWARMOS_JOB_MODE=inline`. A request that enqueues a job (agent task, sync, training) returns `202`
  immediately, then runs the job in the same function invocation after the response is sent. The cron at
  `/api/internal/cron` schedules periodic syncs and drains any queued or expired-lease jobs within
  `SWARMOS_CRON_BUDGET_SECONDS` (default 240 s, kept under the function's max duration).
* **ML:** the function is installed **without** the optional `ml` extra (scikit-learn, SciPy, joblib), which
  would push the Python bundle past Vercel's function size limit. Agents that use a trained model therefore run
  their deterministic baselines (seasonal-naive forecasts, keyword rules, engineering bands) and say so in their
  result; the Model Lab lists the registry but answers training requests with `503 training_unavailable`.
  Run SwarmOS on a full installation (`pip install -e "backend[ml]"`) when you need trained models.

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
4. Initialise the database. Either **from the deployment** (works even when Postgres is not reachable from your
   machine; idempotent, guarded by `CRON_SECRET`):

   ```bash
   curl -X POST https://<deployment>/api/internal/bootstrap \
     -H "Authorization: Bearer $CRON_SECRET" -H 'content-type: application/json' \
     -d '{"admin": {"email": "admin@example.gov.in", "password": "<12+ chars, mixed case + digit>"}}'
   ```

   This runs `alembic upgrade head`, seeds the catalog, source registry and sample documents (no demo data), and
   creates the admin with access to all cities unless that email already exists. Omit the body to only migrate
   and seed. Or **from your machine**, against the production URL:

   ```bash
   export SWARMOS_ENV=production SWARMOS_DATABASE_URL='postgresql+psycopg://…'   # from the Neon dashboard
   backend/.venv/bin/swarmos migrate
   backend/.venv/bin/swarmos seed --no-demo
   backend/.venv/bin/swarmos create-user admin@example.gov.in admin --all-cities
   ```

   Migrations do not run in the build: call the bootstrap endpoint (or `swarmos migrate`) after deploying any
   commit that adds a migration.
5. Deploy. Check `GET /api/health/ready` on the deployment URL.

### Limits to check for your plan

* **Region (latency):** the API and the database must be in the same region. A dashboard request makes about 50
  database queries, so each cross-continent round trip (~200 ms) turns into seconds of lag. `vercel.json` pins
  functions to Singapore (`"regions": ["sin1"]`) because the Neon database is in AWS Asia Pacific (Singapore,
  `ap-southeast-1`), the closest Neon region to users in India. If you move the database, change `regions` to
  match (for example `iad1` with Neon in AWS US East). Verify with `GET /api/health/ready`: `db_latency_ms` should be a few milliseconds, and
  `region` shows where the function ran.
* **Cron frequency:** `vercel.json` schedules the cron once a day (`"0 6 * * *"`), which the Hobby plan allows.
  On Pro you can run it more often, for example every 30 minutes (`"*/30 * * * *"`). Between runs, data refreshes on *Sync now*.
* **Function duration:** 300 s needs Fluid compute (the default for new projects). Lower `maxDuration` and
  `SWARMOS_CRON_BUDGET_SECONDS` together if your plan caps it lower.
* **Bundle size:** a Python function may be at most 225 MB after Vercel's optimisation. With the `ml` extra the
  bundle was 245 MB and failed (`LAMBDA_SIZE_EXCEEDED` in `vercel build`); without it the environment is about
  143 MB. Do not add scikit-learn back to the core dependencies. `.vercelignore` keeps tests, local venvs, ML
  artifacts and node_modules out of CLI uploads.
* **Local AI:** Ollama / llama.cpp must run on loopback, which a Vercel function does not have. On Vercel, agents
  use the deterministic fallbacks.
* **Build locally first:** `npx vercel@65.1.0 build` from the repository root (it needs `.vercel/project.json`,
  created by `vercel link`) reproduces the cloud build, including the handler check and the size limit.

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
| Vercel: `500 FUNCTION_INVOCATION_FAILED` | The function crashed while starting. Check the deployment's build log for `PYTHON_HANDLER_NOT_FOUND` or `LAMBDA_SIZE_EXCEEDED` (a failed build leaves the previous deployment live), then the function logs. |
| Vercel: readiness says migrations are `missing` | Call `POST /api/internal/bootstrap` with the cron secret, or run `swarmos migrate` against the production database (step 4 above). |
| `database is locked` on SQLite | Use one process with the embedded worker, or move to PostgreSQL. |
| Startup error about a non-loopback model URL | The local-only policy is working. Use `127.0.0.1`, or set `SWARMOS_ALLOW_REMOTE_MODEL_HOSTS=true` for your own server. |
| Lost dev passwords | `rm var/swarmos.db && swarmos init` (dev only) or `swarmos create-user`. |

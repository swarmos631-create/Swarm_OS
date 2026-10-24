# Deployment and operations

## Topologies

* **Single node (default):** `swarmos serve` runs the API, the embedded worker and the PWA (from `frontend/dist`) on one port. Put HTTPS in front of it.
* **Scaled:** N API replicas with `SWARMOS_WORKER_EMBEDDED=false`, plus one or more `swarmos worker` processes, all on PostgreSQL. The job queue's compare-and-set claim and leases make several workers safe.

## Production steps

```bash
python3 -m venv /opt/swarmos/venv && /opt/swarmos/venv/bin/pip install -e "backend[postgres]"
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
| `database is locked` on SQLite | Use one process with the embedded worker, or move to PostgreSQL. |
| Startup error about a non-loopback model URL | The local-only policy is working. Use `127.0.0.1`, or set `SWARMOS_ALLOW_REMOTE_MODEL_HOSTS=true` for your own server. |
| Lost dev passwords | `rm var/swarmos.db && swarmos init` (dev only) or `swarmos create-user`. |

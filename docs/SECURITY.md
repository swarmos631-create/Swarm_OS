# Security and privacy

## Controls implemented

| Area | Control |
|---|---|
| Passwords | Argon2id (argon2-cffi defaults), automatic rehash, ≥ 12 chars with mixed case and a digit, constant-time path for unknown accounts |
| Brute force | Per-IP login rate limit (`SWARMOS_LOGIN_RATE_LIMIT_PER_MINUTE`) and account lockout (5 failures → 15 min) |
| Sessions | 256-bit random token in an `HttpOnly`, `SameSite=Lax`, `Secure` (production) cookie; only its SHA-256 is stored; server-side expiry; logout deletes the session |
| CSRF | Double-submit token (`swarmos_csrf` cookie, `SameSite=Strict`, echoed as `X-CSRF-Token`), compared in constant time on every non-GET request |
| Authorisation | Server-side RBAC (viewer < operator < admin) on every route, plus city scope; cross-city record IDs return 404; agent `min_role` enforced in the planner; action approval enforces the action's `required_role` |
| Input validation | Pydantic models on every body and query; enums for statuses and severities; coordinate bounds; incident location must be within 150 km of the city; size limits on text and documents |
| Database | SQLAlchemy parameterised queries only (no string-built SQL) |
| Output | JSON only from the API; React escapes rendered text; no `dangerouslySetInnerHTML`; external links use `rel="noreferrer noopener"` |
| Headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, CSP (`default-src 'none'` for the API; a restrictive policy for the PWA), HSTS in production, `Cache-Control: no-store` on the API |
| CORS | Off by default (the PWA is same-origin). Explicit allow-list only; `*` is rejected in production. |
| Errors | Consistent format with a correlation ID; unhandled exceptions return a generic message; details stay in the logs |
| Logging | Structured JSON; keys and values matching password/token/secret/api-key/cookie/session are redacted |
| Audit | Login success and failure, logout, incident create/update, service requests, task submit/cancel, action approve/reject, document ingest, sync triggers, user creation, model train/promote/rollback |
| AI safety | Agents cannot execute anything; only human-approved internal actions run; compliance can only tighten approval roles; prompts and retrieved documents are treated as data |
| Secrets | Environment only; `.env` and `var/` are git-ignored; dev passwords go to a 0600 file and are never logged |
| Production guardrails | Startup fails if `SWARMOS_ENV=production` and cookies are insecure, CORS is `*`, or a dev seed password is set; dev account seeding refuses to run in production; `/api/docs` is disabled in production |
| Privacy | Health data is accepted only as aggregates, with small-cell suppression (< 10); the training pipeline scans text for phone, email, Aadhaar-like and PAN-like patterns and fails on any hit; demo data is synthetic |

## Production checklist

1. Serve over HTTPS behind a reverse proxy (nginx, Caddy, or a cloud load balancer). Set `SWARMOS_ENV=production`
   and `SWARMOS_TRUSTED_HOSTS`.
2. Use PostgreSQL with least-privilege credentials (the app role needs DML only; run migrations with a separate role).
3. Create accounts with `swarmos create-user`. Do not run `swarmos init` dev seeding in production; use `swarmos migrate` plus `swarmos seed --no-demo`.
4. Put a shared rate limiter at the proxy (the built-in limiter is per process).
5. Set log retention: application logs 30 days, audit log at least 1 year (archive `audit_log` rows; do not delete
   them from the application).
6. **Identity provider**: for SSO, terminate OIDC at the proxy (for example oauth2-proxy with your IdP) and map the
   verified identity to a SwarmOS user. A native OIDC login is the recommended next step and is not yet implemented.
7. Review the CSP if you change the tile provider (`img-src` / `connect-src`).

## Known gaps

* No MFA yet; rely on IdP MFA through the proxy.
* The in-process rate limiter does not coordinate across replicas.
* No file uploads are accepted (documents are submitted as text), so the upload attack surface is intentionally absent.

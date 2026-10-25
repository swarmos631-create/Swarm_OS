# SwarmOS as one service (Railway): the PWA is built with Node, then served by the FastAPI app together with
# the API and the embedded job worker. Same origin, so no proxy or CORS is needed.

# ---------------------------------------------------------------- website (React PWA)
FROM node:22-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# MapmyIndia browser key (public by design: it ships in the JavaScript and is locked to the site's domain).
# Railway passes service variables with the same name as build arguments.
ARG VITE_MAPPLS_KEY=""
ENV VITE_MAPPLS_KEY=${VITE_MAPPLS_KEY}
RUN npm run build

# ---------------------------------------------------------------- API + worker
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY backend/ backend/
COPY ml/ ml/
RUN pip install --upgrade pip && pip install -e "./backend[ml]"
COPY --from=web /app/frontend/dist frontend/dist
ENV SWARMOS_ENV=production \
    SWARMOS_JOB_MODE=worker \
    SWARMOS_WORKER_EMBEDDED=true
EXPOSE 8080
# `init` = migrations + idempotent catalogue/registry/document seeding, so an empty database works on first boot.
CMD ["sh", "-c", "cd backend && swarmos init && exec swarmos serve --host 0.0.0.0 --port ${PORT:-8080}"]

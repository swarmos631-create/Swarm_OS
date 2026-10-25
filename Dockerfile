# SwarmOS as one Railway service: build the PWA, then serve it with the API and embedded worker.

# ---------------------------------------------------------------- website (React PWA)
FROM node:22-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Browser keys are public by design; restrict them to the deployed domain in the provider console.
ARG VITE_MAPPLS_KEY=""
ENV VITE_MAPPLS_KEY=${VITE_MAPPLS_KEY}
RUN npm run build

# ---------------------------------------------------------------- API + worker
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SWARMOS_ENV=production \
    SWARMOS_AUTH_DISABLED=false \
    SWARMOS_JOB_MODE=worker \
    SWARMOS_WORKER_EMBEDDED=true \
    SWARMOS_DEMO_MODE=false
WORKDIR /app
COPY backend/ backend/
COPY ml/ ml/
RUN pip install --upgrade pip && pip install -e "./backend[ml]"
COPY --from=web /app/frontend/dist frontend/dist
EXPOSE 8080
# Migrations must succeed before serving. Optional demo/catalogue seeding is best effort.
CMD ["sh", "-c", "cd backend && swarmos migrate && { swarmos seed || echo 'swarmos seed failed - starting anyway'; } && exec swarmos serve --host 0.0.0.0 --port ${PORT:-8080}"]

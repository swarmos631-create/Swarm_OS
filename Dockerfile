# Build the PWA, then serve it from the same FastAPI deployment as the API and durable worker.
FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SWARMOS_ENV=production \
    SWARMOS_AUTH_DISABLED=false \
    SWARMOS_JOB_MODE=worker \
    SWARMOS_WORKER_EMBEDDED=true \
    SWARMOS_DEMO_MODE=false
WORKDIR /app
COPY backend/ ./backend/
RUN cd /app/backend && pip install --no-cache-dir '.[ml]'
COPY --from=frontend-build /app/frontend/dist/ ./frontend/dist/
EXPOSE 8000
CMD ["/bin/sh", "-c", "swarmos migrate && exec swarmos serve --host 0.0.0.0 --port ${PORT:-8000}"]

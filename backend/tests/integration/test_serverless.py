"""Serverless (Vercel) behaviour: configuration guards, inline jobs, cron, and database-stored model artifacts."""

import importlib
import os
from pathlib import Path

import pytest

from swarmos import config as config_mod
from swarmos.config import Settings, get_settings, normalize_database_url
from tests.conftest import login


def test_database_url_fallbacks_and_driver_normalisation(monkeypatch):
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
    monkeypatch.delenv("SWARMOS_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", "postgres://neon/db")
    assert Settings().database_url == "postgresql+psycopg://neon/db"
    monkeypatch.setenv("SWARMOS_DATABASE_URL", "sqlite:///explicit.db")
    assert Settings().database_url == "sqlite:///explicit.db"  # explicit setting wins


def test_vercel_rejects_sqlite_and_defaults_to_inline_jobs(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    for var in ("SWARMOS_JOB_MODE", "SWARMOS_ML_ROOT"):
        monkeypatch.delenv(var, raising=False)
    try:
        mod = importlib.reload(config_mod)
        with pytest.raises(ValueError, match="SQLite cannot be used on Vercel"):
            mod.Settings(database_url="sqlite:///x.db")
        s = mod.Settings(database_url="postgresql://u:p@h/db")
        assert s.job_mode == "inline" and s.worker_embedded is False
        assert str(s.ml_root).startswith("/tmp")  # noqa: S108
        assert s.ml_configs_dir.joinpath("complaint_classifier.yaml").exists()
    finally:
        monkeypatch.delenv("VERCEL")
        importlib.reload(config_mod)


def test_cron_endpoint_requires_secret(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "cron_secret", None)
    assert client.get("/api/internal/cron").status_code == 503
    monkeypatch.setattr(settings, "cron_secret", "s3cret-value")
    assert client.get("/api/internal/cron").status_code == 401
    assert client.get("/api/internal/cron", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_cron_drains_queued_jobs(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "cron_secret", "s3cret-value")
    monkeypatch.setattr(settings, "sync_enabled", False)
    login(client, "operator@swarmos.local")
    task = client.post("/api/v1/cities/bengaluru/tasks", json={"prompt": "budget utilisation"}).json()
    assert task["status"] == "queued"
    r = client.get("/api/internal/cron", headers={"Authorization": "Bearer s3cret-value"})
    assert r.status_code == 200 and r.json()["ran"] >= 1
    done = client.get(f"/api/v1/cities/bengaluru/tasks/{task['id']}").json()
    assert done["status"] in ("succeeded", "partial")


def test_inline_mode_runs_task_after_response(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_mode", "inline")
    login(client, "operator@swarmos.local")
    task = client.post("/api/v1/cities/bengaluru/tasks", json={"prompt": "check water quality"}).json()
    # TestClient runs background tasks before returning, like the serverless invocation does.
    done = client.get(f"/api/v1/cities/bengaluru/tasks/{task['id']}").json()
    assert done["status"] in ("succeeded", "partial")
    assert all(e["status"] not in ("pending", "running") for e in done["executions"])


def test_promoted_model_loads_from_database_without_files():
    from swarmos.ai.runtime import get_runtime
    from swarmos.ml import pipeline

    s = get_settings()
    r = pipeline.train_model("water_demand_forecaster", promote_if_better=True)
    assert r["promoted"]
    for p in Path(s.ml_root).rglob("*"):  # simulate a fresh serverless instance: no local artifacts or reports
        if p.is_file() and p.suffix in (".joblib", ".md"):
            os.remove(p)
    get_runtime().artifacts.invalidate()
    assert get_runtime().load("water_demand_forecaster").version == r["version"]
    v = next(v for v in pipeline.list_versions("water_demand_forecaster") if v.version == r["version"])
    pipeline.promote("water_demand_forecaster", v.version)  # checksum verified against the database copy

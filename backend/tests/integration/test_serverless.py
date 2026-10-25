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
    monkeypatch.setenv("SWARMOS_DATABASE_DATABASE_URL", "postgres://neon-prefixed/db")
    assert Settings().database_url == "postgresql+psycopg://neon-prefixed/db"  # integration with prefix SWARMOS_DATABASE
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


def test_database_faults_return_actionable_503(tmp_path):
    from fastapi.testclient import TestClient

    from swarmos.db import reset_engine
    from swarmos.main import create_app

    reset_engine(f"sqlite:///{tmp_path / 'empty.db'}")  # reachable but never migrated
    try:
        with TestClient(create_app()) as c:
            r = c.get("/api/v1/auth/me", cookies={"swarmos_session": "x"})
            assert r.status_code == 503 and r.json()["error"]["code"] == "database_not_migrated"
            assert c.get("/api/health/ready").json()["checks"]["migrations"] == "missing"
    finally:
        reset_engine()


def test_invalid_configuration_is_served_as_503_instead_of_crashing():
    from fastapi.testclient import TestClient

    from swarmos.main import _misconfigured_app

    c = TestClient(_misconfigured_app(["SQLite cannot be used on Vercel."]))
    for method in ("get", "post"):
        r = getattr(c, method)("/api/v1/auth/me")
        assert r.status_code == 503
        assert r.json()["error"] == {"code": "misconfigured", "message": "SQLite cannot be used on Vercel.",
                                     "details": None, "correlation_id": None}


def test_without_ml_extra_models_degrade_to_baselines(admin, monkeypatch):
    """The Vercel function ships without scikit-learn: training is refused clearly and inference falls back."""
    from swarmos.ai.runtime import ModelUnavailable, SklearnArtifacts
    from swarmos.ml import pipeline

    monkeypatch.setattr(pipeline, "training_available", lambda: False)
    r = admin.post("/api/v1/models/complaint_classifier/train", json={})
    assert r.status_code == 503 and r.json()["error"]["code"] == "training_unavailable"
    assert admin.get("/api/v1/models").status_code == 200  # the registry still lists models
    with pytest.raises(ModelUnavailable):  # agents catch this and use their deterministic baselines
        SklearnArtifacts(get_settings()).load("complaint_classifier")
    with pytest.raises(pipeline.TrainingUnavailable):
        pipeline.train_model("complaint_classifier")


def test_bootstrap_requires_secret_and_is_idempotent(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "cron_secret", None)
    assert client.post("/api/internal/bootstrap").status_code == 503
    monkeypatch.setattr(settings, "cron_secret", "s3cret-value")
    assert client.post("/api/internal/bootstrap").status_code == 401
    auth = {"Authorization": "Bearer s3cret-value"}
    weak = client.post("/api/internal/bootstrap", headers=auth, json={"admin": {"email": "a@b.co", "password": "weakpassword1"}})
    assert weak.status_code == 422
    admin = {"admin": {"email": "Root@Example.org", "password": "Bootstrap-Pass-123"}}
    first = client.post("/api/internal/bootstrap", headers=auth, json=admin)
    assert first.status_code == 200, first.text
    assert first.json()["migrated"] is True and first.json()["admin"] == "created"
    assert first.json()["seeded"]["cities"] > 0
    again = client.post("/api/internal/bootstrap", headers=auth, json=admin)
    assert again.status_code == 200 and again.json()["admin"] == "exists"
    assert client.post("/api/internal/bootstrap", headers=auth).json().get("admin") is None
    r = client.post("/api/v1/auth/login", json={"email": "root@example.org", "password": "Bootstrap-Pass-123"})
    assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["all_cities"] is True


def test_open_access_needs_no_sign_in(monkeypatch):
    from fastapi.testclient import TestClient

    from swarmos.main import create_app

    settings = get_settings()
    anon = TestClient(create_app())
    assert anon.get("/api/v1/auth/me").status_code == 401  # sign-in required by default
    monkeypatch.setattr(settings, "auth_disabled", True)
    me = anon.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["role"] == "admin" and me.json()["auth_disabled"] is True
    assert me.json()["email"] == settings.open_access_email
    assert anon.get("/api/v1/cities").status_code == 200
    # Writes work without a CSRF token: there is no session to protect.
    r = anon.post("/api/v1/cities/bengaluru/tasks", json={"prompt": "check water quality"})
    assert r.status_code in (200, 201, 202), r.text
    assert anon.get("/api/v1/auth/me").json()["id"] == me.json()["id"]  # one shared account, created once


def test_simulated_data_for_any_city_runs_every_agent(admin, monkeypatch):
    """With SWARMOS_SIMULATE_ALL_CITIES a never-synced city gets climate-calibrated simulated data on first visit."""
    from swarmos.agents.registry import REGISTRY
    from swarmos.demo import seed

    settings = get_settings()
    monkeypatch.setattr(settings, "simulate_all_cities", True)
    monkeypatch.setattr(settings, "job_mode", "inline")
    seed._checked.clear()
    d = admin.get("/api/v1/cities/jaipur/dashboard").json()
    by_id = {i["id"]: i for i in d["indicators"]}
    assert by_id["temperature"]["provenance"] == "demo" and by_id["power_demand"]["status"] == "available"
    task = admin.post("/api/v1/cities/jaipur/tasks", json={"prompt": "full audit", "agent_ids": list(REGISTRY)}).json()
    done = admin.get(f"/api/v1/cities/jaipur/tasks/{task['id']}").json()
    statuses = {e["agent_id"]: e["status"] for e in done["executions"]}
    assert len(statuses) == len(REGISTRY)
    assert not [a for a, s in statuses.items() if s not in ("succeeded", "degraded")], statuses


def test_simulated_weather_follows_city_climate():
    from datetime import UTC, datetime

    from swarmos.demo import simulate
    from swarmos.models import City

    now = datetime(2026, 5, 20, 6, tzinfo=UTC)
    delhi = City(id="delhi", name="Delhi", state="Delhi", lat=28.61, lon=77.21, bbox=[76.8, 28.4, 77.4, 28.9], population=16_000_000)
    shimla = City(id="shimla", name="Shimla", state="HP", lat=31.10, lon=77.17, bbox=[77.0, 31.0, 77.3, 31.2], population=200_000)
    temp = lambda c: max(m.value for m in simulate.environment(c, now) if m.metric == "daily_temperature_2m_max")  # noqa: E731
    assert simulate.nearest_climate(delhi)[0] == "Delhi" and temp(delhi) > temp(shimla) + 8  # May: plains far hotter than hills
    assert len([f for f in simulate.facilities(delhi) if f.layer == "hospital"]) > len([f for f in simulate.facilities(shimla) if f.layer == "hospital"])

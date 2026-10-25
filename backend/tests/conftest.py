"""Test fixtures: an isolated, migrated SQLite database and ML root per test session."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="swarmos-test-"))
REPO = Path(__file__).resolve().parents[2]
os.environ.update({
    "SWARMOS_ENV": "test",
    # Set SWARMOS_TEST_DATABASE_URL to run the suite against PostgreSQL (use an empty, disposable database).
    "SWARMOS_DATABASE_URL": os.environ.get("SWARMOS_TEST_DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}"),
    "SWARMOS_ML_ROOT": str(_TMP / "ml"),
    "SWARMOS_ML_CONFIGS_DIR": str(_TMP / "ml" / "configs"),
    "SWARMOS_JOB_MODE": "worker",
    "SWARMOS_DEV_SEED_PASSWORD": "TestPassw0rd-123",
    "SWARMOS_SYNC_ENABLED": "false",
    "SWARMOS_DEMO_MODE": "true",
    "SWARMOS_SIMULATE_ALL_CITIES": "false",
    "SWARMOS_DEMO_PILOT_CITIES": "bengaluru",
    "SWARMOS_WORKER_EMBEDDED": "false",
    "SWARMOS_LOGIN_RATE_LIMIT_PER_MINUTE": "1000",
    "SWARMOS_OLLAMA_URL": "",
    "SWARMOS_LLAMACPP_URL": "",
})
for var in ("SWARMOS_OGD_API_KEY", "SWARMOS_DATABASE_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL", "CRON_SECRET", "VERCEL"):
    os.environ.pop(var, None)
# Tests rewrite training configs, so they work on a private copy of the packaged configs.
shutil.copytree(REPO / "backend" / "swarmos" / "ml" / "configs", _TMP / "ml" / "configs")

PASSWORD = "TestPassw0rd-123"


@pytest.fixture(scope="session", autouse=True)
def database():
    # Keep the suite offline: no water-mask tiles (bounding-box placement) and every upstream sync gets HTTP 503.
    import httpx
    from alembic import command

    from swarmos.cli import _alembic_cfg
    from swarmos.config import get_settings
    from swarmos.db import reset_engine, session_scope
    from swarmos.demo import seed
    from swarmos.demo.seed import bootstrap
    from swarmos.integrations import sync

    seed.land_mask = lambda city, timeout=15.0: None  # type: ignore[assignment]
    sync.http_client = lambda settings: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))  # type: ignore[assignment]
    get_settings.cache_clear()
    reset_engine()
    command.upgrade(_alembic_cfg(), "head")
    with session_scope() as db:
        bootstrap(db, get_settings())
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from swarmos.core.ratelimit import limiter
    from swarmos.main import create_app

    limiter.reset()
    with TestClient(create_app()) as c:
        yield c


def login(client, email: str = "admin@swarmos.local", password: str = PASSWORD) -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    me = r.json()
    client.headers["x-csrf-token"] = me["csrf_token"]
    return me


@pytest.fixture()
def admin(client):
    login(client)
    return client


@pytest.fixture()
def operator(client):
    login(client, "operator@swarmos.local")
    return client


@pytest.fixture()
def viewer(client):
    login(client, "viewer@swarmos.local")
    return client

"""Typed, validated application configuration.

All settings come from environment variables (prefix ``SWARMOS_``) or a ``.env`` file. Validation errors are
raised at startup with actionable messages instead of failing later at request time.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent
# Vercel sets VERCEL=1 in builds and functions. Functions there have a read-only filesystem (except /tmp), no
# persistent disk, and no process that outlives a request — so storage and background work are configured differently.
ON_VERCEL = os.environ.get("VERCEL") == "1"


def normalize_database_url(url: str) -> str:
    """Accept the plain postgres:// URLs that hosting providers inject and select the psycopg (v3) driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _is_loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SWARMOS_",
        env_file=(str(REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Falls back to the variables that Vercel's Postgres integrations inject: SWARMOS_DATABASE_DATABASE_URL when the
    # Neon integration is connected with the custom prefix "SWARMOS_DATABASE" (which lets a new database take over
    # from an old one that is still connected), otherwise the unprefixed DATABASE_URL / POSTGRES_URL.
    database_url: str = Field(
        default=f"sqlite:///{REPO_ROOT / 'var' / 'swarmos.db'}",
        validation_alias=AliasChoices("SWARMOS_DATABASE_URL", "SWARMOS_DATABASE_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL"),
    )

    # Security
    # Open access: no sign-in screen. A request without a valid session acts as one built-in admin account
    # (open_access_email), so anyone who can reach the URL can do everything. For demos and prototypes only.
    auth_disabled: bool = False
    open_access_email: str = "guest@swarmos.local"
    session_ttl_hours: int = Field(default=12, ge=1, le=24 * 30)
    cookie_secure: bool | None = None  # defaults to True in production
    cors_origins: CsvList = Field(default_factory=list)
    trusted_hosts: CsvList = Field(default_factory=lambda: ["*"])
    login_rate_limit_per_minute: int = 10
    task_rate_limit_per_minute: int = 30

    # Data
    demo_mode: bool = True
    # Generate simulated data for every city on first visit (not only the pilot cities). Needs demo_mode.
    simulate_all_cities: bool = True
    # Long-running servers build every city's simulated data in the background after startup (no first-visit wait).
    prebuild_cities: bool = True
    # Stop pre-building once the database reaches this size (MB); other cities are then built on their first visit.
    # Railway's free volume is 500 MB, so keep room for live data, models and write-ahead log.
    prebuild_db_budget_mb: int = Field(default=120, ge=50)
    # PostgreSQL write-ahead-log cap applied at startup when the app's database user may change it (own database
    # only). Postgres defaults to 1 GB, which can fill a small volume during bulk writes. Empty = leave as is.
    db_max_wal_size: str | None = "64MB"
    # Train any configured ML model that has no promoted version when the API starts (needs the `ml` extra).
    auto_train_models: bool = True
    demo_pilot_cities: CsvList = Field(default_factory=lambda: ["bengaluru", "mumbai", "delhi"])
    http_timeout_seconds: float = 15.0
    http_user_agent: str = "SwarmOS/0.1 (+https://github.com/swarmos631-create/Swarm_OS)"
    sync_enabled: bool = True
    sync_interval_minutes: int = Field(default=30, ge=5)
    # Live readings older than this are pruned daily (and at startup). Hourly feeds for ~90 cities are the largest
    # table, so keep it short on small databases; simulated history is unaffected.
    measurement_retention_days: int = Field(default=3, ge=2, le=365)
    sync_cities: CsvList = Field(default_factory=lambda: ["bengaluru", "mumbai", "delhi"])

    open_meteo_base_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_aq_base_url: str = "https://air-quality-api.open-meteo.com/v1/air-quality"
    usgs_feed_url: str = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_week.geojson"
    overpass_urls: CsvList = Field(
        default_factory=lambda: [
            "https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ]
    )
    openaq_api_key: str | None = None  # OpenAQ v3 key — CPCB/state station data (reachable from any country)
    ogd_api_key: str | None = None  # data.gov.in key — required for CPCB station feed
    ogd_cpcb_resource_id: str = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
    imd_api_base_url: str | None = None  # IMD access is by arrangement; unset == unavailable
    # MapmyIndia (Mappls) static key — real facility locations via the Mappls nearby-places API.
    # Server-side MapmyIndia credentials. The browser key (VITE_MAPPLS_KEY) is deliberately NOT used here: it is
    # restricted to the website's domain, so Mappls rejects it from the server (HTTP 401).
    mappls_api_key: str | None = Field(default=None, validation_alias=AliasChoices("SWARMOS_MAPPLS_API_KEY", "MAPPLS_API_KEY"))
    mappls_client_id: str | None = None
    tomtom_api_key: str | None = None  # TomTom developer key — live corridor speeds (free tier ~2,500 calls/day)
    tomtom_points_per_city: int = Field(default=5, ge=1, le=12)
    mappls_client_secret: str | None = None
    mappls_search_radius_m: int = Field(default=12000, ge=1000, le=50000)

    # Worker / orchestration
    # "worker": a long-running worker (embedded thread or `swarmos worker`) drains the job queue.
    # "inline": each job runs right after the request that enqueued it (serverless); cron drains leftovers.
    job_mode: Literal["worker", "inline"] = "inline" if ON_VERCEL else "worker"
    worker_embedded: bool = True
    cron_secret: str | None = Field(default=None, validation_alias=AliasChoices("SWARMOS_CRON_SECRET", "CRON_SECRET"))
    cron_budget_seconds: float = Field(default=240.0, gt=0, le=780)
    worker_poll_seconds: float = 1.0
    agent_max_concurrency: int = Field(default=6, ge=1, le=32)
    agent_default_timeout_seconds: float = Field(default=20.0, gt=0)
    task_max_agents: int = Field(default=14, ge=1, le=50)

    # Local AI runtime — loopback only unless explicitly allowed
    ollama_url: str | None = None  # e.g. http://127.0.0.1:11434
    ollama_model: str = "llama3.2:3b"
    llamacpp_url: str | None = None  # e.g. http://127.0.0.1:8080 (llama.cpp server)
    allow_remote_model_hosts: bool = False
    model_request_timeout_seconds: float = 30.0
    model_max_concurrency: int = Field(default=2, ge=1, le=8)
    llm_narratives_enabled: bool = False

    # ML paths. Configs ship inside the package; datasets/artifacts/reports are scratch space (artifacts and
    # reports are also stored in the database, so serverless instances can load models without a shared disk).
    ml_root: Path = Path("/tmp/swarmos-ml") if ON_VERCEL else REPO_ROOT / "ml"  # noqa: S108 — Vercel's only writable dir
    ml_configs_path: Path = Field(default=PACKAGE_DIR / "ml" / "configs",
                                  validation_alias=AliasChoices("SWARMOS_ML_CONFIGS_DIR", "ml_configs_path"))

    # Dev bootstrap
    dev_seed_password: str | None = None

    @field_validator("cors_origins", "trusted_hosts", "demo_pilot_cities", "sync_cities", "overpass_urls", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_db(cls, v: str) -> str:
        return normalize_database_url(v)

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if ON_VERCEL and self.is_sqlite:
            raise ValueError(
                "SQLite cannot be used on Vercel: function filesystems are ephemeral and not shared between "
                "instances. Add a Postgres database (for example Neon from the Vercel Marketplace) so DATABASE_URL "
                "is set, or set SWARMOS_DATABASE_URL."
            )
        if self.job_mode == "inline":
            self.worker_embedded = False
        if self.cookie_secure is None:
            self.cookie_secure = self.env == "production"
        for name in ("ollama_url", "llamacpp_url"):
            url = getattr(self, name)
            if url and not self.allow_remote_model_hosts and not _is_loopback(url):
                raise ValueError(
                    f"SWARMOS_{name.upper()}={url!r} is not a loopback address. Local model endpoints must run on "
                    "this host. Set SWARMOS_ALLOW_REMOTE_MODEL_HOSTS=true only for a self-hosted inference server "
                    "you operate."
                )
        if self.env == "production":
            if "*" in self.cors_origins:
                raise ValueError("SWARMOS_CORS_ORIGINS must not contain '*' in production.")
            if self.dev_seed_password:
                raise ValueError("SWARMOS_DEV_SEED_PASSWORD must not be set in production.")
            if not self.cookie_secure:
                raise ValueError("SWARMOS_COOKIE_SECURE must be true in production (HTTPS).")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def datasets_dir(self) -> Path:
        return self.ml_root / "datasets"

    @property
    def artifacts_dir(self) -> Path:
        return self.ml_root / "artifacts"

    @property
    def ml_configs_dir(self) -> Path:
        return self.ml_configs_path

    @property
    def reports_dir(self) -> Path:
        return self.ml_root / "reports"


@lru_cache
def get_settings() -> Settings:
    return Settings()

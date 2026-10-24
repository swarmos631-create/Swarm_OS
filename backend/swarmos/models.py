"""ORM models. The schema is portable between SQLite (development) and PostgreSQL (production).

Coordinates are stored as WGS84 latitude/longitude floats with composite indexes. On PostgreSQL a PostGIS
geography column can be added by migration without changing the domain model (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from swarmos.db import Base, utcnow


def new_id() -> str:
    return uuid.uuid4().hex


class UTCDateTime(TypeDecorator[datetime]):
    """Always returns timezone-aware UTC datetimes (SQLite drops tzinfo; PostgreSQL keeps it)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not allowed; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


TS = UTCDateTime()

ROLES = ("viewer", "operator", "admin")
PROVENANCE = ("live", "cached", "demo", "operator", "derived", "synthetic")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    all_cities: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(TS)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TS)

    cities: Mapped[list[UserCity]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (CheckConstraint("role in ('viewer','operator','admin')", name="role_valid"),)


class UserCity(Base):
    __tablename__ = "user_cities"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), primary_key=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TS, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(200))


class City(Base):
    __tablename__ = "cities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug
    name: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(120))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    zoom: Mapped[float] = mapped_column(Float, default=11.0)
    bbox: Mapped[list] = mapped_column(default=list)  # [min_lon, min_lat, max_lon, max_lat] — approximate viewport
    population: Mapped[int | None] = mapped_column(Integer)
    population_source: Mapped[str | None] = mapped_column(String(200))
    aliases: Mapped[list] = mapped_column(default=list)
    is_pilot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))  # api | feed | fixture | internal
    categories: Mapped[list] = mapped_column(default=list)
    coverage: Mapped[str] = mapped_column(String(300))
    auth_required: Mapped[str] = mapped_column(String(160), default="none")
    license: Mapped[str] = mapped_column(String(300))
    attribution: Mapped[str] = mapped_column(String(300))
    homepage: Mapped[str | None] = mapped_column(String(300))
    freshness_threshold_s: Mapped[int] = mapped_column(Integer, default=3600)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    status_detail: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(TS)
    last_attempt_at: Mapped[datetime | None] = mapped_column(TS)
    last_error_at: Mapped[datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[list] = mapped_column(default=list)
    setup: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    city_id: Mapped[str | None] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16))  # succeeded | failed | skipped
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    records: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Measurement(Base):
    """A time-stamped metric value. ``kind`` separates observations from forecasts and model analyses."""

    __tablename__ = "measurements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(32))
    metric: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(120), default="city")  # station / corridor / feeder / DMA
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(24), default="observation")  # observation|forecast|model_analysis
    observed_at: Mapped[datetime] = mapped_column(TS)
    fetched_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    provenance: Mapped[str] = mapped_column(String(16))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    meta: Mapped[dict] = mapped_column(default=dict)

    __table_args__ = (
        UniqueConstraint("city_id", "source_id", "metric", "entity", "kind", "observed_at", name="uq_measurement"),
        Index("ix_meas_city_cat_time", "city_id", "category", "observed_at"),
        Index("ix_meas_city_metric_time", "city_id", "metric", "observed_at"),
    )


class GeoFeature(Base):
    __tablename__ = "geo_features"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[str | None] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    layer: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(120))
    name: Mapped[str | None] = mapped_column(String(240))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    properties: Mapped[dict] = mapped_column(default=dict)
    provenance: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime | None] = mapped_column(TS)
    fetched_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", "city_id", name="uq_feature"),
        Index("ix_feat_city_layer", "city_id", "layer"),
        Index("ix_feat_latlon", "lat", "lon"),
    )


INCIDENT_STATUSES = ("open", "acknowledged", "in_progress", "resolved", "closed")
SEVERITIES = ("low", "moderate", "high", "critical")


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="open")
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    department: Mapped[str | None] = mapped_column(String(80))
    provenance: Mapped[str] = mapped_column(String(16), default="operator")
    source: Mapped[str] = mapped_column(String(80), default="operator")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reported_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        CheckConstraint("severity in ('low','moderate','high','critical')", name="severity_valid"),
        CheckConstraint(
            "status in ('open','acknowledged','in_progress','resolved','closed')", name="status_valid"
        ),
        Index("ix_inc_city_status", "city_id", "status"),
        Index("ix_inc_city_reported", "city_id", "reported_at"),
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class ServiceRequest(Base):
    __tablename__ = "service_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(40))
    category_confidence: Mapped[float | None] = mapped_column(Float)
    classified_by: Mapped[str | None] = mapped_column(String(80))
    department: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default="open")
    ward: Mapped[str | None] = mapped_column(String(80))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[str] = mapped_column(String(16), default="operator")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (Index("ix_sr_city_created", "city_id", "created_at"),)


TASK_STATUSES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    prompt: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    plan: Mapped[dict] = mapped_column(default=dict)
    result: Mapped[dict | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)

    executions: Mapped[list[AgentExecution]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="AgentExecution.id"
    )

    __table_args__ = (UniqueConstraint("created_by", "idempotency_key", name="uq_task_idem"),)


class AgentExecution(Base):
    __tablename__ = "agent_executions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    depends_on: Mapped[list] = mapped_column(default=list)
    reason: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict] = mapped_column(default=dict)
    output: Mapped[dict | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    task: Mapped[AgentTask] = relationship(back_populates="executions")

    __table_args__ = (UniqueConstraint("task_id", "agent_id", name="uq_exec_task_agent"),)


class AgentHandoff(Base):
    __tablename__ = "agent_handoffs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    from_agent: Mapped[str] = mapped_column(String(64))
    to_agent: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    payload_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


ACTION_STATUSES = ("pending", "approved", "rejected", "executed", "failed", "awaiting_manual")


class ProposedAction(Base):
    __tablename__ = "proposed_actions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id", ondelete="SET NULL"), index=True)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(240))
    rationale: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(default=dict)
    risk: Mapped[str] = mapped_column(String(16), default="low")
    required_role: Mapped[str] = mapped_column(String(16), default="operator")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    decision_note: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(TS)
    execution_result: Mapped[dict | None] = mapped_column()


JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(TS, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(80))
    locked_until: Mapped[datetime | None] = mapped_column(TS)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column()

    __table_args__ = (Index("ix_jobs_poll", "status", "run_after", "priority"),)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    model_name: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(40))
    task_type: Mapped[str] = mapped_column(String(40))
    algorithm: Mapped[str] = mapped_column(String(120))
    artifact_path: Mapped[str] = mapped_column(String(400))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    metrics: Mapped[dict] = mapped_column(default=dict)
    baseline_metrics: Mapped[dict] = mapped_column(default=dict)
    dataset_manifest: Mapped[dict] = mapped_column(default=dict)
    config: Mapped[dict] = mapped_column(default=dict)
    report_path: Mapped[str | None] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(16), default="candidate")  # candidate|promoted|archived
    passed_gate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(TS)
    promoted_by: Mapped[str | None] = mapped_column(String(32))
    job_id: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (UniqueConstraint("model_name", "version", name="uq_model_version"),)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    city_id: Mapped[str | None] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    source: Mapped[str] = mapped_column(String(300))
    classification: Mapped[str] = mapped_column(String(16), default="internal")  # public|internal|restricted
    license: Mapped[str] = mapped_column(String(200), default="unspecified")
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    uploaded_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    chunks: Mapped[list[DocumentChunk]] = relationship(cascade="all, delete-orphan", order_by="DocumentChunk.ordinal")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(240))
    text: Mapped[str] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32))
    actor_email: Mapped[str | None] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    city_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    details: Mapped[dict] = mapped_column(default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[str | None] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    meta: Mapped[dict] = mapped_column(default=dict)

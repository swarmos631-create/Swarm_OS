"""Durable database-backed job queue with leases (works on SQLite and PostgreSQL)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swarmos.core.logging import correlation_id_var
from swarmos.db import utcnow
from swarmos.models import Job


def enqueue(db: Session, kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None, priority: int = 5,
            max_attempts: int = 3, delay_s: float = 0, created_by: str | None = None) -> Job:
    """Enqueue a job. With ``dedupe_key`` an identical active/completed job is returned instead of a duplicate."""
    if dedupe_key:
        existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing:
            return existing
    job = Job(kind=kind, payload=payload, dedupe_key=dedupe_key, priority=priority, max_attempts=max_attempts,
              run_after=utcnow() + timedelta(seconds=delay_s), correlation_id=correlation_id_var.get(), created_by=created_by)
    try:
        with db.begin_nested():
            db.add(job)
    except IntegrityError:
        existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        assert existing is not None
        return existing
    return job


def claim(db: Session, worker_id: str, kinds: list[str] | None = None, lease_s: int = 300) -> Job | None:
    now = utcnow()
    q = select(Job.id).where(Job.status == "queued", Job.run_after <= now)
    if kinds:
        q = q.where(Job.kind.in_(kinds))
    for job_id in db.scalars(q.order_by(Job.priority, Job.created_at).limit(5)):
        res = db.execute(
            update(Job).where(Job.id == job_id, Job.status == "queued")
            .values(status="running", locked_by=worker_id, locked_until=now + timedelta(seconds=lease_s),
                    started_at=now, attempts=Job.attempts + 1)
        )
        if getattr(res, "rowcount", 0) == 1:  # compare-and-set won
            db.commit()
            return db.get(Job, job_id)
    db.commit()
    return None


def claim_by_id(db: Session, job_id: str, worker_id: str, lease_s: int = 300) -> Job | None:
    """Claim one specific queued job (compare-and-set), for inline execution right after enqueueing."""
    now = utcnow()
    res = db.execute(
        update(Job).where(Job.id == job_id, Job.status == "queued")
        .values(status="running", locked_by=worker_id, locked_until=now + timedelta(seconds=lease_s),
                started_at=now, attempts=Job.attempts + 1)
    )
    db.commit()
    return db.get(Job, job_id) if getattr(res, "rowcount", 0) == 1 else None


def complete(db: Session, job_id: str, result: dict[str, Any] | None = None) -> None:
    job = db.get(Job, job_id)
    if job:
        job.status, job.result, job.finished_at, job.locked_until, job.error = "succeeded", result, utcnow(), None, None


def fail(db: Session, job_id: str, error: str) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    job.error = error[:2000]
    job.locked_until = None
    if job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = utcnow() + timedelta(seconds=min(300, 5 * 2 ** job.attempts))
    else:
        job.status, job.finished_at = "failed", utcnow()


def reap_expired(db: Session) -> int:
    """Return jobs whose worker died (lease expired) to the queue."""
    res = db.execute(
        update(Job).where(Job.status == "running", Job.locked_until < utcnow())
        .values(status="queued", locked_by=None, locked_until=None, error="Lease expired; re-queued")
    )
    return int(getattr(res, "rowcount", 0) or 0)


def extend_lease(db: Session, job_id: str, lease_s: int = 300) -> None:
    db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(locked_until=utcnow() + timedelta(seconds=lease_s)))

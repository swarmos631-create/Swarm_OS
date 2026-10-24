"""Start a just-enqueued job according to the configured job mode."""

from __future__ import annotations

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from swarmos.config import get_settings
from swarmos.models import Job


def dispatch(background_tasks: BackgroundTasks, db: Session, job: Job | None) -> None:
    """In inline (serverless) mode run the job after the response is sent, in the same invocation.

    In worker mode nothing happens here: the worker claims the job from the queue. The commit makes the job row
    visible before the background task claims it, independent of when request-scoped dependencies close.
    """
    if job is None or job.status != "queued" or get_settings().job_mode != "inline":
        return
    from swarmos.jobs.worker import run_job_now

    db.commit()
    background_tasks.add_task(run_job_now, job.id)

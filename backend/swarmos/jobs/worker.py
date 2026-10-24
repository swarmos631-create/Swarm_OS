"""Background worker: claims jobs, runs handlers, schedules periodic data synchronisation.

Run embedded in the API process (default, SWARMOS_WORKER_EMBEDDED=true) or standalone:

    python -m swarmos.jobs.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from swarmos.config import get_settings
from swarmos.core.logging import configure_logging, correlation_id_var
from swarmos.db import session_scope, utcnow
from swarmos.jobs import queue

log = logging.getLogger("swarmos.worker")
Handler = Callable[[dict[str, Any], str], Awaitable[dict[str, Any] | None]]


async def _agent_task(payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    from swarmos.agents.orchestrator import Orchestrator

    status = await Orchestrator().execute(payload["task_id"])
    return {"task_status": status}


async def _sync(payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    from swarmos.integrations.sync import run_sync

    return await run_sync(payload["source_id"], payload.get("city_id"), force=bool(payload.get("force")))


async def _sync_city(payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    from swarmos.integrations.sync import sync_city

    return await sync_city(payload["city_id"], payload.get("sources"), force=bool(payload.get("force")))


async def _train(payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    from swarmos.ml.pipeline import train_model

    return await asyncio.to_thread(train_model, payload["model_name"], job_id=job_id, promote_if_better=bool(payload.get("promote_if_better")),
                                   actor_id=payload.get("actor_id"))


HANDLERS: dict[str, Handler] = {"agent_task": _agent_task, "sync": _sync, "sync_city": _sync_city, "train_model": _train}


class Worker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.settings = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident() % 10000}"
        self._stop = asyncio.Event()
        self._last_schedule: float = 0.0
        self.active = 0
        self.max_parallel = 3

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("worker_started", extra={"worker_id": self.worker_id})
        inflight: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                self._maybe_schedule(loop.time())
                with session_scope() as db:
                    queue.reap_expired(db)
                if len(inflight) < self.max_parallel:
                    with session_scope() as db:
                        job = queue.claim(db, self.worker_id, kinds=list(HANDLERS))
                        claimed = (job.id, job.kind, dict(job.payload), job.correlation_id) if job else None
                    if claimed:
                        t = asyncio.create_task(self._run_job(*claimed))
                        inflight.add(t)
                        t.add_done_callback(inflight.discard)
                        continue
            except Exception:
                log.exception("worker_loop_error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.worker_poll_seconds)
            except TimeoutError:
                pass
        if inflight:
            log.info("worker_draining", extra={"inflight": len(inflight)})
            await asyncio.wait(inflight, timeout=30)
        log.info("worker_stopped")

    async def _run_job(self, job_id: str, kind: str, payload: dict[str, Any], correlation_id: str | None) -> None:
        token = correlation_id_var.set(correlation_id or job_id)
        hb = asyncio.create_task(self._heartbeat(job_id))
        try:
            result = await HANDLERS[kind](payload, job_id)
            with session_scope() as db:
                queue.complete(db, job_id, result)
        except Exception as exc:
            log.exception("job_failed", extra={"job_id": job_id, "kind": kind})
            with session_scope() as db:
                queue.fail(db, job_id, f"{type(exc).__name__}: {exc}")
        finally:
            hb.cancel()
            correlation_id_var.reset(token)

    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(60)
            with session_scope() as db:
                queue.extend_lease(db, job_id)

    def _maybe_schedule(self, now_mono: float) -> None:
        s = self.settings
        if not s.sync_enabled or now_mono - self._last_schedule < 60:
            return
        self._last_schedule = now_mono
        bucket = int(utcnow().timestamp() // (s.sync_interval_minutes * 60))
        from swarmos.integrations.adapters import ADAPTERS

        with session_scope() as db:
            for city_id in s.sync_cities:
                for sid, a in ADAPTERS.items():
                    if a.spec.scope == "city" and a.configured(s)[0]:
                        queue.enqueue(db, "sync", {"source_id": sid, "city_id": city_id}, dedupe_key=f"sched:{sid}:{city_id}:{bucket}",
                                      priority=8, max_attempts=2)
            for sid, a in ADAPTERS.items():
                if a.spec.scope == "global" and a.configured(s)[0]:
                    queue.enqueue(db, "sync", {"source_id": sid}, dedupe_key=f"sched:{sid}:global:{bucket}", priority=8, max_attempts=2)


class EmbeddedWorker:
    """Runs the worker's event loop in a daemon thread inside the API process."""

    def __init__(self) -> None:
        self.worker = Worker()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        def target() -> None:
            self.loop = asyncio.new_event_loop()
            self.loop.run_until_complete(self.worker.run())

        self.thread = threading.Thread(target=target, name="swarmos-worker", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 35) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.worker.stop)
        if self.thread:
            self.thread.join(timeout)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = Worker()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stop)
        await worker.run()

    asyncio.run(runner())


if __name__ == "__main__":
    main()

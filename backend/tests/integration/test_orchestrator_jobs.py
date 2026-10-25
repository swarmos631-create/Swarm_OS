"""Orchestrator failure semantics and job queue behaviour."""

import asyncio
import dataclasses
import time

import pytest
from sqlalchemy import select

from swarmos.agents import orchestrator as orch_mod
from swarmos.agents import registry
from swarmos.agents.contracts import AgentResult, TransientAgentError
from swarmos.agents.orchestrator import Orchestrator
from swarmos.core.security import principal_from_user
from swarmos.db import session_scope
from swarmos.jobs import queue
from swarmos.models import AgentTask, Job, User
from swarmos.services.tasks import submit_task


def _submit(agent_ids):
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == "admin@swarmos.local"))
        t, _ = submit_task(db, principal_from_user(u), "bengaluru", "test", params={}, agent_ids=agent_ids, domains=None, idempotency_key=None)
        return t.id


def _executions(task_id):
    with session_scope() as db:
        t = db.get(AgentTask, task_id)
        return t.status, {e.agent_id: e for e in t.executions}, t.result


@pytest.fixture()
def patch_skill(monkeypatch):
    def apply(agent_id, fn, **overrides):
        spec = dataclasses.replace(registry.REGISTRY[agent_id], skill=fn, **overrides)
        monkeypatch.setitem(registry.REGISTRY, agent_id, spec)
        monkeypatch.setitem(orch_mod.REGISTRY, agent_id, spec)
    return apply


def test_failure_of_one_agent_never_yields_task_success(patch_skill):
    def boom(ctx):
        raise RuntimeError("sensor bus offline")

    patch_skill("traffic_flow_analyst", boom)
    tid = _submit(["traffic_flow_analyst", "mobility_optimization_agent", "urban_heat_analyst"])
    status = asyncio.run(Orchestrator().execute(tid))
    st, ex, result = _executions(tid)
    assert status == st and st in ("partial", "failed")
    assert ex["traffic_flow_analyst"].status == "failed" and "sensor bus offline" in ex["traffic_flow_analyst"].error
    # dependent agent still ran and honestly reported the missing input
    assert ex["mobility_optimization_agent"].status == "unavailable"
    assert result["counts"].get("failed") == 1


def test_timeout_is_recorded(patch_skill):
    def slow(ctx):
        time.sleep(1.5)
        return AgentResult(status="ok", summary="late", confidence=0.5)

    patch_skill("urban_heat_analyst", slow, timeout_s=0.3)
    tid = _submit(["urban_heat_analyst"])
    asyncio.run(Orchestrator().execute(tid))
    _, ex, _ = _executions(tid)
    assert ex["urban_heat_analyst"].status == "timed_out"


def test_transient_errors_are_retried(patch_skill):
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientAgentError("database is locked")
        return AgentResult(status="ok", summary="fine", confidence=0.6, provenance=["live"])

    patch_skill("urban_heat_analyst", flaky, max_retries=2)
    tid = _submit(["urban_heat_analyst"])
    asyncio.run(Orchestrator().execute(tid))
    _, ex, _ = _executions(tid)
    assert ex["urban_heat_analyst"].status == "succeeded" and ex["urban_heat_analyst"].attempts == 2


def test_output_contract_violation_is_failure(patch_skill):
    patch_skill("air_quality_analyst", lambda ctx: AgentResult(status="ok", summary="no metrics", confidence=0.9))
    tid = _submit(["air_quality_analyst"])
    asyncio.run(Orchestrator().execute(tid))
    _, ex, _ = _executions(tid)
    assert ex["air_quality_analyst"].status == "failed" and "contract" in ex["air_quality_analyst"].error


def test_rerun_is_idempotent():
    tid = _submit(["budget_analysis_agent"])
    first = asyncio.run(Orchestrator().execute(tid))
    second = asyncio.run(Orchestrator().execute(tid))
    assert first == second


def test_queue_dedupe_claim_retry_and_reap():
    with session_scope() as db:
        a = queue.enqueue(db, "noop", {"x": 1}, dedupe_key="dedupe-1", max_attempts=2)
        b = queue.enqueue(db, "noop", {"x": 2}, dedupe_key="dedupe-1")
        assert a.id == b.id
        job_id = a.id
    with session_scope() as db:
        j = queue.claim(db, "w1", kinds=["noop"])
        assert j and j.id == job_id and j.status == "running"
    with session_scope() as db:
        assert queue.claim(db, "w2", kinds=["noop"]) is None  # already claimed
        queue.fail(db, job_id, "boom")
    with session_scope() as db:
        j = db.get(Job, job_id)
        assert j.status == "queued" and j.attempts == 1  # retried with backoff
        from datetime import timedelta

        from swarmos.db import utcnow

        j.status, j.locked_until = "running", utcnow() - timedelta(seconds=1)
    with session_scope() as db:
        assert queue.reap_expired(db) >= 1
        assert db.get(Job, job_id).status == "queued"


def test_prebuild_cities_builds_only_cities_without_data():
    from sqlalchemy import func, select

    from swarmos.config import get_settings
    from swarmos.db import session_scope
    from swarmos.demo.seed import DEMO, prebuild_cities
    from swarmos.models import Measurement

    settings = get_settings().model_copy(update={"simulate_all_cities": True})
    assert prebuild_cities(settings, pause_s=0, city_ids=["kochi", "bengaluru"]) == 1  # bengaluru is already seeded
    with session_scope() as db:
        n = db.scalar(select(func.count(Measurement.id)).where(Measurement.city_id == "kochi", Measurement.source_id == DEMO))
    assert n and n > 1000
    assert prebuild_cities(settings, pause_s=0, city_ids=["kochi"]) == 0  # idempotent

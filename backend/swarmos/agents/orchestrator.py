"""DAG orchestrator for agent tasks.

* Independent agents run concurrently under a bounded semaphore; each runs in a worker thread with its own
  database session, a timeout, bounded retries for transient errors and cooperative cancellation.
* Every execution, input, output, error and agent-to-agent handoff is persisted.
* Outputs are validated against :class:`AgentResult`; a contract violation is recorded as a failure.
* A task is ``succeeded`` only if every agent succeeded; otherwise ``partial`` or ``failed``. Never a false success.
* Re-running a task (e.g. after a worker crash) reuses already-terminal executions (idempotent).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from swarmos.agents.chat import compose_answer, primary_agents
from swarmos.agents.contracts import AgentContext, AgentResult, Cancelled, TransientAgentError
from swarmos.agents.registry import REGISTRY
from swarmos.ai.runtime import ModelRuntime, get_runtime
from swarmos.config import get_settings
from swarmos.core.security import CLEARANCE
from swarmos.db import session_scope, utcnow
from swarmos.integrations.sync import city_source_status
from swarmos.models import AgentExecution, AgentHandoff, AgentTask, City, Notification, ProposedAction, User
from swarmos.services.data import CityData

log = logging.getLogger("swarmos.orchestrator")

TERMINAL_EXEC = {"succeeded", "degraded", "unavailable", "failed", "timed_out", "cancelled"}
RESULT_TO_EXEC = {"ok": "succeeded", "degraded": "degraded", "unavailable": "unavailable", "failed": "failed"}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _sanitize_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _run_skill_sync(task_id: str, agent_id: str, city_id: str, prompt: str, params: dict[str, Any],
                    upstream: dict[str, AgentResult], now: datetime, clearance: set[str], runtime: ModelRuntime,
                    cancel_event: threading.Event) -> AgentResult:
    spec = REGISTRY[agent_id]
    agent_params = spec.params.model_validate(params.get(agent_id, {}))
    with session_scope() as db:
        city = db.get(City, city_id)
        assert city is not None
        ctx = AgentContext(spec=spec, city=city, data=CityData(db, city, now), params=agent_params, prompt=prompt,
                           upstream=upstream, models=runtime, now=now, task_id=task_id, clearance=clearance,
                           cancel_event=cancel_event)
        ctx.check_cancelled()
        try:
            result = spec.skill(ctx)
        except OperationalError as exc:  # e.g. database locked — retryable
            raise TransientAgentError(str(exc)) from exc
    if not isinstance(result, AgentResult):
        raise TypeError(f"{agent_id} returned {type(result).__name__}, expected AgentResult")
    # Re-validate (catches in-place mutation after construction) and enforce declared output metrics.
    result = AgentResult.model_validate(result.model_dump())
    if result.status == "ok":
        missing = [m for m in spec.output_metrics if m not in result.metrics]
        if missing:
            raise ValueError(f"Output contract violation: missing metrics {missing}")
    return result


_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _agent_pool() -> ThreadPoolExecutor:
    """Threads reserved for agent skills. asyncio's default executor is small on 1-CPU hosts and is shared with
    model training, so a skill could wait there for a thread while its timeout was already running. Twice the
    agent concurrency leaves room for threads still finishing after a timeout."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=get_settings().agent_max_concurrency * 2, thread_name_prefix="agent")
        return _pool


class Orchestrator:
    def __init__(self, runtime: ModelRuntime | None = None) -> None:
        self.settings = get_settings()
        self.runtime = runtime or get_runtime()

    async def execute(self, task_id: str) -> str:
        with session_scope() as db:
            task = db.get(AgentTask, task_id)
            if task is None:
                raise ValueError(f"Unknown task {task_id}")
            if task.status in ("succeeded", "partial", "failed", "cancelled"):
                return task.status
            if task.cancel_requested:
                task.status, task.finished_at = "cancelled", utcnow()
                for e in task.executions:
                    if e.status not in TERMINAL_EXEC:
                        e.status = "cancelled"
                return "cancelled"
            plan = task.plan
            city_id, prompt, params = task.city_id, task.prompt, task.params or {}
            role = "viewer"
            if task.created_by:
                user = db.get(User, task.created_by)
                role = user.role if user else "viewer"
            task.status = "running"
            task.started_at = task.started_at or utcnow()
            results: dict[str, AgentResult] = {}
            for e in task.executions:
                if e.status in ("succeeded", "degraded", "unavailable", "failed") and e.output:
                    try:
                        results[e.agent_id] = AgentResult.model_validate(e.output)
                    except ValidationError:
                        e.status = "pending"
                elif e.status not in TERMINAL_EXEC or e.status in ("timed_out", "cancelled"):
                    e.status = "pending"
        now = utcnow()
        clearance = CLEARANCE[role]
        agents: list[str] = [a["id"] for a in plan["agents"]]
        deps: dict[str, list[str]] = {a["id"]: a["depends_on"] for a in plan["agents"]}
        finished: set[str] = set(results)
        sem = asyncio.Semaphore(self.settings.agent_max_concurrency)
        cancel_events: dict[str, threading.Event] = {}
        running: dict[str, asyncio.Task[None]] = {}
        cancelled = False

        async def run_one(agent_id: str) -> None:
            async with sem:
                await self._run_agent(task_id, agent_id, city_id, prompt, params, deps[agent_id], results, now,
                                      clearance, cancel_events.setdefault(agent_id, threading.Event()))

        last_cancel_check = 0.0
        while len(finished) < len(agents):
            if time.monotonic() - last_cancel_check > 0.3:
                last_cancel_check = time.monotonic()
                with session_scope() as db:
                    t = db.get(AgentTask, task_id)
                    cancelled = bool(t and t.cancel_requested)
                if cancelled:
                    for ev in cancel_events.values():
                        ev.set()
                    break
            for a in agents:
                if a not in finished and a not in running and all(d in finished for d in deps[a]):
                    running[a] = asyncio.create_task(run_one(a))
            if not running:
                raise RuntimeError("Scheduler stalled: unsatisfiable dependencies")
            done, _ = await asyncio.wait(running.values(), timeout=0.3, return_when=asyncio.FIRST_COMPLETED)
            for a, fut in list(running.items()):
                if fut in done:
                    exc = fut.exception()
                    if exc is not None:
                        log.error("agent_runner_crashed", extra={"agent": a, "error": _sanitize_error(exc)})
                        results[a] = AgentResult(status="failed", summary="Agent runner crashed", method="none",
                                                 limitations=[_sanitize_error(exc)])
                    finished.add(a)
                    del running[a]

        if cancelled and running:
            await asyncio.wait(running.values(), timeout=self.settings.agent_default_timeout_seconds)

        return self._finalise(task_id, agents, results, cancelled)

    async def _run_agent(self, task_id: str, agent_id: str, city_id: str, prompt: str, params: dict[str, Any],
                         deps: list[str], results: dict[str, AgentResult], now: datetime, clearance: set[str],
                         cancel_event: threading.Event) -> None:
        spec = REGISTRY[agent_id]
        timeout = spec.timeout_s or self.settings.agent_default_timeout_seconds
        upstream = {d: results[d] for d in deps if d in results}
        input_payload = {"params": params.get(agent_id, {}), "upstream": {d: r.status for d, r in upstream.items()}}
        with session_scope() as db:
            ex = db.scalar(select(AgentExecution).where(AgentExecution.task_id == task_id, AgentExecution.agent_id == agent_id))
            assert ex is not None
            ex.status, ex.started_at, ex.input, ex.error = "running", utcnow(), input_payload, None
            for d, r in upstream.items():
                db.add(AgentHandoff(task_id=task_id, from_agent=d, to_agent=agent_id,
                                    payload_digest=_digest(r.model_dump()), payload_status=r.status))
        t0 = time.monotonic()
        status, result, error, attempts = "failed", None, None, 0
        for attempt in range(spec.max_retries + 1):
            attempts = attempt + 1
            try:
                result = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        _agent_pool(), _run_skill_sync, task_id, agent_id, city_id, prompt, params, upstream, now,
                        clearance, self.runtime, cancel_event),
                    timeout=timeout,
                )
                status, error = RESULT_TO_EXEC[result.status], None
                break
            except TimeoutError:
                cancel_event.set()  # ask the worker thread to stop at its next checkpoint
                status, error = "timed_out", f"Exceeded {timeout:.0f}s timeout"
                break
            except Cancelled:
                status, error = "cancelled", "Cancelled by operator"
                break
            except TransientAgentError as exc:
                status, error = "failed", _sanitize_error(exc)
                await asyncio.sleep(0.2 * (2**attempt))
                continue
            except Exception as exc:  # contract violations and bugs are not retried
                log.exception("agent_failed", extra={"agent": agent_id, "task": task_id})
                status, error = "failed", _sanitize_error(exc)
                break
        if result is None:
            result = AgentResult(status="failed", summary=error or "failed", method="none", limitations=[error or ""])
        results[agent_id] = result
        with session_scope() as db:
            ex = db.scalar(select(AgentExecution).where(AgentExecution.task_id == task_id, AgentExecution.agent_id == agent_id))
            assert ex is not None
            ex.status, ex.error, ex.attempts = status, error, attempts
            ex.output = result.model_dump(mode="json") if status not in ("timed_out", "cancelled") else None
            ex.confidence = result.confidence
            ex.finished_at = utcnow()
            ex.duration_ms = int((time.monotonic() - t0) * 1000)

    def _finalise(self, task_id: str, agents: list[str], results: dict[str, AgentResult], cancelled: bool) -> str:
        with session_scope() as db:
            task = db.get(AgentTask, task_id)
            assert task is not None
            for e in task.executions:
                if e.status not in TERMINAL_EXEC:
                    e.status = "cancelled" if cancelled else "failed"
                    e.error = e.error or ("Cancelled before start" if cancelled else "Did not run")
            statuses = {e.agent_id: e.status for e in task.executions}
            counts: dict[str, int] = {}
            for s in statuses.values():
                counts[s] = counts.get(s, 0) + 1
            useful = counts.get("succeeded", 0) + counts.get("degraded", 0)
            if cancelled:
                final = "cancelled"
            elif counts.get("succeeded", 0) == len(agents):
                final = "succeeded"
            elif useful > 0:
                final = "partial"
            else:
                final = "failed"

            action_ids = [] if cancelled else self._persist_actions(db, task, results, statuses)
            ok_results = {a: r for a, r in results.items() if statuses.get(a) in ("succeeded", "degraded")}
            asked = primary_agents(task.plan)
            # A roll-up summary leads only when the question asked for an overview; otherwise lead with the agents
            # that matched the question (e.g. the weather agent for "what is the weather today?").
            summary_src = next((ok_results[a] for a in ("city_situation_summarizer", "public_safety_situation_summarizer")
                                if a in ok_results and a in asked), None)
            primary = [a for a in asked if a in ok_results] or [
                a for a in agents if a not in ("city_operations_planning_agent", "compliance_checklist_agent")]
            summary = summary_src.summary if summary_src else " ".join(
                ok_results[a].summary for a in primary if a in ok_results)[:3000] or "No agent produced a usable result."
            answer, suggestions = compose_answer(task.plan, results, statuses, task.prompt)
            confs = [r.confidence for r in ok_results.values() if r.confidence is not None]
            sev_rank = {"critical": 4, "high": 3, "moderate": 2, "low": 1, "info": 0}
            # Roll-up agents repeat their inputs' findings; keep each finding once, attributed to its originating agent.
            rollups = {"city_situation_summarizer", "public_safety_situation_summarizer"}
            seen_findings: set[tuple[str, str]] = set()
            unique = []
            for a, r in sorted(ok_results.items(), key=lambda kv: kv[0] in rollups):
                for f in r.findings:
                    key = (f.title, f.detail)
                    if key not in seen_findings:
                        seen_findings.add(key)
                        unique.append({"agent_id": a, **f.model_dump()})
            findings = sorted(unique, key=lambda f: -sev_rank[f["severity"]])[:20]
            freshness = [
                {"id": s["id"], "name": s["name"], "status": s["status"], "freshness": s["freshness"]}
                for s in city_source_status(db, task.city_id) if s["applies_to_city"] and s["scope"] != "none"
            ]
            task.result = {
                "status": final,
                "summary": summary,
                "answer": answer,
                "suggestions": suggestions,
                "counts": counts,
                "confidence": round(sum(confs) / len(confs), 2) if confs else None,
                "provenance": sorted(set().union(*(set(r.provenance) for r in ok_results.values()))) if ok_results else [],
                "limitations": sorted({lim for r in ok_results.values() for lim in r.limitations})[:30],
                "missing_data": sorted({m for r in results.values() for m in r.missing})[:30],
                "findings": findings,
                "proposed_action_ids": action_ids,
                "data_freshness": freshness,
                "execution_policy": "Agents only analyse and recommend. No action executes without human approval.",
            }
            task.status = final
            task.finished_at = utcnow()
            if final in ("partial", "failed"):
                task.error = f"{len(agents) - useful} of {len(agents)} agents did not produce a full result"
            if action_ids:
                db.add(Notification(city_id=task.city_id, level="info", title=f"{len(action_ids)} action(s) awaiting approval",
                                    body=task.prompt[:160], link=f"/workspace?task={task.id}"))
        log.info("task_finished", extra={"task": task_id, "status": final, "counts": counts})
        return final

    def _persist_actions(self, db: Any, task: AgentTask, results: dict[str, AgentResult], statuses: dict[str, str]) -> list[str]:
        reviews = {}
        comp = results.get("compliance_checklist_agent")
        if comp and statuses.get("compliance_checklist_agent") == "succeeded":
            reviews = {r["fingerprint"]: r for r in comp.metrics.get("reviews", [])}
        seen: set[str] = set()
        ids = []
        role_rank = {"operator": 2, "admin": 3}
        for agent_id, res in results.items():
            if statuses.get(agent_id) not in ("succeeded", "degraded"):
                continue
            for a in res.actions:
                fp = a.fingerprint()
                if fp in seen:
                    continue
                seen.add(fp)
                review = reviews.get(fp)
                required = a.required_role
                if review and role_rank.get(review["recommended_role"], 0) > role_rank[required]:
                    required = review["recommended_role"]  # compliance can only make approval stricter
                row = ProposedAction(
                    task_id=task.id, city_id=task.city_id, agent_id=agent_id, kind=a.kind, title=a.title,
                    rationale=a.rationale, risk=a.risk, required_role=required,
                    payload={**a.payload, "_evidence": {"provenance": res.provenance, "confidence": res.confidence,
                                                         "agent_status": res.status,
                                                         "demo_basis": bool({"demo", "synthetic"} & set(res.provenance))},
                             "_compliance": review},
                )
                db.add(row)
                db.flush()
                ids.append(row.id)
        return ids

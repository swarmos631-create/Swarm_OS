# Agents and orchestration

## Design

All 50 agents are declarative `AgentSpec`s in `backend/swarmos/agents/registry.py`, bound to **skills**
(domain functions in `agents/skills/*.py`) and run by one shared runtime. There are no per-agent classes.
`validate_registry()` runs at import time and enforces exactly 50 agents, 10 domains × 5, unique IDs and names,
known dependencies, and no cycles.

Each spec declares: `id`, `name`, `domain`, `description`, `capabilities`, `keywords` (used by the planner),
`skill`, `data` (categories it reads), `depends_on`, `aggregate` (depends on every other planned agent),
`params` (a Pydantic input schema), `output_metrics` (required keys when `status=ok`), `model` (a trained model it
uses, if any), `min_role`, `timeout_s`, `max_retries`, and `config`.

### Result contract (`AgentResult`)

```
status: ok | degraded | unavailable | failed
summary, findings[{title, detail, severity, location}], metrics{…}
actions[{kind: create_incident|escalate_incident|notify_operators|manual, title, rationale, payload, risk, required_role}]
confidence (required for ok/degraded, forbidden for unavailable), method, data_refs[], provenance[], limitations[], missing[]
```

Validation rules prevent false success. An `unavailable` result must list what is `missing`, cannot claim
confidence, and cannot propose actions. Any result that touches `demo`/`synthetic` data automatically gets a
limitation stating so.

## The 50 agents

| Domain | Agents (method) |
|---|---|
| Mobility | Traffic Flow (speed ratio) · Congestion Forecast (trained HGB forecaster, seasonal-naive fallback) · Public Transit (OSM grid coverage) · Road Incident Triage (priority score) · Mobility Optimization (playbook over upstream) |
| Environment | Air Quality (Indian NAQI 2014 breakpoints) · Weather Impact (IMD rainfall classes) · Flood Risk (transparent additive index) · Urban Heat (IMD absolute heatwave criterion) · Environmental Anomaly (robust z-score) |
| Water | Water Demand (trained forecaster) · Water Quality (BIS IS 10500) · Leak Detection (minimum night flow) · Drainage Risk (complaint hotspots + flood index) · Wastewater Ops (capacity + BOD limits) |
| Energy | Energy Demand (trained forecaster) · Grid Anomaly (IEGC band + Isolation Forest if promoted) · Renewable Optimizer (GHI integration) · Streetlight Ops (astronomical schedule) · Utility Outage Triage |
| Public safety | Emergency Triage (weighted priority) · Incident Correlation (spatio-temporal union-find) · Resource Coordinator (nearest OSM facilities) · Disaster Impact (USGS + weather hazards) · Safety Summarizer |
| Civic | Complaint Classification (trained text model / keyword rules) · Request Routing · Resolution Tracker (SLA) · Asset Maintenance · Service Quality |
| Planning | Land Use (OSM counts) · Infrastructure Health · Construction Impact · Urban Growth (needs multi-year LULC; unavailable by default) · Infrastructure Prioritization (MCDA) |
| Health & community | Aggregate Health Trends (aggregated counts only, small-cell suppression; unavailable by default) · Facility Capacity · Community Needs · Accessibility (OSM wheelchair tags) · Service Coverage (grid distance) |
| Governance | Policy Documents (BM25 retrieval, clearance-filtered) · Compliance Checklist · Budget Analysis · Operational Performance · Resource Allocation (greedy) |
| Cross-domain | City Situation Summarizer · Cross-Domain Correlation (Pearson) · Anomaly Investigation · Scenario Simulation (deterministic what-if) · City Operations Planning |

## Planner

`agents/planner.py` is deterministic and explainable. It matches the prompt against agent keywords (whole words
plus common inflections), or uses explicit `agent_ids` / `domains`. It then adds transitive dependencies, applies
the role filter server-side, caps the plan size while keeping dependency closure, and always appends City
Operations Planning and Compliance Checklist as the final stage. Every selection carries a human-readable reason.
The prompt is used only for keyword matching: text inside it cannot grant roles, add tools, or select
unregistered agents (covered by tests).

## Orchestrator

`agents/orchestrator.py`:

* Starts every agent whose dependencies have finished (in any status) under a semaphore. A dependent still runs
  when its input failed; it receives no upstream result and reports `unavailable` or `degraded` itself.
* Runs each skill in a worker thread with its **own DB session**, a timeout (`timed_out` status), retries for
  `TransientAgentError` (for example a locked DB), and a cancel event the skill checks at checkpoints.
* Validates output, enforces `output_metrics`, persists input, output, error, duration, attempts and confidence,
  and writes one `agent_handoffs` row per upstream input with the SHA-256 of the payload.
* Polls `cancel_requested`; agents that have not started become `cancelled`.
* Re-running a task reuses executions that already finished, which makes the run idempotent after a worker crash.
* Final status: `succeeded` (all agents ok), `partial` (some useful results), `failed` (none), or `cancelled`.
* Proposed actions are de-duplicated and persisted. The compliance review can only make the required approval role
  *stricter*, never looser.

## Approvals

`POST /cities/{id}/actions/{action}/decision` (operator+, and admin for actions that need it). Executors exist only
for internal, reversible operations: create an incident, change incident status, notify operators. Any other kind
becomes `awaiting_manual`: approved, but explicitly not executed, because SwarmOS has no connection to physical
infrastructure. Every decision is audited.

## Adding an agent

1. Write a skill `def my_skill(ctx: AgentContext) -> AgentResult` in the right `skills/` module. Read data only
   through `ctx.data` (`CityData`) and upstream results through `ctx.up(agent_id)`.
2. Return `unavailable(...)` when the required data is missing, and never invent values.
3. Add an `AgentSpec` to `AGENTS`. To keep the registry at 50, extend `validate_registry()` deliberately.
4. Add a unit test. `tests/integration/test_orchestrator_jobs.py` shows how to exercise failure modes.

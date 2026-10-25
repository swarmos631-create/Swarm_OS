# API additions: operator workflows and public access

All endpoints are under /api/v1. Requests use the existing JSON error format and city catalog. Do not put provider credentials in request bodies or frontend configuration.

## Recommendation review

* GET /cities/{city_id}/actions?status=pending&limit=50&offset=0
  * Any authenticated user with city access. Lists city recommendations newest first; optional status and standard pagination filters apply.
  * New recommendations include the originating agent's provenance, structured data references, limitations, confidence and demo-data flag under `payload._evidence`. These are bounded source summaries; inspect the source task for the full execution output and timestamps. Older recommendations may have been stored before this metadata was retained.
* GET /cities/{city_id}/actions/{action_id}/evidence
  * Any authenticated user with city access. Resolves bounded measurement, mapped-feature, citizen/service-request, incident and agent-execution IDs captured by the agent. Every row is checked against the requested city (shared mapped features are limited to the city's existing 800 km relevance radius). Returns source names or status, provenance, timestamps and limitations. Citizen request text, coordinates and ward, and incident narratives and precise coordinates, are omitted. Deleted or out-of-scope IDs are reported unresolved.
* POST /cities/{city_id}/actions/{action_id}/decision
  * Auth: operator or admin; city-scoped; CSRF required. Decisions are audited and may execute an internal action or mark a manual action for follow-up.

## Situation reports

* POST /cities/{city_id}/reports
  * Auth: operator or admin; city-scoped; CSRF required. Captures current dashboard records into a bounded durable snapshot and returns the snapshot.
* GET /cities/{city_id}/reports?limit=30&offset=0
  * Any authenticated user with city access. Returns the city's saved report history without full snapshots.
* GET /cities/{city_id}/reports/{report_id}
  * Any authenticated user with city access. Returns the saved snapshot; cross-city IDs return 404.

Snapshots retain the generated timestamp and reporting-window note, indicators, incidents, infrastructure, alerts, source health and sync failures, recent agent review/consensus summaries, cross-domain agent findings, saved scenario comparisons, recommendations and decision states, and operator-recorded intervention outcomes when present. Audit history and private intervention notes are excluded. The frontend supports Markdown download and browser print/Save as PDF. Data retains its existing provenance and availability labels; reports do not independently verify reports or infer causality.

## Intervention outcomes

* GET /cities/{city_id}/outcome-measurements?limit=30
  * Any authenticated user with city access. Lists recent observed measurements that are not marked demo or synthetic.
* PUT /cities/{city_id}/actions/{action_id}/outcome
  * Auth: operator or admin; action must be executed or awaiting manual execution; city-scoped; CSRF required.
  * Body: {"outcome":"improved|unchanged|worsened|unverified","note":"...","measurement_ids":[123]}.
  * Measurement references must belong to the city, be observations, and not be marked demo or synthetic. Re-recording updates the current operator report; each write is audited.
* GET /cities/{city_id}/actions/{action_id}/outcome
  * Any authenticated user with city access; cross-city action IDs return 404.

These are operator-reported follow-up outcomes. Linked measurements add time-stamped context only; this workflow does not prove causality or calculate intervention effectiveness.

## Forecast evaluation

* GET /cities/{city_id}/forecast-evaluation?days=90&window_hours=3
  * Any authenticated user with city access; days is 1–180 and valid-time tolerance is 1–12 hours.
  * Returns matched forecast/observation pairs, per-metric mean absolute error and mean bias, forecast issue time and lead time.

Only live/cached forecasts issued before the valid time are eligible, and observations must also be live/cached. Metric, entity and unit must match exactly; one forecast is paired at most once. Small or non-representative samples remain labeled and do not establish future performance.

## Cross-task impact review

* GET /cities/{city_id}/impact-review?days=30&limit_tasks=100
  * Any authenticated user with city access; days is 1–90 and task cap is 1–200.
  * Returns exact metric-path and normalized finding-title co-occurrences across separate successful/partial tasks and at least two agent domains, with task references, timestamps, provenance and source references.

This is a descriptive review aid. It does not average values across agents, infer causality, vote on truth or recommend interventions.

## Incident evidence

* GET /cities/{city_id}/incidents/{incident_id}/evidence/candidates?source_type=measurement|agent_execution|service_request|feature|proposed_action
  * Auth: operator or admin; city-scoped.
  * Returns at most 30 recent records of that type, excluding records already linked to this incident.
* POST /cities/{city_id}/incidents/{incident_id}/evidence
  * Auth: operator or admin; city-scoped; CSRF required.
  * Body: {"source_type":"measurement","source_id":"123","relationship":"supports|contradicts|context","note":"..."}.
  * Source references must exist in the same city (global OSM features are distance-scoped). Duplicate links return 409; unavailable or cross-city records return 404.
* DELETE /cities/{city_id}/incidents/{incident_id}/evidence/{evidence_id}
  * Auth: operator or admin; city-scoped; CSRF required; returns 204.
* GET /cities/{city_id}/incidents/{incident_id} now includes evidence[], resolved from the current source record. If the record has disappeared, the link remains visible with an unavailable-source caveat.

Evidence responses include label, summary, provenance, observed/fetched timestamps, freshness when available, limitations, and the operator's note. Service requests are reports, not independent verification. Proposed actions are not proof that work was carried out.

## Saved scenario runs

* POST /cities/{city_id}/scenarios
  * Auth: operator or admin; city-scoped; CSRF required; returns 202.
  * Body: {"name":"Monsoon stress test","extra_rainfall_mm":125,"temperature_delta_c":2.5}. Rainfall is bounded to 0..500 mm; temperature change to -10..10 °C.
  * Creates a scenario_runs snapshot linked to an agent_task. The existing durable worker executes the rule-based scenario_simulation_agent.
* GET /cities/{city_id}/scenarios?limit=50&offset=0
  * Any authenticated user with city access.
  * Returns saved inputs, task status, result metrics, source provenance and limitations.

The simulation perturbs available forecast or model inputs and compares against the embedded baseline. It is not a hydrological model or an independent forecast.

## Task disagreement review

GET /cities/{city_id}/tasks/{task_id} includes a consensus object. It compares only exact shared metric paths and exact normalized finding titles from succeeded/degraded execution records. Thresholds are a review heuristic. An empty overlap means insufficient_overlap, not agreement. Confidence values are never averaged.

## Public citizen intake and reference lookup

* POST /public/cities/{city_id}/service-requests
  * No login; rate-limited to 5 submissions per hashed client key per 10-minute shared-database window.
  * Body: {"text":"..."} (10–2000 characters). It does not collect a name, phone, email, or precise coordinate.
  * Returns an opaque random reference, current status, local category and timestamp; does not echo report text.
* GET /public/cities/{city_id}/service-requests/{reference}
  * No login; rate-limited to 30 requests/minute per hashed client key in the shared database.
  * Returns only reference, category and lifecycle status/timestamps. A reference is a bearer secret: anyone holding it can see this limited status.
* PATCH /cities/{city_id}/service-requests/{request_id}
  * Auth: operator or admin; city-scoped; CSRF required.
  * Body: {"status":"open|in_progress|resolved"}. Status changes enter the audit log.

Rate limits use atomic fixed-window PostgreSQL counters shared across API replicas. Caller keys are hashed before persistence, so raw IP addresses are not stored in the limiter table. Fixed windows can allow a short burst across a window boundary; use an edge/Redis sliding-window limiter if stricter burst control is required.

## SakhiRoute route alternatives

* POST /public/cities/{city_id}/routes/compare
  * No login; rate-limited to 3 requests/minute per hashed client key through the shared database, plus a best-effort one-request/second FOSSGIS limit per API process.
  * Body: {"origin":{"lat":12.97,"lon":77.59},"destination":{"lat":12.98,"lon":77.60},"mode":"foot|bike|car"}.
  * Both endpoints must be within 150 km of the selected city centre. Returns up to three real OSRM LineString route alternatives, estimated distance/time, nearby mapped incident/help-facility/transit-station counts, up to 25 nearest feature evidence references (source and external IDs, provenance, and timestamps), nearby mapped facility/station `wheelchair=*` counts (`yes`, `limited`, `no`, `designated`, `unknown`), attribution and caveats. Incident details remain aggregate-only. Wheelchair tags describe mapped features and do not validate route accessibility.
  * Coordinates are not persisted by SwarmOS. The routing provider may log coordinates. Do not send coordinates without the user's explicit action and disclosure.

The frontend draws returned alternatives as colored Mappls polylines when the optional map key is configured, or as MapLibre polylines over the keyless OpenFreeMap fallback; a schematic preview is used only if interactive maps fail. No route endpoint produces a safety score. Transit counts do not identify active services or schedules. A missing feature or zero incident count is not evidence of safety or absence.

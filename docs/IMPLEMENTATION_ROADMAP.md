# SwarmOS implementation roadmap

## Audit snapshot — 2026-10-26

The current branch is `codex/mission-control`, based on the previously audited `main` revision `660af2e`. The operator has since confirmed Railway is the sole deployment target. This roadmap review is local and read-only: browser initialization explicitly returned `Local browser use is disabled in Work settings`, and no Railway CLI is installed, so no deployment state or live service revision was independently verified in this pass.

### Architecture and reusable components

- React 19, TypeScript, Vite, TanStack Query and a PWA in `frontend/`; city-scoped routes and shared status/provenance components already exist.
- FastAPI, Pydantic, SQLAlchemy and Alembic in `backend/swarmos/`; SQLite is used in tests/development and PostgreSQL is supported for Railway.
- Existing durable job queue supports agent tasks, syncs and model training. Agent tasks persist plans, executions, handoffs, outputs, actions and cancellation state.
- The 50-agent registry includes scenario simulation, cross-domain correlation, anomaly investigation, complaint classification and operations planning.
- Authentication, CSRF protection, RBAC, city scoping, audit events, request rate limiting, structured errors and optimistic incident updates are already implemented.

### Feature status

| Capability | Status | Existing implementation / remaining gap |
|---|---|---|
| Source trust and freshness | Implemented locally; verify in live UI | `DataSource`, `SyncRun`, `Measurement`, adapter metadata and `Integrations` UI. Distinguishes model analyses/forecasts and demo records; city source health now reports the last successful sync separately from the latest attempt. Material readings show their observed and fetched times. |
| Mission Control | Implemented locally | City-scoped persisted run list, dependency graph, per-agent outputs, handoffs, recommendations and deterministic disagreement review. |
| Scenario Lab | Implemented locally | Saved weather stress runs are linked to durable agent tasks; each result includes the rule-based baseline and perturbed indices. Runs preserve inputs. |
| Incident evidence | Implemented locally | Audited links to city-scoped measurements, agent executions, service requests, mapped features and proposed actions; shows provenance, timestamps and limitations. |
| Explainable recommendations | Improved locally; partial | Mission Control includes a city-wide recent recommendation review, state filters, provenance, agent evidence references and limitations, linked to the source run. New proposals retain these summaries; older proposals may have partial metadata. References to retrieved policy documents are resolved only for documents visible to the current user's clearance and city scope; inaccessible document IDs and titles are omitted. Raw source records remain available in run details where the user's clearance allows them; references do not independently verify a recommendation. |
| Impact and learning | Improved locally; partial | Operators can record audited manual-execution time declarations and follow-up outcome notes, link non-demo observed measurements, and compare nearest linked observations around the declared boundary by metric, unit, entity and source. Forecast evaluation still matches prior live/cached forecasts to live/cached observations and reports historical error without guaranteeing future performance. Neither the operator declaration nor descriptive changes independently verify field execution or establish causality/benefit. |
| Situation reports | Implemented locally | Operator-created city snapshots are persisted with bounded dashboard sections, retained city-scoped history, provenance/source-health context, Markdown export and print-to-PDF. |
| Cross-domain impact | Implemented locally as a review aid | Analytics matches exact metric paths and normalized finding titles across distinct tasks and agent domains, showing provenance, source references and task timestamps. Results are co-occurrence only. |
| Citizen reporting portal | Implemented locally | Public text-only intake, local classification, private reference lookup with limited status, operator triage, and hashed shared-database rate limits. |
| Agent disagreement/consensus | Implemented locally as a review aid | Exact shared metric paths and exact matching finding titles are compared. No voting, confidence averaging or ground-truth claim. |
| SafeRoute | Implemented locally as a constrained MVP | Keyless FOSSGIS/OSRM alternatives rendered as map polylines on Mappls or the OpenFreeMap fallback, optional Mappls address search and click-to-set points, manual coordinate entry, user-selected provider/time/context/transit-wheelchair-tag ordering, nearby mapped incident/help/transit-station context, aggregate incident-origin breakdowns (including a clear demo/unverified-source distinction), community-mapped street-lamp points and optional OSM lighting tags from stored sync rows, capped source-row evidence, explicit OSM wheelchair-tag counts, segment-based distance checks against sampled route geometry, and visible warnings when the 2,000-incident/3,000-feature candidate scan is capped; no verified lamp status, safety score, route accessibility validation, route persistence or live traffic. |

### Real-data availability and limits

No-key live integrations already include Open-Meteo weather and CAMS modelled air quality, MET Norway fallback forecasts, USGS earthquakes, OpenStreetMap/Overpass and OpenFreeMap vector infrastructure data, NDMA SACHET alerts, and GDACS alerts. CPCB station observations need a free data.gov.in key. IMD access is by arrangement. TomTom traffic needs provider credentials/terms. Optional Mappls address search needs the existing domain-restricted browser key to have Place Search enabled. OSM completeness varies and is not proof that an unmapped feature is absent. Modelled weather and air quality are not station observations.

The product must keep these distinctions visible. Avoid adding providers whose published terms do not support the intended use; prefer official government feeds or documented open data, with bounded requests and attribution.

### Integration points and order

1. Find a permitted, current working-state lighting feed with route-relevant geometry and add audited accessibility checks. Mapped OSM street-lamp points are pole-location context only; they do not verify that lamps work or illuminate the road. An audit of the public [Delhi PWD street-light grievance listing](https://pwdsewa.pwddelhi.gov.in/HOME/Index?Recordtype=All&type=Street+Light) found citizen complaint entries, not an asset-status or repair-verification feed; its narratives and location clues are not scraped or used as route evidence. The current `wheelchair=*` display is mapped facility metadata, not route accessibility validation. Stored route context consists of points, not a sidewalk/footway network or audited curb-ramp, slope, width, surface, crossing, or step-free-continuity data, so route accessibility remains unassessed. Delhi Open Transit Data is not wired in: its static feed is dated June 2024, real-time data requires an account, and use requires the operator to state and obtain approval for the intended use. Validate a routing provider suitable for production scale and confirm Mappls Place Search is enabled on the deployed browser key. Existing transport station counts are community-mapped context only.
2. Continue review of aggregate-reference coverage for future agent result types. Current row-backed measurements, mapped features (including earthquake feature IDs), citizen/service requests, operational incidents, agent executions and retrieved documents carry bounded references; documents are clearance and city filtered, while citizen text/location and incident narratives/precise coordinates remain redacted in recommendation evidence. Some external-feed summary references identify only the provider and count because the source event is not persisted.
3. Validate Railway production data-source credentials, settings, migrations and runtime with the operator when read-only production access is enabled. Railway browser access is currently disabled in Work settings. Do not deploy without separate explicit authorization.

### Acceptance and validation

- City scope and role checks are enforced by the API, not only the frontend.
- Evidence points to persisted records and keeps the source's origin separate from freshness.
- Scenarios preserve inputs and comparable runs, and label simulated results as simulated.
- Consensus never treats vote count or uncalibrated confidence as ground truth.
- Public request lookups cannot enumerate records and never reveal staff-only notes.
- SafeRoute requires actual geometry from a permitted provider and presents limitations and uncertainty.
- Every metric includes its source, time basis and adequacy limitations. No unsupported values are filled in.

Current local validation (2026-10-26): backend command /private/tmp/swarmos-roadmap-venv/bin/python -m pytest -q -p no:cacheprovider backend/tests passed 110 tests (7 non-blocking dependency/runtime warnings); Alembic upgraded a disposable SQLite database to head. Frontend Vitest passed 10 tests, TypeScript, changed-file ESLint and production build passed. Full Playwright passed all 14 Chromium journeys, including manual execution, task approval, integrations and SafeRoute. Ruff and git diff --check passed for changed Python files. The document-access regression checks city and viewer/operator clearance, and redacts derived task results when restricted documents contribute. The Railway container/runtime, production PostgreSQL schema and live site remain unverified; browser access is disabled in Work settings. No deployment is being performed.

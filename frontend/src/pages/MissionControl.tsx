import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { Execution, InterventionOutcome, Page, ProposedAction, TaskDetail, TaskSummary } from "../api/types";
import { Dialog, EmptyState, ErrorState, LoadingBlock, Panel, ProvenanceBadge, StatusBadge } from "../components/ui";
import { ago, duration, fmtNum, fmtTime, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useSession } from "../state/session";
import { useOnline, useToast } from "../state/ui";

const ACTIVE = new Set(["queued", "running"]);
const FILTERS = ["all", "queued", "running", "succeeded", "partial", "failed", "cancelled"];
const EMPTY_TASKS: TaskSummary[] = [];

export default function MissionControlPage() {
  const { cityId, city } = useCity();
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const runs = useQuery({
    queryKey: ["mission-control", cityId],
    queryFn: () => api<Page<TaskSummary>>(`/cities/${cityId}/tasks`, { query: { limit: 50 } }),
    refetchInterval: 8_000,
  });
  const tasks = runs.data?.items ?? EMPTY_TASKS;
  const visible = useMemo(() => tasks.filter((task) =>
    (filter === "all" || task.status === filter) &&
    (!search || `${task.prompt} ${task.id}`.toLowerCase().includes(search.toLowerCase()))), [tasks, filter, search]);
  const counts = useMemo(() => tasks.reduce<Record<string, number>>((acc, task) => {
    acc[task.status] = (acc[task.status] ?? 0) + 1;
    return acc;
  }, {}), [tasks]);
  const activeCount = (counts.queued ?? 0) + (counts.running ?? 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Mission Control{city ? ` — ${city.name}` : ""}</h1>
          <p>Monitor persisted agent runs, their progress and outcomes for this city. Status refreshes every 8 seconds.</p>
        </div>
        <Link className="btn primary" to={`/c/${cityId}/workspace`}><Icon name="play" />Start an agent task</Link>
      </div>

      <div className="grid cols-4">
        <Metric label="Runs loaded" value={runs.isLoading ? "—" : tasks.length} note={`${runs.data?.total ?? 0} total city runs`} />
        <Metric label="In progress" value={runs.isLoading ? "—" : activeCount} note="Queued or running" />
        <Metric label="Completed" value={runs.isLoading ? "—" : counts.succeeded ?? 0} note="Succeeded in this view" />
        <Metric label="Needs attention" value={runs.isLoading ? "—" : (counts.failed ?? 0) + (counts.partial ?? 0)} note="Failed or partial in this view" />
      </div>

      <Panel title="Agent runs" icon="agents" actions={<span className="xs faint">Updated {runs.dataUpdatedAt ? ago(new Date(runs.dataUpdatedAt).toISOString()) : "—"}</span>} flush>
        <div className="row wrap panel-body" style={{ borderBottom: "1px solid var(--line)" }}>
          <label className="sr-only" htmlFor="mission-search">Search runs</label>
          <input id="mission-search" className="input" placeholder="Search task or ID…" value={search} onChange={(event) => setSearch(event.target.value)} style={{ maxWidth: 260 }} />
          <div className="row wrap" role="group" aria-label="Filter runs by status">
            {FILTERS.map((status) => <button key={status} type="button" className="chip" aria-pressed={filter === status} onClick={() => setFilter(status)}>
              {titleCase(status)}{status !== "all" && <span className="faint"> · {counts[status] ?? 0}</span>}
            </button>)}
          </div>
        </div>
        {runs.error ? <div className="panel-body"><ErrorState error={runs.error} onRetry={() => void runs.refetch()} /></div>
          : !runs.data ? <div className="panel-body"><LoadingBlock rows={5} /></div>
          : visible.length === 0 ? <EmptyState title={tasks.length ? "No matching runs" : "No agent runs yet"}>
            {tasks.length ? "Change the status filter or search text." : "Start a task from AI Assistant. Runs and their agent execution graph will appear here."}
          </EmptyState>
          : <div className="table-wrap"><table><thead><tr><th>Task</th><th>Status</th><th>Agents</th><th>Started</th><th>Result</th><th /></tr></thead>
            <tbody>{visible.map((task) => <tr key={task.id}>
              <td><button className="link" type="button" onClick={() => setSelected(task.id)} style={{ textAlign: "left", maxWidth: 480 }}>
                <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{task.prompt}</span>
                <span className="xs faint mono">{task.id.slice(0, 12)}</span>
              </button></td>
              <td><StatusBadge s={task.status} /></td>
              <td className="num">{task.agent_count}</td>
              <td title={fmtTime(task.created_at)}>{ago(task.created_at)}</td>
              <td className="small muted" style={{ maxWidth: 280 }}>{task.summary || task.error || "—"}</td>
              <td><button className="btn sm" type="button" onClick={() => setSelected(task.id)}>Inspect</button></td>
            </tr>)}</tbody></table></div>}
      </Panel>
      <RecommendationsReview cityId={cityId} onInspectTask={(taskId) => setSelected(taskId)} />
      {selected && <RunDetails cityId={cityId} taskId={selected} onClose={() => setSelected(null)} />}
    </>
  );
}

function Metric({ label, value, note }: { label: string; value: string | number; note: string }) {
  return <section className="panel panel-body"><div className="upper faint">{label}</div><div style={{ fontSize: 25, fontWeight: 650, margin: "5px 0" }}>{value}</div><div className="xs faint">{note}</div></section>;
}

function RecommendationsReview({ cityId, onInspectTask }: { cityId: string; onInspectTask: (taskId: string) => void }) {
  const [filter, setFilter] = useState("all");
  const q = useQuery({
    queryKey: ["recommendation-review", cityId, filter],
    queryFn: () => api<Page<ProposedAction>>(`/cities/${cityId}/actions`, { query: { limit: 50, ...(filter === "all" ? {} : { status: filter }) } }),
    refetchInterval: 15_000,
  });
  const pending = useQuery({
    queryKey: ["recommendation-review-count", cityId, "pending"],
    queryFn: () => api<Page<ProposedAction>>(`/cities/${cityId}/actions`, { query: { status: "pending", limit: 1 } }),
    refetchInterval: 30_000,
  });
  const actions = q.data?.items ?? [];
  return <Panel title={`City recommendation review · ${pending.data?.total ?? "…"} pending`} icon="shield">
    <div className="stack">
      <div className="row wrap" role="group" aria-label="Filter recommendations by state">
        {["all", "pending", "approved", "executed", "awaiting_manual", "rejected", "failed"].map((status) => <button key={status} className="chip" type="button" aria-pressed={filter === status} onClick={() => setFilter(status)}>
          {titleCase(status)}
        </button>)}
        <span className="xs faint">Latest {actions.length} of {q.data?.total ?? 0} matching recommendation(s)</span>
      </div>
      {q.error ? <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        : !q.data ? <LoadingBlock rows={3} />
        : actions.length === 0 ? <EmptyState title={filter === "all" ? "No recommendations yet" : "No recommendations in this state"}>
          {filter === "all" ? "Recommendations from completed agent runs will appear here with their evidence context." : "Choose another state to review recommendations."}
        </EmptyState>
        : <ul className="list">{actions.map((action) => {
          const evidence = action.payload._evidence as { provenance?: string[]; limitations?: string[]; data_refs?: Record<string, unknown>[]; confidence?: number; demo_basis?: boolean } | undefined;
          return <li key={action.id} style={{ flexDirection: "column", alignItems: "stretch" }}>
            <div className="row between wrap">
              <div style={{ minWidth: 0, flex: 1 }}><b>{action.title}</b><div className="xs faint">{action.agent_name} · {titleCase(action.kind)} · {titleCase(action.risk)} risk · {fmtTime(action.created_at)}</div></div>
              <StatusBadge s={action.status} />
            </div>
            <p className="small muted" style={{ margin: 0 }}>{action.rationale || "No rationale was recorded."}</p>
            <div className="row wrap xs">
              {(evidence?.provenance ?? []).map((source) => <ProvenanceBadge key={source} p={source} />)}
              {evidence?.demo_basis && <span className="badge b-demo">Synthetic basis</span>}
              {evidence?.confidence != null && <span className="faint">Agent confidence {Math.round(evidence.confidence * 100)}%</span>}
              {action.task_id && <button className="link xs" type="button" onClick={() => onInspectTask(action.task_id!)}>Inspect source run</button>}
            </div>
            <RecommendationEvidence cityId={cityId} action={action} refs={evidence?.data_refs ?? []} agentLimitations={evidence?.limitations ?? []} />
          </li>;
        })}</ul>}
    </div>
  </Panel>;
}

function RecommendationEvidence({ cityId, action, refs, agentLimitations }: {
  cityId: string; action: ProposedAction; refs: Record<string, unknown>[]; agentLimitations: string[];
}) {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["action-evidence", cityId, action.id],
    queryFn: () => api<{ references: (Record<string, unknown> & { records?: Record<string, unknown>[]; unresolved_ids?: Array<number | string> })[]; limitations: string[] }>(
      `/cities/${cityId}/actions/${action.id}/evidence`,
    ),
    enabled: open,
  });
  return <details onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="xs">Evidence and limitations ({refs.length} source reference(s))</summary>
    <div className="stack" style={{ paddingTop: 8 }}>
      {q.error ? <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        : q.isLoading ? <LoadingBlock rows={2} />
        : q.data?.references.some((reference) => (reference.records?.length ?? 0) > 0) ? <ul className="list">
          {q.data.references.flatMap((reference, referenceIndex) => (reference.records ?? []).map((record, recordIndex) => {
            const isMeasurement = typeof record.metric === "string";
            const isIncident = typeof record.severity === "string" && typeof record.category === "string";
            const isServiceRequest = typeof record.status === "string" && typeof record.category === "string" && !isIncident;
            const isExecution = typeof record.agent_id === "string";
            const isDocument = typeof record.classification === "string" && typeof record.title === "string";
            const title = isMeasurement
              ? `${record.metric}: ${String(record.value)} ${String(record.unit ?? "")}`
              : isServiceRequest ? `Service request · ${String(record.category)}`
                : isIncident ? `Incident · ${String(record.category)} · ${String(record.severity)}`
                  : isExecution ? `Agent execution · ${String(record.agent_id)}`
                    : isDocument ? `Policy document · ${String(record.title)}`
                      : String(record.name ?? `${record.layer ?? "Mapped feature"} · ${record.external_id ?? record.id}`);
            const sourceLabel = isServiceRequest ? "Redacted citizen/service request"
              : isIncident ? `Incident · ${String(record.status)}`
                : isExecution ? `Execution · ${String(record.status)}`
                  : isDocument ? `${String(record.classification)} · ${String(record.source ?? "Stored document")}`
                    : String(record.source_name ?? record.source_id);
            return <li key={`${String(record.id)}-${referenceIndex}-${recordIndex}`} style={{ alignItems: "flex-start" }}>
              <div className="stack" style={{ flex: 1 }}>
                <b className="small">{title}</b>
                <span className="xs muted">{sourceLabel} · {String(record.provenance)}{record.kind ? ` · ${String(record.kind)}` : ""}{record.entity ? ` · ${String(record.entity)}` : ""}{isServiceRequest ? ` · ${String(record.status)}${record.department ? ` · ${String(record.department)}` : ""}` : ""}</span>
                <span className="xs faint">Observed {fmtTime(typeof record.observed_at === "string" ? record.observed_at : null)} · fetched {fmtTime(typeof record.fetched_at === "string" ? record.fetched_at : null)} · record #{String(record.id)}</span>
                {!!(record.limitations as string[] | undefined)?.length && <span className="xs faint">Source limits: {(record.limitations as string[]).join(" · ")}</span>}
              </div>
              <ProvenanceBadge p={typeof record.provenance === "string" ? record.provenance : "unavailable"} />
            </li>;
          }))}
        </ul> : refs.length ? <span className="xs faint">No matching persisted rows could be resolved for these references (older runs may not contain row IDs).</span>
          : <span className="xs faint">The agent did not attach structured source references to this recommendation.</span>}
      {!!agentLimitations.length && <ul className="xs muted" style={{ margin: 0, paddingLeft: 18 }}>{agentLimitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>}
      {!!q.data?.limitations.length && <ul className="xs muted" style={{ margin: 0, paddingLeft: 18 }}>{q.data.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>}
      <span className="xs faint">Resolved evidence retains source provenance and timestamps. It does not independently verify the recommendation.</span>
    </div>
  </details>;
}

function RunDetails({ cityId, taskId, onClose }: { cityId: string; taskId: string; onClose: () => void }) {
  const [executionId, setExecutionId] = useState<string | null>(null);
  const { can } = useSession();
  const online = useOnline();
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["task", cityId, taskId],
    queryFn: () => api<TaskDetail>(`/cities/${cityId}/tasks/${taskId}`),
    refetchInterval: (query) => query.state.data && ACTIVE.has(query.state.data.status) ? 1_000 : false,
  });
  const task = q.data;
  const chosen = task?.executions.find((execution) => execution.agent_id === executionId);
  const completed = task?.executions.filter((execution) => !["pending", "running"].includes(execution.status)).length ?? 0;
  const names = Object.fromEntries((task?.executions ?? []).map((execution) => [execution.agent_id, execution.name]));
  const statuses = Object.fromEntries((task?.executions ?? []).map((execution) => [execution.agent_id, execution.status]));
  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) => api(`/cities/${cityId}/actions/${id}/decision`, { method: "POST", body: { approve } }),
    onSuccess: (_result, variables) => { toast("ok", variables.approve ? "Recommendation approved" : "Recommendation rejected"); void qc.invalidateQueries({ queryKey: ["task", cityId, taskId] }); void qc.invalidateQueries({ queryKey: ["mission-control", cityId] }); void qc.invalidateQueries({ queryKey: ["recommendation-review", cityId] }); void qc.invalidateQueries({ queryKey: ["recommendation-review-count", cityId] }); },
    onError: () => toast("err", "Recommendation decision could not be saved"),
  });
  return <Dialog title={`Run ${taskId.slice(0, 10)}`} onClose={onClose} wide>
    {q.error ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !task ? <LoadingBlock rows={5} /> : (
      <div className="stack-lg">
        <div className="row between wrap"><StatusBadge s={task.status} /><span className="small faint">Created {fmtTime(task.created_at)}{task.finished_at ? ` · finished ${fmtTime(task.finished_at)}` : ""}</span></div>
        <p style={{ margin: 0, fontWeight: 550 }}>{task.prompt}</p>
        <div>
          <div className="row between small"><span>{completed} of {task.executions.length} agents finished</span><span>{task.executions.length ? Math.round(completed / task.executions.length * 100) : 0}%</span></div>
          <div className="progress" style={{ marginTop: 6 }}><span style={{ width: `${task.executions.length ? completed / task.executions.length * 100 : 0}%` }} /></div>
        </div>
        <Panel title="Persisted dependency graph" icon="grid">
          <div className="dag" aria-label="Agent execution stages">
            {task.plan.stages.map((stage, index) => <div className="dag-stage" key={index}>
              <span className="upper">Stage {index + 1}</span>
              {stage.map((agentId) => <button key={agentId} type="button" className={`dag-node st-${statuses[agentId] ?? "pending"}`} onClick={() => setExecutionId(agentId)}>
                <div style={{ fontWeight: 550 }}>{names[agentId] ?? agentId}</div><div className="xs faint">{titleCase(statuses[agentId] ?? "pending")}</div>
              </button>)}
            </div>)}
          </div>
          {!task.executions.length && <div className="xs faint">No execution records were returned for this task.</div>}
        </Panel>
        {task.result && <Panel title="Run summary" icon="check">
          <div className="stack"><p style={{ margin: 0 }}>{task.result.answer ?? task.result.summary}</p>
            <div className="row wrap">{task.result.provenance.map((source) => <ProvenanceBadge key={source} p={source} />)}{Object.entries(task.result.counts).map(([status, count]) => <StatusBadge key={status} s={status} label={`${count} ${status}`} />)}</div>
            {task.result.limitations.length > 0 && <div className="small muted"><b>Limitations</b><ul>{task.result.limitations.slice(0, 6).map((limitation) => <li key={limitation}>{limitation}</li>)}</ul></div>}
            {task.result.missing_data.length > 0 && <div className="small muted"><b>Missing data:</b> {task.result.missing_data.join(", ")}</div>}
          </div>
        </Panel>}
        {task.consensus && <Panel title="Agent disagreement review" icon="analytics">
          <div className="stack">
            <div className="row wrap"><StatusBadge s={task.consensus.status} label={task.consensus.status === "differences_found" ? `${task.consensus.material_differences} material difference(s)` : task.consensus.status === "insufficient_overlap" ? "Insufficient shared signals" : "No material difference detected"} />
              <span className="badge b-records">{task.consensus.shared_signals} shared signal(s)</span></div>
            {task.consensus.comparisons.map((comparison, index) => <div key={`${comparison.kind}-${comparison.signal}-${index}`} className="small">
              <div className="row between wrap"><b>{titleCase(comparison.kind)}: {comparison.signal}</b>{comparison.material_difference && <StatusBadge s="degraded" label="Review difference" />}</div>
              <div className="xs muted">{comparison.values.map((value) => `${names[value.agent_id] ?? value.agent_id}: ${value.value ?? value.severity ?? value.title}`).join(" · ")}{comparison.spread != null ? ` · spread ${comparison.spread}` : ""}</div>
            </div>)}
            {task.consensus.limitations.map((limitation) => <span className="xs faint" key={limitation}>{limitation}</span>)}
          </div>
        </Panel>}
        {task.handoffs?.length ? <Panel title={`Agent handoffs (${task.handoffs.length})`} icon="integrations" flush>
          <ul className="list">{task.handoffs.map((handoff, index) => <li key={`${handoff.from}-${handoff.to}-${index}`} className="small">
            <span style={{ flex: 1 }}>{names[handoff.from] ?? handoff.from} <span className="faint">→</span> {names[handoff.to] ?? handoff.to}</span><StatusBadge s={handoff.status} /><span className="xs faint">{handoff.digest} · {ago(handoff.at)}</span>
          </li>)}</ul>
        </Panel> : null}
        {task.actions?.length ? <Panel title={`Explainable recommendations (${task.actions.length})`} icon="alert">
          <div className="stack-lg">{task.actions.map((action) => {
            const evidence = action.payload._evidence as { provenance?: string[]; limitations?: string[]; data_refs?: Record<string, unknown>[] } | undefined;
            return <section className="stack" key={action.id} style={{ borderBottom: "1px solid var(--line)", paddingBottom: 12 }}>
              <div className="row between wrap"><b>{action.title}</b><StatusBadge s={action.status} /><span className="badge b-records">{titleCase(action.risk)} risk · {action.required_role} approval</span></div>
              <span className="small muted">{action.rationale}</span>
              <div className="row wrap">{(evidence?.provenance ?? []).map((p) => <ProvenanceBadge key={p} p={p} />)}</div>
              {!!evidence?.limitations?.length && <span className="xs faint">Evidence limitations: {evidence.limitations.join(" · ")}</span>}
              {evidence?.data_refs?.length ? <details><summary className="xs">Evidence references ({evidence.data_refs.length})</summary><pre className="json">{JSON.stringify(evidence.data_refs, null, 2)}</pre></details> : null}
              {action.status === "executed" && action.execution_result?.method === "operator_reported_manual" && <div className="notice info">
                Operator-reported manual execution at {action.executed_at ? fmtTime(action.executed_at) : "time unavailable"}. SwarmOS did not perform or independently verify the field action.
                {typeof action.execution_result.note === "string" && <div className="small">{action.execution_result.note}</div>}
              </div>}
              {action.status === "awaiting_manual" && can("operator") && <ManualExecutionEditor cityId={cityId} taskId={taskId} actionId={action.id} />}
              {(action.status === "executed" || action.status === "awaiting_manual") && can("operator") &&
                <OutcomeEditor cityId={cityId} taskId={taskId} actionId={action.id} outcome={action.outcome ?? null} />}
              {action.status === "pending" && can("operator") && <div className="row">
                <button className="btn sm primary" disabled={!online || decide.isPending} onClick={() => decide.mutate({ id: action.id, approve: true })}>Approve &amp; execute</button>
                <button className="btn sm ghost" disabled={!online || decide.isPending} onClick={() => decide.mutate({ id: action.id, approve: false })}>Reject</button>
              </div>}
            </section>;
          })}</div>
        </Panel> : null}
        {chosen && <ExecutionDetail execution={chosen} onClose={() => setExecutionId(null)} />}
      </div>
    )}
  </Dialog>;
}

function OutcomeEditor({ cityId, taskId, actionId, outcome }: { cityId: string; taskId: string; actionId: string; outcome: InterventionOutcome | null }) {
  const [value, setValue] = useState<InterventionOutcome["outcome"]>(outcome?.outcome ?? "unverified");
  const [note, setNote] = useState(outcome?.note ?? "");
  const [measurementIds, setMeasurementIds] = useState<number[]>(outcome?.measurement_ids ?? []);
  const online = useOnline();
  const toast = useToast();
  const qc = useQueryClient();
  const measurements = useQuery({
    queryKey: ["outcome-measurements", cityId],
    queryFn: () => api<{ items: { id: number; metric: string; value: number; unit: string; provenance: string; observed_at: string }[] }>(`/cities/${cityId}/outcome-measurements`),
  });
  const save = useMutation({
    mutationFn: () => api<InterventionOutcome>(`/cities/${cityId}/actions/${actionId}/outcome`, {
      method: "PUT", body: { outcome: value, note, measurement_ids: measurementIds },
    }),
    onSuccess: () => { toast("ok", "Intervention outcome saved"); void qc.invalidateQueries({ queryKey: ["task", cityId, taskId] }); },
    onError: () => toast("err", "Outcome could not be saved"),
  });
  return <div className="stack" style={{ border: "1px solid var(--line)", borderRadius: 8, padding: 10 }}>
    <b className="small">Observed outcome</b>
    {outcome && <div className="stack"><div className="row wrap"><StatusBadge s={outcome.outcome} /><span className="xs faint">Recorded {fmtTime(outcome.recorded_at)}</span></div>
      <span className="small muted">{outcome.note}</span>
      {outcome.evidence.length > 0 && <div className="small">Linked observations: {outcome.evidence.map((item) => `${item.metric} · ${item.entity} ${item.value} ${item.unit} (${item.source_id}, ${item.provenance}, ${fmtTime(item.observed_at)})`).join(" · ")}</div>}
      {!!outcome.observed_changes.length && <div className="stack">
        <span className="small"><b>Descriptive before/after changes</b> · paired around {outcome.comparison_boundary ? fmtTime(outcome.comparison_boundary) : "recorded execution"}</span>
        <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
          {outcome.observed_changes.map((change) => <li key={`${change.metric}/${change.entity}/${change.source_id}`}>
            {change.metric} · {change.entity} · {change.source_id}: {fmtNum(change.before.value)} {change.unit} → {fmtNum(change.after.value)} {change.unit} ({change.absolute_change >= 0 ? "+" : ""}{fmtNum(change.absolute_change)} {change.unit}{change.relative_change_pct == null ? "" : `, ${change.relative_change_pct >= 0 ? "+" : ""}${fmtNum(change.relative_change_pct)}%`})
          </li>)}
        </ul>
        <span className="xs faint">{outcome.comparison_method}</span>
      </div>}
      {outcome.evidence.length > 0 && !outcome.observed_changes.length && <span className="xs faint">
        {outcome.change_comparison_status === "no_recorded_execution_time"
          ? "No before/after comparison: the action has no recorded execution timestamp."
          : "No matched observations on both sides of execution for the same metric, unit, entity and source."}
      </span>}
      <span className="xs faint">{outcome.interpretation}</span>
    </div>}
    <div className="row wrap">
      <label className="small">Result <select className="input" value={value} onChange={(event) => setValue(event.target.value as InterventionOutcome["outcome"])}>
        <option value="improved">Improved</option><option value="unchanged">No observed change</option><option value="worsened">Worsened</option><option value="unverified">Not yet verified</option>
      </select></label>
    </div>
    <label className="small">Operator note<textarea className="input" rows={2} maxLength={2000} value={note} onChange={(event) => setNote(event.target.value)} placeholder="What was observed after the intervention? State uncertainty and context." /></label>
    {measurements.data?.items.length ? <fieldset className="stack" style={{ border: "1px solid var(--line)", borderRadius: 8, padding: 10 }}>
      <legend className="small">Link observed measurements</legend>
      {measurements.data.items.slice(0, 12).map((item) => <label className="row small" key={item.id}>
        <input type="checkbox" checked={measurementIds.includes(item.id)} onChange={(event) => setMeasurementIds((ids) => event.target.checked ? [...ids, item.id].slice(-20) : ids.filter((id) => id !== item.id))} />
        <span>{item.metric}: {item.value} {item.unit} · {item.provenance} · {fmtTime(item.observed_at)}</span>
      </label>)}
    </fieldset> : <span className="xs faint">No non-demo observed measurements are available to link for this city.</span>}
    <div className="row between"><span className="xs faint">Outcome notes are operator-reported. Linked observations add context but do not prove causality.</span>
      <button className="btn sm primary" disabled={!online || save.isPending || note.trim().length < 12} onClick={() => save.mutate()}>{outcome ? "Update outcome" : "Save outcome"}</button>
    </div>
  </div>;
}

function ManualExecutionEditor({ cityId, taskId, actionId }: { cityId: string; taskId: string; actionId: string }) {
  const [executedAt, setExecutedAt] = useState("");
  const [note, setNote] = useState("");
  const online = useOnline();
  const toast = useToast();
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: () => api<ProposedAction>(`/cities/${cityId}/actions/${actionId}/manual-execution`, {
      method: "POST",
      body: { executed_at: executedAt ? new Date(executedAt).toISOString() : undefined, note },
    }),
    onSuccess: () => {
      toast("ok", "Manual execution recorded");
      void qc.invalidateQueries({ queryKey: ["task", cityId, taskId] });
      void qc.invalidateQueries({ queryKey: ["mission-control", cityId] });
      void qc.invalidateQueries({ queryKey: ["recommendation-review", cityId] });
    },
    onError: () => toast("err", "Manual execution could not be recorded"),
  });
  return <div className="stack" style={{ border: "1px solid var(--line)", borderRadius: 8, padding: 10 }}>
    <b className="small">Record manual execution</b>
    <span className="xs faint">This records an operator declaration only. SwarmOS does not contact or operate field systems. The time is used as the before/after measurement boundary.</span>
    <label className="small">Actual completion time (optional; blank uses current time)
      <input className="input" type="datetime-local" value={executedAt} onChange={(event) => setExecutedAt(event.target.value)} />
    </label>
    <label className="small">Execution note<textarea className="input" rows={2} maxLength={1000} value={note} onChange={(event) => setNote(event.target.value)} placeholder="What was completed, and how do you know?" /></label>
    <button className="btn sm primary" disabled={!online || save.isPending || note.trim().length < 12} onClick={() => save.mutate()}>
      {save.isPending ? "Recording…" : "Mark manually executed"}
    </button>
  </div>;
}

function ExecutionDetail({ execution, onClose }: { execution: Execution; onClose: () => void }) {
  return <Panel title={execution.name} icon="agents" actions={<button className="btn sm ghost" type="button" onClick={onClose}>Close</button>}>
    <div className="stack">
      <div className="row wrap"><StatusBadge s={execution.status} /><span className="badge b-records">{execution.attempts} attempt{execution.attempts === 1 ? "" : "s"}</span>
        {execution.duration_ms != null && <span className="badge b-info">{duration(execution.duration_ms / 1000)}</span>}{execution.confidence != null && <span className="badge b-info">Confidence {Math.round(execution.confidence * 100)}%</span>}</div>
      <div className="xs faint">{execution.started_at ? fmtTime(execution.started_at) : "Not started"}{execution.finished_at ? ` → ${fmtTime(execution.finished_at)}` : ""}</div>
      <div className="small"><b>Depends on:</b> {execution.depends_on.map((id) => id).join(", ") || "No upstream agents"}</div>
      {execution.reason && <div className="small muted"><b>Planned because:</b> {execution.reason}</div>}
      {execution.error && <div className="notice error" role="alert">{execution.error}</div>}
      {execution.output && <>
        <p style={{ margin: 0 }}>{execution.output.summary}</p>
        <div className="row wrap">{execution.output.provenance.map((source) => <ProvenanceBadge key={source} p={source} />)}</div>
        {!!execution.output.findings.length && (
          <ul className="list">
            {execution.output.findings.map((finding, index) => (
              <li key={`${finding.title}-${index}`} className="small">
                <StatusBadge s={finding.severity} />
                <span><b>{finding.title}</b>{finding.detail && <span className="muted"> · {finding.detail}</span>}</span>
              </li>
            ))}
          </ul>
        )}
        {!!execution.output.limitations.length && <div className="small muted"><b>Limitations</b><ul>{execution.output.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul></div>}
        {!!execution.output.missing.length && <div className="small muted"><b>Missing inputs:</b> {execution.output.missing.join(", ")}</div>}
      </>}
      <details><summary className="small">Recorded inputs and metrics</summary><pre className="json">{JSON.stringify({ input: execution.input, metrics: execution.output?.metrics ?? {} }, null, 2)}</pre></details>
    </div>
  </Panel>;
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, ApiError, newIdempotencyKey } from "../api/client";
import type { Execution, Page, Plan, ProposedAction, TaskDetail, TaskSummary } from "../api/types";
import { ConfirmDialog, Drawer, EmptyState, ErrorState, Field, LoadingBlock, Panel, ProvenanceBadge, SeverityBadge, StatusBadge } from "../components/ui";
import { ago, fmtTime, pct, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useSession } from "../state/session";
import { useOnline, useToast } from "../state/ui";

const EXAMPLES = [
  "Give me a city situation overview",
  "Is there flooding risk in the next 72 hours and which drains need attention?",
  "How is air quality and heat stress today?",
  "Check water quality, leaks and grid anomalies",
  "What if 80 mm of extra rain falls tomorrow?",
  "Which open incidents need dispatch and where are the nearest facilities?",
];
const ACTIVE = new Set(["queued", "running"]);

export default function WorkspacePage() {
  const { cityId, city } = useCity();
  const [params, setParams] = useSearchParams();
  const taskId = params.get("task");
  const online = useOnline();
  const toast = useToast();
  const qc = useQueryClient();
  const [prompt, setPrompt] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [rain, setRain] = useState(50);
  const [dtemp, setDtemp] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const planM = useMutation({
    mutationFn: () => api<Plan>(`/cities/${cityId}/tasks/plan`, { method: "POST", body: { prompt } }),
    onSuccess: (p) => { setPlan(p); setError(null); },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Planning failed"),
  });
  const runM = useMutation({
    mutationFn: () => {
      const params: Record<string, Record<string, unknown>> = {};
      if (plan?.agents.some((a) => a.id === "scenario_simulation_agent")) params.scenario_simulation_agent = { extra_rainfall_mm: rain, temperature_delta_c: dtemp };
      return api<TaskDetail>(`/cities/${cityId}/tasks`, { method: "POST", body: { prompt, params, idempotency_key: newIdempotencyKey() } });
    },
    onSuccess: (t) => {
      setParams({ task: t.id });
      setPlan(null);
      void qc.invalidateQueries({ queryKey: ["tasks", cityId] });
      toast("info", `Task submitted to ${t.agent_count} agents`);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Submission failed"),
  });

  const history = useQuery({ queryKey: ["tasks", cityId], queryFn: () => api<Page<TaskSummary>>(`/cities/${cityId}/tasks`, { query: { limit: 15 } }), refetchInterval: 10_000 });

  return (
    <>
      <div className="page-head">
        <div><h1>AI Workspace — {city?.name}</h1><p>Ask an operational question. SwarmOS plans which agents to run, executes them locally, and returns findings with provenance. Nothing is executed without your approval.</p></div>
      </div>
      <div className="grid cols-3">
        <div className="span-2 stack-lg">
          <Panel title="New task" icon="workspace">
            <div className="stack-lg">
              <Field label="Question or task" error={error}>
                <textarea className="textarea" value={prompt} onChange={(e) => { setPrompt(e.target.value); setPlan(null); }} maxLength={2000}
                  placeholder="e.g. Is there flooding risk in the next 72 hours?" data-testid="task-prompt" />
              </Field>
              <div className="row wrap">
                {EXAMPLES.map((ex) => <button key={ex} className="chip" onClick={() => { setPrompt(ex); setPlan(null); }}>{ex}</button>)}
              </div>
              <div className="row">
                <button className="btn" disabled={prompt.trim().length < 3 || planM.isPending} onClick={() => planM.mutate()} data-testid="preview-plan">
                  <Icon name="agents" />{planM.isPending ? "Planning…" : "Preview plan"}
                </button>
                <button className="btn primary" disabled={!plan || runM.isPending || !online} onClick={() => runM.mutate()} data-testid="run-task"
                  title={!plan ? "Preview the plan first" : !online ? "Unavailable offline" : undefined}>
                  <Icon name="play" />{runM.isPending ? "Submitting…" : "Run agents"}
                </button>
              </div>
            </div>
          </Panel>
          {plan && (
            <Panel title={`Proposed execution plan · ${plan.agents.length} agents`} icon="agents">
              <PlanGraph stages={plan.stages} names={Object.fromEntries(plan.agents.map((a) => [a.id, a.name]))} />
              <div className="table-wrap" style={{ marginTop: 10 }}>
                <table className="table"><thead><tr><th>Agent</th><th>Why selected</th><th>Depends on</th></tr></thead>
                  <tbody>{plan.agents.map((a) => <tr key={a.id}><td style={{ fontWeight: 500 }}>{a.name}</td><td className="small muted">{a.reason}</td><td className="xs faint">{a.depends_on.length ? `${a.depends_on.length} upstream` : "—"}</td></tr>)}</tbody>
                </table>
              </div>
              {Object.keys(plan.excluded).length > 0 && (
                <div className="notice warn" style={{ marginTop: 10 }}><Icon name="info" /><span>Excluded: {Object.entries(plan.excluded).map(([a, why]) => `${a} (${why})`).join("; ")}</span></div>
              )}
              {plan.agents.some((a) => a.id === "scenario_simulation_agent") && (
                <div className="grid cols-2" style={{ marginTop: 10 }}>
                  <Field label="Scenario: extra rainfall in 24 h (mm)"><input className="input" type="number" min={0} max={500} value={rain} onChange={(e) => setRain(Math.max(0, Math.min(500, Number(e.target.value))))} /></Field>
                  <Field label="Scenario: temperature change (°C)"><input className="input" type="number" min={-10} max={10} step={0.5} value={dtemp} onChange={(e) => setDtemp(Math.max(-10, Math.min(10, Number(e.target.value))))} /></Field>
                </div>
              )}
            </Panel>
          )}
          {taskId ? <TaskView taskId={taskId} /> : !plan && <Panel><EmptyState title="No task selected" icon="workspace">Write a question, preview the plan, then run it. Results, confidence, data freshness and proposed actions appear here.</EmptyState></Panel>}
        </div>
        <Panel title="History" icon="refresh" flush>
          {history.error ? <div className="panel-body"><ErrorState error={history.error} /></div> : !history.data ? <div className="panel-body"><LoadingBlock /></div> : (
            <ul className="list">
              {history.data.items.map((t) => (
                <li key={t.id} style={{ cursor: "pointer", background: t.id === taskId ? "rgba(34,211,238,0.06)" : undefined }} onClick={() => setParams({ task: t.id })}>
                  <StatusBadge s={t.status} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="small" style={{ fontWeight: 500 }}>{t.prompt}</div>
                    <div className="xs faint">{t.agent_count} agents · {ago(t.created_at)}</div>
                  </div>
                </li>
              ))}
              {!history.data.items.length && <li className="small faint">No tasks yet for this city.</li>}
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}

function PlanGraph({ stages, names, statuses, onPick }: { stages: string[][]; names: Record<string, string>; statuses?: Record<string, string>; onPick?: (id: string) => void }) {
  return (
    <div className="dag" role="list" aria-label="Agent dependency stages">
      {stages.map((stage, i) => (
        <div key={i} className="dag-stage" role="listitem">
          <span className="upper">Stage {i + 1}</span>
          {stage.map((a) => (
            <button key={a} className={`dag-node st-${statuses?.[a] ?? "pending"}`} onClick={() => onPick?.(a)} disabled={!onPick} title={statuses?.[a]}>
              <div style={{ fontWeight: 500 }}>{names[a] ?? a}</div>
              {statuses && <div className="xs faint">{titleCase(statuses[a] ?? "pending")}</div>}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

function TaskView({ taskId }: { taskId: string }) {
  const { cityId } = useCity();
  const online = useOnline();
  const toast = useToast();
  const qc = useQueryClient();
  const [pick, setPick] = useState<Execution | null>(null);
  const q = useQuery({
    queryKey: ["task", cityId, taskId],
    queryFn: () => api<TaskDetail>(`/cities/${cityId}/tasks/${taskId}`),
    refetchInterval: (query) => (query.state.data && ACTIVE.has(query.state.data.status) ? 1000 : false),
  });
  const cancel = useMutation({
    mutationFn: () => api(`/cities/${cityId}/tasks/${taskId}/cancel`, { method: "POST" }),
    onSuccess: () => { toast("info", "Cancellation requested"); void q.refetch(); },
    onError: (e) => toast("err", e instanceof ApiError ? e.message : "Cancel failed"),
  });
  const t = q.data;
  const terminal = t ? !ACTIVE.has(t.status) : false;
  useEffect(() => {
    if (terminal) void qc.invalidateQueries({ queryKey: ["tasks", cityId] });
  }, [terminal, qc, cityId]);
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  if (!t) return <Panel><LoadingBlock rows={6} /></Panel>;
  const done = t.executions.filter((e) => !["pending", "running"].includes(e.status)).length;
  const statuses = Object.fromEntries(t.executions.map((e) => [e.agent_id, e.status]));
  const names = Object.fromEntries(t.executions.map((e) => [e.agent_id, e.name]));
  const r = t.result;

  return (
    <>
      <Panel title={<>Task <span className="mono faint">{t.id.slice(0, 8)}</span></>} icon="agents"
        actions={<>
          <StatusBadge s={t.status} />
          {ACTIVE.has(t.status) && <button className="btn sm danger" disabled={cancel.isPending || t.cancel_requested || !online} onClick={() => cancel.mutate()}><Icon name="stop" />{t.cancel_requested ? "Cancelling…" : "Cancel"}</button>}
        </>}>
        <div className="stack-lg">
          <p style={{ fontWeight: 500 }}>“{t.prompt}”</p>
          <div>
            <div className="row between small"><span className="muted">{done} / {t.executions.length} agents finished</span><span className="faint">Submitted {fmtTime(t.created_at)}{t.finished_at ? ` · finished ${fmtTime(t.finished_at)}` : ""}</span></div>
            <div className="progress" style={{ marginTop: 6 }}><span style={{ width: `${(done / Math.max(1, t.executions.length)) * 100}%` }} /></div>
          </div>
          <PlanGraph stages={t.plan.stages} names={names} statuses={statuses} onPick={(a) => setPick(t.executions.find((e) => e.agent_id === a) ?? null)} />
        </div>
      </Panel>

      {r && (
        <Panel title="Consolidated result" icon="check" id="task-result">
          <div className="stack-lg">
            {t.status !== "succeeded" && t.status !== "cancelled" && (
              <div className="notice warn"><Icon name="alert" /><span><b>{titleCase(t.status)} result.</b> {t.error} Agents that were unavailable or failed are listed below — their domains are not covered by this answer.</span></div>
            )}
            <p style={{ fontSize: 15, lineHeight: 1.6 }} data-testid="task-summary">{r.summary}</p>
            <div className="row wrap">
              <span className="badge b-info">Confidence {pct(r.confidence)}</span>
              {r.provenance.map((p) => <ProvenanceBadge key={p} p={p} />)}
              {Object.entries(r.counts).map(([s, n]) => <StatusBadge key={s} s={s} label={`${n} ${s}`} />)}
            </div>
            {r.findings.length > 0 && (
              <div>
                <div className="upper faint" style={{ marginBottom: 6 }}>Key findings</div>
                <ul className="list" style={{ border: "1px solid var(--line)", borderRadius: 10 }}>
                  {r.findings.slice(0, 10).map((f, i) => (
                    <li key={i}><SeverityBadge s={f.severity} /><div style={{ flex: 1 }}><b className="small">{f.title}</b>{f.detail && <div className="xs muted">{f.detail}</div>}<div className="xs faint">{names[f.agent_id ?? ""] ?? f.agent_id}</div></div></li>
                  ))}
                </ul>
              </div>
            )}
            <div className="grid cols-2">
              <div>
                <div className="upper faint" style={{ marginBottom: 6 }}>Data freshness at run time</div>
                <ul className="list">{r.data_freshness.map((s) => <li key={s.id} className="small"><span style={{ flex: 1 }}>{s.name}</span><StatusBadge s={s.freshness?.state ?? "never"} /></li>)}</ul>
              </div>
              <div>
                <div className="upper faint" style={{ marginBottom: 6 }}>Limitations & missing data</div>
                <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                  {r.missing_data.map((m) => <li key={m}>Missing: {m}</li>)}
                  {r.limitations.slice(0, 8).map((l) => <li key={l}>{l}</li>)}
                </ul>
              </div>
            </div>
            <p className="xs faint">{r.execution_policy}</p>
          </div>
        </Panel>
      )}
      {t.actions && t.actions.length > 0 && <ActionsPanel actions={t.actions} onChanged={() => { void q.refetch(); void qc.invalidateQueries({ queryKey: ["incidents", cityId] }); }} />}
      {pick && <ExecutionDrawer ex={t.executions.find((e) => e.agent_id === pick.agent_id) ?? pick} onClose={() => setPick(null)} handoffs={t.handoffs ?? []} />}
    </>
  );
}

function ActionsPanel({ actions, onChanged }: { actions: ProposedAction[]; onChanged: () => void }) {
  const { cityId } = useCity();
  const { can, me } = useSession();
  const online = useOnline();
  const toast = useToast();
  const [confirm, setConfirm] = useState<{ a: ProposedAction; approve: boolean } | null>(null);
  const [note, setNote] = useState("");
  const m = useMutation({
    mutationFn: (v: { a: ProposedAction; approve: boolean }) => api<ProposedAction>(`/cities/${cityId}/actions/${v.a.id}/decision`, { method: "POST", body: { approve: v.approve, note } }),
    onSuccess: (a) => { toast("ok", `Action ${a.status.replace("_", " ")}`); setConfirm(null); setNote(""); onChanged(); },
    onError: (e) => toast("err", e instanceof ApiError ? e.message : "Decision failed"),
  });
  const pending = actions.filter((a) => a.status === "pending").length;
  return (
    <Panel title={`Proposed actions · ${pending} awaiting approval`} icon="shield" flush>
      <ul className="list">
        {actions.map((a) => {
          const ev = a.payload._evidence ?? {};
          const comp = a.payload._compliance;
          const allowed = can(a.required_role);
          return (
            <li key={a.id} style={{ flexDirection: "column", alignItems: "stretch" }}>
              <div className="row between wrap">
                <div style={{ flex: 1, minWidth: 0 }}>
                  <b>{a.title}</b>
                  <div className="xs faint">{a.agent_name} · {titleCase(a.kind)} · requires {a.required_role}</div>
                </div>
                <div className="row">
                  <span className={`badge ${a.risk === "high" ? "sev-critical" : a.risk === "moderate" ? "sev-moderate" : "sev-low"}`}>{a.risk} risk</span>
                  <StatusBadge s={a.status} />
                </div>
              </div>
              <p className="small muted">{a.rationale}</p>
              <div className="row wrap xs">
                {ev.demo_basis && <span className="badge b-demo">Based on synthetic demo data</span>}
                {ev.confidence != null && <span className="faint">Agent confidence {pct(ev.confidence)}</span>}
                {comp && <span className="faint">Compliance checklist {comp.passed}/{comp.total}{comp.passed < comp.total ? `: ${comp.checks.filter((c: any) => !c.passed).map((c: any) => c.note).join(" ")}` : ""}</span>}
              </div>
              {a.status === "pending" ? (
                <div className="row">
                  <button className="btn sm primary" disabled={!allowed || !online} title={!allowed ? `Requires ${a.required_role} (you are ${me?.role})` : undefined} onClick={() => setConfirm({ a, approve: true })}><Icon name="check" />Approve</button>
                  <button className="btn sm" disabled={!allowed || !online} onClick={() => setConfirm({ a, approve: false })}><Icon name="x" />Reject</button>
                </div>
              ) : (
                <div className="xs faint">Decided {fmtTime(a.decided_at)}{a.decision_note ? ` — “${a.decision_note}”` : ""}{a.execution_result ? ` · ${JSON.stringify(a.execution_result)}` : ""}</div>
              )}
            </li>
          );
        })}
      </ul>
      {confirm && (
        <ConfirmDialog title={confirm.approve ? "Approve action" : "Reject action"} confirmLabel={confirm.approve ? "Approve" : "Reject"} danger={!confirm.approve} busy={m.isPending}
          onClose={() => setConfirm(null)} onConfirm={() => m.mutate(confirm)}
          body={<>
            <p><b>{confirm.a.title}</b></p>
            <p className="small muted">{confirm.approve ? (["create_incident", "escalate_incident", "notify_operators"].includes(confirm.a.kind)
              ? "SwarmOS will perform this internal action immediately and record it in the audit log."
              : "This action has no automated executor. Approval records it for manual execution by the responsible department.") : "The recommendation will be closed without action."}</p>
            {confirm.a.payload._evidence?.demo_basis && <div className="notice warn"><Icon name="alert" />This recommendation is based on synthetic demo data.</div>}
          </>}>
          <Field label="Decision note (optional, audited)"><textarea className="textarea" value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} style={{ minHeight: 60 }} /></Field>
        </ConfirmDialog>
      )}
    </Panel>
  );
}

function ExecutionDrawer({ ex, onClose, handoffs }: { ex: Execution; onClose: () => void; handoffs: { from: string; to: string; digest: string; status: string }[] }) {
  const o = ex.output;
  return (
    <Drawer title={ex.name} onClose={onClose}>
      <div className="stack-lg">
        <div className="row wrap"><StatusBadge s={ex.status} />{o?.provenance.map((p) => <ProvenanceBadge key={p} p={p} />)}{o?.confidence != null && <span className="badge b-info">Confidence {pct(o.confidence)}</span>}</div>
        <dl className="kv">
          <dt>Why selected</dt><dd>{ex.reason}</dd>
          <dt>Method</dt><dd className="mono">{o?.method ?? "—"}</dd>
          <dt>Duration</dt><dd className="num">{ex.duration_ms != null ? `${ex.duration_ms} ms` : "—"} · {ex.attempts} attempt(s)</dd>
          <dt>Inputs from</dt><dd>{handoffs.filter((h) => h.to === ex.agent_id).map((h) => `${h.from} (${h.status}, ${h.digest})`).join(", ") || "none"}</dd>
        </dl>
        {ex.error && <div className="notice error"><Icon name="alert" />{ex.error}</div>}
        {o && <p>{o.summary}</p>}
        {o?.missing.length ? <div className="notice warn"><Icon name="info" />Missing: {o.missing.join("; ")}</div> : null}
        {o?.findings.length ? <ul className="list">{o.findings.map((f, i) => <li key={i}><SeverityBadge s={f.severity} /><div className="small"><b>{f.title}</b><div className="muted">{f.detail}</div></div></li>)}</ul> : null}
        {o?.limitations.length ? <div><div className="upper faint">Limitations</div><ul className="small muted" style={{ paddingLeft: 18 }}>{o.limitations.map((l) => <li key={l}>{l}</li>)}</ul></div> : null}
        {o?.data_refs.length ? <div><div className="upper faint" style={{ marginBottom: 4 }}>Supporting data references</div><pre className="json">{JSON.stringify(o.data_refs, null, 2)}</pre></div> : null}
        {o && <div><div className="upper faint" style={{ marginBottom: 4 }}>Structured metrics</div><pre className="json">{JSON.stringify(o.metrics, null, 2)}</pre></div>}
      </div>
    </Drawer>
  );
}

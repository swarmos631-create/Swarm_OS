import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, ApiError, newIdempotencyKey } from "../api/client";
import type { ChatReply, Execution, Page, ProposedAction, TaskDetail, TaskSummary } from "../api/types";
import { ConfirmDialog, Drawer, ErrorState, Field, LoadingBlock, Panel, ProvenanceBadge, SeverityBadge, StatusBadge } from "../components/ui";
import { ago, fmtTime, pct, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useSession } from "../state/session";
import { useOnline, useToast } from "../state/ui";

const EXAMPLES = [
  "Give me a city situation overview",
  "Will it rain in the next 24 hours?",
  "How is air quality and heat stress today?",
  "Are there any official disaster alerts?",
  "Which open incidents need dispatch and where are the nearest facilities?",
  "What if 80 mm of extra rain falls tomorrow?",
];
const ACTIVE = new Set(["queued", "running"]);

interface ChatMsg { id: string; role: "user" | "assistant"; text: string; taskId?: string; suggestions?: string[]; interpreted?: string; error?: boolean }

const chatKey = (cityId: string) => `swarmos.chat.${cityId}`;
function loadChat(cityId: string): ChatMsg[] {
  try {
    const raw = localStorage.getItem(chatKey(cityId));
    return raw ? (JSON.parse(raw) as ChatMsg[]).slice(-60) : [];
  } catch { return []; }
}
function saveChat(cityId: string, msgs: ChatMsg[]) {
  try { localStorage.setItem(chatKey(cityId), JSON.stringify(msgs.slice(-60))); } catch { /* storage unavailable */ }
}
const msgId = () => Math.random().toString(36).slice(2, 10);

export default function WorkspacePage() {
  const { cityId, city } = useCity();
  const [params, setParams] = useSearchParams();
  const taskId = params.get("task");
  const online = useOnline();
  const qc = useQueryClient();
  const [messages, setMessages] = useState<ChatMsg[]>(() => loadChat(cityId));
  const [input, setInput] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { setMessages(loadChat(cityId)); }, [cityId]);
  useEffect(() => { saveChat(cityId, messages); endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [messages, cityId]);

  const send = useMutation({
    mutationFn: (text: string) => {
      const history = messages.slice(-10).map((m) => ({ role: m.role, text: m.text.slice(0, 4000) }));
      return api<ChatReply>(`/cities/${cityId}/chat`, { method: "POST", body: { message: text, history, idempotency_key: newIdempotencyKey() } });
    },
    onSuccess: (r) => {
      setMessages((cur) => [...cur, r.type === "reply"
        ? { id: msgId(), role: "assistant", text: r.text, suggestions: r.suggestions }
        : { id: msgId(), role: "assistant", text: "", taskId: r.task.id, interpreted: r.interpreted_as }]);
      if (r.type === "task") void qc.invalidateQueries({ queryKey: ["tasks", cityId] });
    },
    onError: (e) => setMessages((cur) => [...cur, { id: msgId(), role: "assistant", error: true,
      text: e instanceof ApiError ? e.message : "Sorry — I couldn't reach the server. Please try again." }]),
  });

  const ask = (text: string) => {
    const t = text.trim();
    if (!t || send.isPending || !online) return;
    setMessages((cur) => [...cur, { id: msgId(), role: "user", text: t }]);
    setInput("");
    send.mutate(t);
  };
  const onAnswered = (id: string, text: string, suggestions: string[]) =>
    setMessages((cur) => cur.map((m) => (m.id === id && m.text !== text ? { ...m, text, suggestions } : m)));

  const history = useQuery({ queryKey: ["tasks", cityId], queryFn: () => api<Page<TaskSummary>>(`/cities/${cityId}/tasks`, { query: { limit: 15 } }), refetchInterval: 15_000 });

  return (
    <>
      <div className="page-head">
        <div><h1>AI Assistant — {city?.name}</h1><p>Ask about the city in plain language. SwarmOS picks the right specialist agents, runs them on the server and answers with the evidence. Nothing is executed without your approval.</p></div>
        {messages.length > 0 && <button className="btn ghost" onClick={() => { setMessages([]); setParams({}); }}><Icon name="refresh" />New chat</button>}
      </div>
      <div className="grid cols-3">
        <div className="span-2 stack-lg">
          <Panel title="Chat" icon="workspace" flush>
            <div className="chat" data-testid="chat">
              <div className="chat-log" aria-live="polite">
                {messages.length === 0 && (
                  <div className="chat-empty">
                    <p className="muted">Hi! I'm the SwarmOS assistant for {city?.name}. Try one of these, or type your own question:</p>
                    <div className="row wrap">{EXAMPLES.map((ex) => <button key={ex} className="chip" onClick={() => ask(ex)}>{ex}</button>)}</div>
                  </div>
                )}
                {messages.map((m) => (
                  <div key={m.id} className={`chat-msg ${m.role}${m.error ? " error" : ""}`}>
                    {m.role === "assistant" && <span className="chat-avatar" aria-hidden="true"><Icon name="agents" width={14} height={14} /></span>}
                    <div className="chat-bubble">
                      {m.taskId && !m.text
                        ? <TaskAnswer taskId={m.taskId} interpreted={m.interpreted} onDone={(text, sug) => onAnswered(m.id, text, sug)} />
                        : <div className="chat-text">{m.text}</div>}
                      {m.taskId && m.text && (
                        <button className="link xs" onClick={() => setParams({ task: m.taskId! })}>View agents, evidence and proposed actions</button>
                      )}
                      {m.suggestions && m.suggestions.length > 0 && m === messages[messages.length - 1] && (
                        <div className="row wrap" style={{ marginTop: 8 }}>{m.suggestions.map((sg) => <button key={sg} className="chip" onClick={() => ask(sg)}>{sg}</button>)}</div>
                      )}
                    </div>
                  </div>
                ))}
                {send.isPending && <div className="chat-msg assistant"><span className="chat-avatar"><Icon name="agents" width={14} height={14} /></span><div className="chat-bubble"><span className="typing"><i /><i /><i /></span></div></div>}
                <div ref={endRef} />
              </div>
              <form className="chat-input" onSubmit={(e) => { e.preventDefault(); ask(input); }}>
                <label className="sr-only" htmlFor="chat-box">Message</label>
                <textarea id="chat-box" className="textarea" rows={1} value={input} maxLength={2000} data-testid="task-prompt"
                  placeholder={online ? `Ask about ${city?.name ?? "the city"}… (Enter to send, Shift+Enter for a new line)` : "Offline — reconnect to ask questions"}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input); } }} />
                <button className="btn primary" type="submit" disabled={!input.trim() || send.isPending || !online} data-testid="run-task"><Icon name="play" />Send</button>
              </form>
            </div>
          </Panel>
          {taskId && <TaskView taskId={taskId} />}
        </div>
        <Panel title="Recent questions" icon="refresh" flush>
          {history.error ? <div className="panel-body"><ErrorState error={history.error} /></div> : !history.data ? <div className="panel-body"><LoadingBlock /></div> : (
            <ul className="list">
              {history.data.items.map((t) => (
                <li key={t.id} style={{ cursor: "pointer", background: t.id === taskId ? "var(--accent-soft)" : undefined }} onClick={() => setParams({ task: t.id })}>
                  <StatusBadge s={t.status} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="small" style={{ fontWeight: 500 }}>{t.prompt}</div>
                    <div className="xs faint">{t.agent_count} agents · {ago(t.created_at)}</div>
                  </div>
                </li>
              ))}
              {!history.data.items.length && <li className="small faint">No questions yet for this city.</li>}
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}

/** Polls an agent run and shows progress, then the conversational answer (reported back so the chat keeps it). */
function TaskAnswer({ taskId, interpreted, onDone }: { taskId: string; interpreted?: string; onDone: (text: string, suggestions: string[]) => void }) {
  const { cityId } = useCity();
  const q = useQuery({
    queryKey: ["task", cityId, taskId],
    queryFn: () => api<TaskDetail>(`/cities/${cityId}/tasks/${taskId}`),
    refetchInterval: (query) => (query.state.data && ACTIVE.has(query.state.data.status) ? 1000 : false),
  });
  const t = q.data;
  const finished = t && !ACTIVE.has(t.status);
  useEffect(() => {
    if (finished && t) {
      const text = t.result?.answer ?? t.result?.summary ?? (t.status === "cancelled" ? "The run was cancelled." : t.error ?? "The agents could not produce an answer.");
      onDone(text, t.result?.suggestions ?? []);
    }
  }, [finished, t, onDone]);
  if (q.error) return <div className="chat-text">Couldn't load the result: {q.error instanceof ApiError ? q.error.message : "network error"}</div>;
  const done = t ? t.executions.filter((e) => !["pending", "running"].includes(e.status)).length : 0;
  const total = t?.executions.length ?? 0;
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="small muted">
        {interpreted ? <>Checking: “{interpreted}”</> : "Working on it…"}
      </div>
      <div className="xs faint">Running {total || "…"} agent{total === 1 ? "" : "s"}: {done} / {total || "?"} done</div>
      <div className="progress"><span style={{ width: `${total ? (done / total) * 100 : 8}%` }} /></div>
    </div>
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
            <p style={{ fontSize: 15, lineHeight: 1.6, whiteSpace: "pre-line" }} data-testid="task-summary">{r.answer ?? r.summary}</p>
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

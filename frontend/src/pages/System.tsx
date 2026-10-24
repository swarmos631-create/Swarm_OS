import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError } from "../api/client";
import type { Page, RuntimeProvider } from "../api/types";
import { Dialog, EmptyState, ErrorState, Field, LoadingBlock, Panel, StatusBadge, Tabs } from "../components/ui";
import { ago, fmtTime } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCities } from "../state/city";
import { useSession } from "../state/session";
import { useOnline, useToast } from "../state/ui";

interface Status {
  environment: string; database: string; demo_mode: boolean;
  worker: { embedded: boolean; job_counts: Record<string, number> };
  jobs: { id: string; kind: string; status: string; attempts: number; created_at: string; finished_at: string | null; error: string | null }[];
  model_runtime: { providers: RuntimeProvider[] };
  limits: Record<string, number>;
  sync: { enabled: boolean; interval_minutes: number; cities: string[] };
}

export default function SystemPage() {
  const { can } = useSession();
  const [tab, setTab] = useState<"status" | "audit" | "users">("status");
  if (!can("operator")) return <EmptyState title="Operator access required" icon="shield">System status is available to operators and administrators.</EmptyState>;
  return (
    <>
      <div className="page-head"><div><h1>System</h1><p>Health, background jobs, audit trail and accounts.</p></div></div>
      <Tabs label="System sections" value={tab} onChange={setTab} tabs={[{ id: "status", label: "Status & jobs" }, ...(can("admin") ? [{ id: "audit" as const, label: "Audit log" }, { id: "users" as const, label: "Users" }] : [])]} />
      {tab === "status" && <StatusTab />}
      {tab === "audit" && <AuditTab />}
      {tab === "users" && <UsersTab />}
    </>
  );
}

function StatusTab() {
  const q = useQuery({ queryKey: ["system"], queryFn: () => api<Status>("/system/status"), refetchInterval: 10_000 });
  const health = useQuery({ queryKey: ["ready"], queryFn: async () => (await fetch("/api/health/ready")).json() as Promise<{ status: string; checks: Record<string, string>; version: string }>, refetchInterval: 15_000 });
  if (q.error) return <ErrorState error={q.error} />;
  if (!q.data) return <LoadingBlock rows={6} />;
  const s = q.data;
  return (
    <>
      <div className="grid cols-4">
        <Panel title="Readiness">{health.data ? <div className="stack"><StatusBadge s={health.data.status === "ready" ? "healthy" : "failed"} label={health.data.status} />{Object.entries(health.data.checks).map(([k, v]) => <div key={k} className="row between small"><span className="muted">{k}</span><span>{v}</span></div>)}<span className="xs faint">v{health.data.version}</span></div> : <LoadingBlock />}</Panel>
        <Panel title="Environment"><dl className="kv" style={{ gridTemplateColumns: "100px 1fr" }}><dt>Mode</dt><dd>{s.environment}</dd><dt>Database</dt><dd>{s.database}</dd><dt>Demo</dt><dd>{s.demo_mode ? "enabled" : "disabled"}</dd><dt>Worker</dt><dd>{s.worker.embedded ? "embedded" : "external"}</dd></dl></Panel>
        <Panel title="Job queue"><div className="row wrap">{Object.entries(s.worker.job_counts).map(([k, v]) => <StatusBadge key={k} s={k} label={`${v} ${k}`} />)}</div></Panel>
        <Panel title="Limits & sync"><dl className="kv" style={{ gridTemplateColumns: "150px 1fr" }}>{Object.entries(s.limits).map(([k, v]) => <><dt key={k}>{k.replace(/_/g, " ")}</dt><dd className="num">{v}</dd></>)}<dt>auto-sync</dt><dd>{s.sync.enabled ? `every ${s.sync.interval_minutes} min: ${s.sync.cities.join(", ")}` : "off"}</dd></dl></Panel>
      </div>
      <Panel title="Recent background jobs" flush>
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Kind</th><th>Status</th><th>Attempts</th><th>Created</th><th>Finished</th><th>Error</th></tr></thead>
          <tbody>{s.jobs.map((j) => <tr key={j.id}><td className="mono small">{j.kind}</td><td><StatusBadge s={j.status} /></td><td className="num">{j.attempts}</td><td className="small faint">{ago(j.created_at)}</td><td className="small faint">{fmtTime(j.finished_at)}</td><td className="xs" style={{ color: "var(--red)", maxWidth: 320 }}>{j.error}</td></tr>)}</tbody>
        </table></div>
      </Panel>
    </>
  );
}

interface Audit { id: number; at: string; actor_email: string | null; action: string; target_type: string | null; target_id: string | null; city_id: string | null; outcome: string; details: Record<string, unknown>; ip: string | null; correlation_id: string | null }

function AuditTab() {
  const [offset, setOffset] = useState(0);
  const [action, setAction] = useState("");
  const q = useQuery({ queryKey: ["audit", offset, action], queryFn: () => api<Page<Audit>>("/system/audit", { query: { limit: 50, offset, action } }) });
  return (
    <Panel title="Audit log" flush actions={<input className="input" style={{ width: 200 }} placeholder="Filter action (e.g. auth.login)" value={action} onChange={(e) => { setAction(e.target.value); setOffset(0); }} aria-label="Filter audit action" />}>
      {q.error ? <div className="panel-body"><ErrorState error={q.error} /></div> : !q.data ? <div className="panel-body"><LoadingBlock /></div> : (
        <>
          <div className="table-wrap"><table className="table">
            <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>City</th><th>Outcome</th><th>Ref</th></tr></thead>
            <tbody>{q.data.items.map((a) => <tr key={a.id}><td className="small">{fmtTime(a.at)}</td><td className="small">{a.actor_email ?? "system"}</td><td className="mono small">{a.action}</td><td className="xs muted">{a.target_type} {a.target_id?.slice(0, 12)}</td><td className="small">{a.city_id ?? "—"}</td><td><StatusBadge s={a.outcome === "success" ? "succeeded" : "failed"} label={a.outcome} /></td><td className="mono xs faint">{a.correlation_id}</td></tr>)}</tbody>
          </table></div>
          <div className="row between" style={{ padding: 12 }}><span className="small faint">{q.data.total} records</span><div className="row"><button className="btn sm" disabled={!offset} onClick={() => setOffset(offset - 50)}>Previous</button><button className="btn sm" disabled={offset + 50 >= q.data.total} onClick={() => setOffset(offset + 50)}>Next</button></div></div>
        </>
      )}
    </Panel>
  );
}

function UsersTab() {
  const q = useQuery({ queryKey: ["users"], queryFn: () => api<{ items: { id: string; email: string; display_name: string; role: string; all_cities: boolean; city_ids: string[]; is_active: boolean; last_login_at: string | null }[] }>("/system/users") });
  const [creating, setCreating] = useState(false);
  return (
    <Panel title="Accounts" flush actions={<button className="btn sm primary" onClick={() => setCreating(true)}><Icon name="plus" />New user</button>}>
      {q.error ? <div className="panel-body"><ErrorState error={q.error} /></div> : !q.data ? <div className="panel-body"><LoadingBlock /></div> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>User</th><th>Role</th><th>City scope</th><th>Last sign-in</th></tr></thead>
          <tbody>{q.data.items.map((u) => <tr key={u.id}><td>{u.display_name}<div className="xs faint">{u.email}</div></td><td><span className="badge b-records">{u.role}</span></td><td className="small">{u.all_cities ? "All cities" : u.city_ids.join(", ") || "None"}</td><td className="small faint">{ago(u.last_login_at)}</td></tr>)}</tbody>
        </table></div>
      )}
      {creating && <CreateUser onClose={() => setCreating(false)} />}
    </Panel>
  );
}

function CreateUser({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const online = useOnline();
  const cities = useCities();
  const [f, setF] = useState({ email: "", display_name: "", role: "viewer", password: "", all_cities: false, city_ids: [] as string[] });
  const [err, setErr] = useState<string | null>(null);
  const m = useMutation({
    mutationFn: () => api("/system/users", { method: "POST", body: f }),
    onSuccess: () => { toast("ok", "User created"); void qc.invalidateQueries({ queryKey: ["users"] }); onClose(); },
    onError: (e) => setErr(e instanceof ApiError ? (Array.isArray(e.details) ? (e.details as any[]).map((d) => d.msg).join("; ") : e.message) : "Failed"),
  });
  const valid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f.email) && f.display_name.length >= 2 && f.password.length >= 12;
  return (
    <Dialog title="Create user" onClose={onClose} footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!valid || m.isPending || !online} onClick={() => m.mutate()}>Create</button></>}>
      <div className="stack-lg">
        <Field label="Email"><input className="input" type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
        <Field label="Display name"><input className="input" value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} /></Field>
        <Field label="Role"><select className="select" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}><option value="viewer">Viewer</option><option value="operator">Operator</option><option value="admin">Admin</option></select></Field>
        <Field label="Initial password" hint="At least 12 characters with upper- and lower-case letters and a digit."><input className="input" type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
        <label className="row small"><input type="checkbox" checked={f.all_cities} onChange={(e) => setF({ ...f, all_cities: e.target.checked })} />Access to all cities</label>
        {!f.all_cities && (
          <Field label="Cities">
            <select className="select" multiple style={{ height: 120, backgroundImage: "none" }} value={f.city_ids} onChange={(e) => setF({ ...f, city_ids: Array.from(e.target.selectedOptions).map((o) => o.value) })}>
              {(cities.data?.items ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </Field>
        )}
        {err && <div className="notice error"><Icon name="alert" />{err}</div>}
      </div>
    </Dialog>
  );
}

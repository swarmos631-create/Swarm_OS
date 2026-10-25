import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError } from "../api/client";
import type { Incident, IncidentEvidence, Page } from "../api/types";
import { Dialog, Drawer, EmptyState, ErrorState, Field, FreshnessBadge, LoadingBlock, Panel, ProvenanceBadge, SeverityBadge, StatusBadge, Tabs } from "../components/ui";
import { ago, fmtTime, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useSession } from "../state/session";
import { useOnline, useToast } from "../state/ui";

const CATEGORIES = ["road", "mobility", "flooding", "drainage", "fire", "medical", "structural", "energy", "water", "utility", "garbage", "construction", "hazmat", "general"];
const STATUSES = ["open", "acknowledged", "in_progress", "resolved", "closed"];
const TRANSITIONS: Record<string, string[]> = {
  open: ["acknowledged", "in_progress", "resolved", "closed"], acknowledged: ["in_progress", "resolved", "closed"],
  in_progress: ["acknowledged", "resolved", "closed"], resolved: ["closed", "open"], closed: ["open"],
};
const PAGE = 25;

export default function IncidentsPage() {
  const [tab, setTab] = useState<"incidents" | "requests">("incidents");
  return (
    <>
      <div className="page-head">
        <div><h1>Incidents & service requests</h1><p>City-scoped records. Every change is versioned and audited.</p></div>
      </div>
      <Tabs label="Record type" value={tab} onChange={setTab} tabs={[{ id: "incidents", label: "Incidents" }, { id: "requests", label: "Citizen service requests" }]} />
      {tab === "incidents" ? <IncidentList /> : <RequestList />}
    </>
  );
}

function IncidentList() {
  const { cityId } = useCity();
  const { can } = useSession();
  const online = useOnline();
  const [status, setStatus] = useState<string[]>(["open", "acknowledged", "in_progress"]);
  const [severity, setSeverity] = useState("");
  const [category, setCategory] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"-reported_at" | "reported_at" | "severity">("-reported_at");
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["incidents", cityId, status.join(","), severity, category, q, sort, offset],
    queryFn: () => api<Page<Incident>>(`/cities/${cityId}/incidents`, { query: { status: status.join(","), severity, category, q, sort, limit: PAGE, offset } }),
    placeholderData: keepPreviousData,
  });

  const toggleStatus = (s: string) => { setOffset(0); setStatus((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s])); };

  return (
    <Panel title={`Incidents${list.data ? ` (${list.data.total})` : ""}`} icon="incidents" flush
      actions={can("operator") && <button className="btn sm primary" onClick={() => setCreating(true)} disabled={!online} data-testid="new-incident"><Icon name="plus" />Report incident</button>}>
      <div className="row wrap" style={{ padding: 12, gap: 8, borderBottom: "1px solid var(--line)" }}>
        {STATUSES.map((s) => <button key={s} className="chip" aria-pressed={status.includes(s)} onClick={() => toggleStatus(s)}>{titleCase(s)}</button>)}
        <select className="select" style={{ width: 150 }} value={severity} onChange={(e) => { setSeverity(e.target.value); setOffset(0); }} aria-label="Filter by severity">
          <option value="">All severities</option>{["critical", "high", "moderate", "low"].map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
        </select>
        <select className="select" style={{ width: 160 }} value={category} onChange={(e) => { setCategory(e.target.value); setOffset(0); }} aria-label="Filter by category">
          <option value="">All categories</option>{CATEGORIES.map((c) => <option key={c} value={c}>{titleCase(c)}</option>)}
        </select>
        <input className="input" style={{ width: 200 }} placeholder="Search title…" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} aria-label="Search incidents" />
        <select className="select" style={{ width: 170, marginLeft: "auto" }} value={sort} onChange={(e) => setSort(e.target.value as typeof sort)} aria-label="Sort">
          <option value="-reported_at">Newest first</option><option value="reported_at">Oldest first</option><option value="severity">Severity</option>
        </select>
      </div>
      {list.error ? <div className="panel-body"><ErrorState error={list.error} onRetry={() => void list.refetch()} /></div> : !list.data ? <div className="panel-body"><LoadingBlock rows={6} /></div> :
        list.data.items.length === 0 ? <EmptyState title="No incidents match these filters">Adjust the filters or report a new incident.</EmptyState> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Severity</th><th>Title</th><th>Category</th><th>Status</th><th>Reported</th><th>Source</th></tr></thead>
              <tbody>
                {list.data.items.map((i) => (
                  <tr key={i.id} className="clickable" onClick={() => setOpenId(i.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpenId(i.id)}>
                    <td><SeverityBadge s={i.severity} /></td>
                    <td style={{ fontWeight: 500 }}>{i.title}</td>
                    <td className="muted">{titleCase(i.category)}</td>
                    <td><StatusBadge s={i.status} /></td>
                    <td className="muted small" title={fmtTime(i.reported_at)}>{ago(i.reported_at)}</td>
                    <td><ProvenanceBadge p={i.provenance} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      {list.data && list.data.total > PAGE && (
        <div className="row between" style={{ padding: 12 }}>
          <span className="small faint">{offset + 1}–{Math.min(offset + PAGE, list.data.total)} of {list.data.total}</span>
          <div className="row">
            <button className="btn sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
            <button className="btn sm" disabled={offset + PAGE >= list.data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </div>
        </div>
      )}
      {creating && <CreateIncident onClose={() => setCreating(false)} />}
      {openId && <IncidentDrawer id={openId} onClose={() => setOpenId(null)} />}
    </Panel>
  );
}

function CreateIncident({ onClose }: { onClose: () => void }) {
  const { cityId, city } = useCity();
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ title: "", description: "", category: "road", severity: "moderate", lat: "", lon: "", department: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const m = useMutation({
    mutationFn: () => api<Incident>(`/cities/${cityId}/incidents`, {
      method: "POST",
      body: { ...form, lat: form.lat ? Number(form.lat) : null, lon: form.lon ? Number(form.lon) : null, department: form.department || null },
    }),
    onSuccess: (inc) => {
      toast("ok", `Incident "${inc.title}" created`);
      void qc.invalidateQueries({ queryKey: ["incidents", cityId] });
      void qc.invalidateQueries({ queryKey: ["dashboard", cityId] });
      onClose();
    },
    onError: (e) => setErrors({ form: e instanceof ApiError ? e.message : "Could not create incident" }),
  });
  const validate = () => {
    const er: Record<string, string> = {};
    if (form.title.trim().length < 3) er.title = "Title must be at least 3 characters";
    if ((form.lat && !form.lon) || (!form.lat && form.lon)) er.lat = "Provide both latitude and longitude, or neither";
    if (form.lat && (Number.isNaN(Number(form.lat)) || Math.abs(Number(form.lat)) > 90)) er.lat = "Latitude must be between -90 and 90";
    if (form.lon && (Number.isNaN(Number(form.lon)) || Math.abs(Number(form.lon)) > 180)) er.lon = "Longitude must be between -180 and 180";
    setErrors(er);
    return !Object.keys(er).length;
  };
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setForm({ ...form, [k]: e.target.value });
  return (
    <Dialog title={`Report incident — ${city?.name}`} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={m.isPending} onClick={() => validate() && m.mutate()}>{m.isPending ? "Saving…" : "Create incident"}</button>
    </>}>
      <div className="stack-lg">
        <Field label="Title" error={errors.title}><input className="input" value={form.title} onChange={set("title")} maxLength={200} /></Field>
        <Field label="Description"><textarea className="textarea" value={form.description} onChange={set("description")} maxLength={4000} /></Field>
        <div className="grid cols-2">
          <Field label="Category"><select className="select" value={form.category} onChange={set("category")}>{CATEGORIES.map((c) => <option key={c} value={c}>{titleCase(c)}</option>)}</select></Field>
          <Field label="Severity"><select className="select" value={form.severity} onChange={set("severity")}>{["low", "moderate", "high", "critical"].map((c) => <option key={c} value={c}>{titleCase(c)}</option>)}</select></Field>
          <Field label="Latitude" error={errors.lat} hint={city ? `City centre ${city.lat}` : undefined}><input className="input" inputMode="decimal" value={form.lat} onChange={set("lat")} /></Field>
          <Field label="Longitude" error={errors.lon} hint={city ? `City centre ${city.lon}` : undefined}><input className="input" inputMode="decimal" value={form.lon} onChange={set("lon")} /></Field>
        </div>
        <Field label="Responsible department (optional)"><input className="input" value={form.department} onChange={set("department")} maxLength={80} /></Field>
        {errors.form && <div className="notice error" role="alert"><Icon name="alert" />{errors.form}</div>}
      </div>
    </Dialog>
  );
}

function IncidentDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const { cityId } = useCity();
  const { can } = useSession();
  const online = useOnline();
  const qc = useQueryClient();
  const toast = useToast();
  const [note, setNote] = useState("");
  const [linking, setLinking] = useState(false);
  const q = useQuery({ queryKey: ["incident", cityId, id], queryFn: () => api<Incident>(`/cities/${cityId}/incidents/${id}`) });
  const m = useMutation({
    mutationFn: (body: Record<string, unknown>) => api<Incident>(`/cities/${cityId}/incidents/${id}`, { method: "PATCH", body: { ...body, version: q.data?.version } }),
    onSuccess: () => {
      toast("ok", "Incident updated");
      setNote("");
      void qc.invalidateQueries({ queryKey: ["incident", cityId, id] });
      void qc.invalidateQueries({ queryKey: ["incidents", cityId] });
      void qc.invalidateQueries({ queryKey: ["dashboard", cityId] });
    },
    onError: (e) => {
      toast("err", e instanceof ApiError ? e.message : "Update failed");
      if (e instanceof ApiError && e.status === 409) void q.refetch();
    },
  });
  const inc = q.data;
  return (
    <Drawer title="Incident" onClose={onClose}>
      {q.error ? <ErrorState error={q.error} /> : !inc ? <LoadingBlock rows={6} /> : (
        <div className="stack-lg">
          <div>
            <h2 style={{ fontSize: 18 }}>{inc.title}</h2>
            <div className="row wrap" style={{ marginTop: 8 }}><SeverityBadge s={inc.severity} /><StatusBadge s={inc.status} /><ProvenanceBadge p={inc.provenance} /></div>
          </div>
          {inc.description && <p className="muted">{inc.description}</p>}
          <dl className="kv">
            <dt>Category</dt><dd>{titleCase(inc.category)}</dd>
            <dt>Department</dt><dd>{inc.department ?? "Unassigned"}</dd>
            <dt>Reported</dt><dd>{fmtTime(inc.reported_at)}</dd>
            <dt>Source</dt><dd>{inc.source}</dd>
            <dt>Location</dt><dd className="num">{inc.lat != null ? `${inc.lat.toFixed(5)}, ${inc.lon?.toFixed(5)}` : "Not provided"}</dd>
            <dt>Version</dt><dd className="num">{inc.version}</dd>
          </dl>
          {can("operator") ? (
            <div className="stack">
              <div className="upper faint">Change status</div>
              <div className="row wrap">
                {(TRANSITIONS[inc.status] ?? []).map((s) => (
                  <button key={s} className="btn sm" disabled={!online || m.isPending} onClick={() => m.mutate({ status: s, note: note || undefined })}>{titleCase(s)}</button>
                ))}
              </div>
              <Field label="Note (optional)"><textarea className="textarea" value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} style={{ minHeight: 60 }} /></Field>
              <div className="row">
                <select className="select" style={{ width: 160 }} value={inc.severity} disabled={!online || m.isPending} onChange={(e) => m.mutate({ severity: e.target.value })} aria-label="Change severity">
                  {["low", "moderate", "high", "critical"].map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
                </select>
                <button className="btn sm" disabled={!note || !online || m.isPending} onClick={() => m.mutate({ note })}>Add note</button>
              </div>
            </div>
          ) : <div className="notice info"><Icon name="info" />Viewer accounts cannot modify incidents.</div>}
          <Panel title={`Evidence (${inc.evidence?.length ?? 0})`} icon="database" actions={can("operator") && <button className="btn sm" disabled={!online} onClick={() => setLinking((value) => !value)}><Icon name="plus" />Link evidence</button>}>
            {inc.evidence?.length ? <ul className="list">
              {inc.evidence.map((evidence) => <EvidenceCard key={evidence.id} evidence={evidence} cityId={cityId} incidentId={id} canEdit={can("operator")} onChanged={() => { void qc.invalidateQueries({ queryKey: ["incident", cityId, id] }); void qc.invalidateQueries({ queryKey: ["incidents", cityId] }); }} />)}
            </ul> : <p className="small faint" style={{ margin: 0 }}>No evidence has been linked yet. Missing records are not treated as proof that an incident did not occur.</p>}
          </Panel>
          {linking && can("operator") && <LinkEvidence cityId={cityId} incidentId={id} onClose={() => setLinking(false)} onLinked={() => {
            setLinking(false);
            toast("ok", "Evidence linked to incident");
            void qc.invalidateQueries({ queryKey: ["incident", cityId, id] });
            void qc.invalidateQueries({ queryKey: ["incidents", cityId] });
          }} />}
          <div>
            <div className="upper faint" style={{ marginBottom: 6 }}>Timeline</div>
            <ul className="list">
              {(inc.events ?? []).map((e, i) => (
                <li key={i}><span className="badge b-records">{titleCase(e.kind)}</span>
                  <div className="small" style={{ flex: 1 }}>{e.from_status ? `${titleCase(e.from_status)} → ${titleCase(e.to_status ?? "")}` : e.to_status ? titleCase(e.to_status) : ""} {e.note && <span className="muted">— {e.note}</span>}
                    <div className="xs faint">{fmtTime(e.created_at)}</div></div></li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </Drawer>
  );
}

const EVIDENCE_TYPES = [
  ["measurement", "Measurements"], ["agent_execution", "Agent findings"], ["service_request", "Service requests"],
  ["feature", "Mapped features"], ["proposed_action", "Proposed actions"],
] as const;
type EvidenceType = typeof EVIDENCE_TYPES[number][0];

type EvidenceOption = Omit<IncidentEvidence, "id" | "relationship" | "note" | "created_at">;

function LinkEvidence({ cityId, incidentId, onClose, onLinked }: { cityId: string; incidentId: string; onClose: () => void; onLinked: () => void }) {
  const [sourceType, setSourceType] = useState<EvidenceType>("agent_execution");
  const [sourceId, setSourceId] = useState("");
  const [relationship, setRelationship] = useState<"supports" | "contradicts" | "context">("context");
  const [note, setNote] = useState("");
  const toast = useToast();
  const candidates = useQuery({
    queryKey: ["incident-evidence-candidates", cityId, incidentId, sourceType],
    queryFn: () => api<{ items: EvidenceOption[] }>(`/cities/${cityId}/incidents/${incidentId}/evidence/candidates`, { query: { source_type: sourceType } }),
  });
  const add = useMutation({
    mutationFn: () => api<IncidentEvidence>(`/cities/${cityId}/incidents/${incidentId}/evidence`, {
      method: "POST", body: { source_type: sourceType, source_id: sourceId, relationship, note: note.trim() },
    }),
    onSuccess: onLinked,
    onError: (error) => toast("err", error instanceof ApiError ? error.message : "Evidence could not be linked"),
  });
  return <Dialog title="Link existing evidence" onClose={onClose} footer={<>
    <button className="btn ghost" type="button" onClick={onClose}>Cancel</button>
    <button className="btn primary" type="button" disabled={!sourceId || note.trim().length < 6 || add.isPending} onClick={() => add.mutate()}>{add.isPending ? "Linking…" : "Link evidence"}</button>
  </>}>
    <div className="stack-lg">
      <p className="small muted" style={{ margin: 0 }}>Links point to existing city records. They preserve the record’s provenance and are not an independent verification.</p>
      <Field label="Evidence record type"><select className="select" value={sourceType} onChange={(event) => { setSourceType(event.target.value as EvidenceType); setSourceId(""); }}>
        {EVIDENCE_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></Field>
      <Field label="Record"><select className="select" value={sourceId} onChange={(event) => setSourceId(event.target.value)} disabled={!candidates.data?.items.length}>
        <option value="">{candidates.isLoading ? "Loading available records…" : candidates.data?.items.length ? "Choose a record" : "No records available"}</option>
        {(candidates.data?.items ?? []).map((candidate) => <option key={candidate.source_id} value={candidate.source_id}>{candidate.label} — {candidate.summary.slice(0, 100)}</option>)}
      </select></Field>
      {candidates.error && <ErrorState error={candidates.error} onRetry={() => void candidates.refetch()} />}
      <Field label="Relationship"><select className="select" value={relationship} onChange={(event) => setRelationship(event.target.value as typeof relationship)}>
        <option value="supports">Supports the incident claim</option><option value="contradicts">Contradicts the incident claim</option><option value="context">Context only</option>
      </select></Field>
      <Field label="Why is this evidence relevant?" hint="Required for audit; describe the connection without overstating what the source proves."><textarea className="textarea" value={note} onChange={(event) => setNote(event.target.value)} maxLength={1000} rows={3} /></Field>
    </div>
  </Dialog>;
}

function EvidenceCard({ evidence, cityId, incidentId, canEdit, onChanged }: {
  evidence: IncidentEvidence; cityId: string; incidentId: string; canEdit: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const remove = useMutation({
    mutationFn: () => api<void>(`/cities/${cityId}/incidents/${incidentId}/evidence/${evidence.id}`, { method: "DELETE" }),
    onSuccess: () => { toast("ok", "Evidence link removed"); onChanged(); },
    onError: (error) => toast("err", error instanceof ApiError ? error.message : "Evidence link could not be removed"),
  });
  return <li style={{ display: "block" }}>
    <div className="row between wrap">
      <div className="row wrap"><span className={`badge ${evidence.relationship === "supports" ? "b-ok" : evidence.relationship === "contradicts" ? "b-error" : "b-records"}`}>{titleCase(evidence.relationship)}</span>
        <span className="badge b-records">{titleCase(evidence.source_type)}</span>
        {Array.isArray(evidence.provenance) ? evidence.provenance.map((p) => <ProvenanceBadge key={p} p={p} />) : <ProvenanceBadge p={evidence.provenance} />}
        {evidence.freshness && <FreshnessBadge f={evidence.freshness} />}
      </div>
      {canEdit && <button className="btn sm ghost" type="button" disabled={remove.isPending} onClick={() => remove.mutate()} aria-label={`Unlink ${evidence.label}`}>Unlink</button>}
    </div>
    <div className="stack" style={{ gap: 4, marginTop: 7 }}>
      <b className="small">{evidence.label}</b><span className="small muted">{evidence.summary}</span>
      {evidence.note && <span className="small">Operator note: {evidence.note}</span>}
      <span className="xs faint">{evidence.source ?? "Source unavailable"}{evidence.observed_at ? ` · observed ${fmtTime(evidence.observed_at)}` : ""}{evidence.fetched_at ? ` · fetched ${fmtTime(evidence.fetched_at)}` : ""}</span>
      {!!evidence.limitations.length && <span className="xs muted">Limitations: {evidence.limitations.join(" · ")}</span>}
    </div>
  </li>;
}

interface SR { id: string; text: string; category: string | null; category_confidence: number | null; classified_by: string | null; department: string | null; status: string; ward: string | null; provenance: string; created_at: string }

function RequestList() {
  const { cityId } = useCity();
  const { can } = useSession();
  const online = useOnline();
  const qc = useQueryClient();
  const toast = useToast();
  const [offset, setOffset] = useState(0);
  const [text, setText] = useState("");
  const [ward, setWard] = useState("");
  const list = useQuery({ queryKey: ["requests", cityId, offset], queryFn: () => api<Page<SR>>(`/cities/${cityId}/service-requests`, { query: { limit: PAGE, offset } }), placeholderData: keepPreviousData });
  const m = useMutation({
    mutationFn: () => api<SR>(`/cities/${cityId}/service-requests`, { method: "POST", body: { text, ward: ward || null } }),
    onSuccess: (r) => { toast("ok", `Classified as ${titleCase(r.category ?? "unknown")} → ${r.department ?? "unrouted"} (${r.classified_by})`); setText(""); void qc.invalidateQueries({ queryKey: ["requests", cityId] }); },
    onError: (e) => toast("err", e instanceof ApiError ? e.message : "Failed"),
  });
  return (
    <div className="grid cols-3">
      <Panel title={`Service requests${list.data ? ` (${list.data.total})` : ""}`} className="span-2" flush>
        {list.error ? <div className="panel-body"><ErrorState error={list.error} /></div> : !list.data ? <div className="panel-body"><LoadingBlock /></div> : list.data.items.length === 0 ? <EmptyState title="No service requests">Requests logged by operators will appear here.</EmptyState> : (
          <div className="table-wrap"><table className="table">
            <thead><tr><th>Request</th><th>Category</th><th>Department</th><th>Status</th><th>Created</th><th>Source</th></tr></thead>
            <tbody>{list.data.items.map((r) => <RequestRow key={r.id} request={r} cityId={cityId} canEdit={can("operator")} />)}</tbody>
          </table></div>
        )}
        {list.data && list.data.total > PAGE && (
          <div className="row between" style={{ padding: 12 }}>
            <span className="small faint">{offset + 1}–{Math.min(offset + PAGE, list.data.total)} of {list.data.total}</span>
            <div className="row"><button className="btn sm" disabled={!offset} onClick={() => setOffset(offset - PAGE)}>Previous</button><button className="btn sm" disabled={offset + PAGE >= list.data.total} onClick={() => setOffset(offset + PAGE)}>Next</button></div>
          </div>
        )}
      </Panel>
      <Panel title="Log a citizen request" icon="plus">
        {can("operator") ? (
          <div className="stack-lg">
            <Field label="Complaint text" hint="Classified automatically by the local complaint model (or keyword rules if no model is promoted).">
              <textarea className="textarea" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} placeholder="e.g. Streetlight not working on 3rd Cross for a week" />
            </Field>
            <Field label="Ward (optional)"><input className="input" value={ward} onChange={(e) => setWard(e.target.value)} maxLength={80} /></Field>
            <button className="btn primary" disabled={text.trim().length < 5 || m.isPending || !online} onClick={() => m.mutate()}>{m.isPending ? "Classifying…" : "Log & classify"}</button>
            <p className="xs faint">Do not enter personal data such as phone numbers or ID numbers.</p>
          </div>
        ) : <div className="small faint">Operator role required to log requests.</div>}
      </Panel>
    </div>
  );
}

function RequestRow({ request: r, cityId, canEdit }: { request: SR; cityId: string; canEdit: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const update = useMutation({
    mutationFn: (status: "open" | "in_progress" | "resolved") => api<SR>(`/cities/${cityId}/service-requests/${r.id}`, { method: "PATCH", body: { status } }),
    onSuccess: () => { toast("ok", "Service request status updated"); void qc.invalidateQueries({ queryKey: ["requests", cityId] }); },
    onError: (error) => toast("err", error instanceof ApiError ? error.message : "Status could not be updated"),
  });
  return <tr>
    <td style={{ maxWidth: 360 }}>{r.text}<div className="xs faint">{r.ward}</div></td>
    <td>{titleCase(r.category ?? "—")}{r.category_confidence != null && <div className="xs faint">{Math.round(r.category_confidence * 100)}% · {r.classified_by}</div>}</td>
    <td className="small muted">{r.department ?? "—"}</td>
    <td>{canEdit ? <select className="select" aria-label={`Update status for request ${r.id}`} value={r.status} disabled={update.isPending || r.status === "acknowledged"} onChange={(event) => update.mutate(event.target.value as "open" | "in_progress" | "resolved")}>
      <option value="open">Open</option><option value="in_progress">In progress</option><option value="resolved">Resolved</option>
      {r.status === "acknowledged" && <option value="acknowledged">Acknowledged</option>}
    </select> : <StatusBadge s={r.status} />}</td>
    <td className="small muted">{ago(r.created_at)}</td>
    <td><ProvenanceBadge p={r.provenance} /></td>
  </tr>;
}

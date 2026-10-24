import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError } from "../api/client";
import type { DataSource, SourceStatus } from "../api/types";
import { Drawer, ErrorState, FreshnessBadge, LoadingBlock, Panel, StatusBadge, Timestamp } from "../components/ui";
import { duration, fmtTime } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useSession } from "../state/session";
import { useOnline, useToast } from "../state/ui";

export default function IntegrationsPage() {
  const { cityId, city } = useCity();
  const { can } = useSession();
  const online = useOnline();
  const toast = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const all = useQuery({ queryKey: ["sources"], queryFn: () => api<{ items: DataSource[] }>("/integrations"), refetchInterval: 15_000 });
  const per = useQuery({
    queryKey: ["city-sources", cityId],
    queryFn: () => api<{ items: SourceStatus[]; recent_runs: { source_id: string; status: string; started_at: string; duration_ms: number | null; records: number; error: string | null }[] }>(`/cities/${cityId}/integrations`),
    refetchInterval: 5_000,
  });
  const sync = useMutation({
    mutationFn: (id: string) => api<{ job_id: string }>(`/cities/${cityId}/integrations/${id}/sync`, { method: "POST", body: { force: true } }),
    onSuccess: () => { toast("info", "Synchronisation queued — status updates below"); setTimeout(() => void qc.invalidateQueries({ queryKey: ["city-sources", cityId] }), 1500); },
    onError: (e) => toast("err", e instanceof ApiError ? e.message : "Sync failed"),
  });
  const byId = Object.fromEntries((per.data?.items ?? []).map((s) => [s.id, s]));

  return (
    <>
      <div className="page-head">
        <div><h1>Data integrations</h1><p>Registered sources, their honest connection status, and per-city freshness for {city?.name}. A feed is never shown as live merely because an endpoint exists.</p></div>
      </div>
      {all.error ? <ErrorState error={all.error} /> : !all.data ? <LoadingBlock rows={6} /> : (
        <Panel title="Source registry" icon="integrations" flush>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Source</th><th>Status</th><th>{city?.name} freshness</th><th>Last run</th><th>Licence</th><th /></tr></thead>
              <tbody>
                {all.data.items.map((s) => {
                  const c = byId[s.id];
                  const syncable = s.enabled && s.scope !== "none";
                  return (
                    <tr key={s.id}>
                      <td><button className="btn ghost sm" style={{ padding: 0, height: "auto", fontWeight: 600, color: "var(--text)" }} onClick={() => setOpen(s.id)}>{s.name}</button>
                        <div className="xs faint">{s.categories.join(", ")} · {s.kind}</div></td>
                      <td><StatusBadge s={c && !c.applies_to_city ? "unavailable" : s.status} label={c && !c.applies_to_city ? "Not for this city" : undefined} />
                        {s.status_detail && <div className="xs faint" style={{ maxWidth: 260 }}>{s.status_detail}</div>}</td>
                      <td>{s.scope === "none" ? <span className="xs faint">n/a</span> : <FreshnessBadge f={c?.freshness} />}</td>
                      <td className="small">{c?.last_run ? <><StatusBadge s={c.last_run.status} /> <span className="faint"><Timestamp iso={c.last_run.started_at} /> · {c.last_run.records} rec · {c.last_run.duration_ms ?? "—"} ms</span>
                        {c.last_run.error && <div className="xs" style={{ color: "var(--red)" }}>{c.last_run.error}</div>}</> : <span className="faint">—</span>}</td>
                      <td className="xs muted" style={{ maxWidth: 200 }}>{s.license}</td>
                      <td>{can("operator") && syncable && <button className="btn sm" disabled={!online || sync.isPending} onClick={() => sync.mutate(s.id)} data-testid={`sync-${s.id}`}><Icon name="refresh" />Sync now</button>}
                        {!syncable && s.setup && <button className="btn sm ghost" onClick={() => setOpen(s.id)}>Setup guide</button>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      <Panel title="Recent synchronisation runs" flush>
        {!per.data ? <div className="panel-body"><LoadingBlock /></div> : (
          <ul className="list">
            {per.data.recent_runs.map((r, i) => (
              <li key={i} className="small"><StatusBadge s={r.status} /><span style={{ flex: 1 }}>{r.source_id} · {r.records} records · {r.duration_ms ?? "—"} ms {r.error && <span style={{ color: "var(--red)" }}>· {r.error}</span>}</span><span className="xs faint">{fmtTime(r.started_at)}</span></li>
            ))}
            {!per.data.recent_runs.length && <li className="small faint">No synchronisation runs yet for this city.</li>}
          </ul>
        )}
      </Panel>
      {open && all.data && <SourceDrawer s={all.data.items.find((x) => x.id === open)!} onClose={() => setOpen(null)} />}
    </>
  );
}

function SourceDrawer({ s, onClose }: { s: DataSource; onClose: () => void }) {
  return (
    <Drawer title={s.name} onClose={onClose}>
      <div className="stack-lg">
        <div className="row wrap"><StatusBadge s={s.status} /><span className="badge b-records">{s.kind}</span></div>
        <dl className="kv">
          <dt>Coverage</dt><dd>{s.coverage}</dd>
          <dt>Authentication</dt><dd>{s.auth_required}</dd>
          <dt>Licence</dt><dd>{s.license}</dd>
          <dt>Attribution</dt><dd>{s.attribution}</dd>
          <dt>Freshness threshold</dt><dd>{duration(s.freshness_threshold_s)}</dd>
          <dt>Min. sync interval</dt><dd>{duration(s.min_sync_interval_s)}</dd>
          <dt>Last success</dt><dd>{fmtTime(s.last_success_at)}</dd>
          <dt>Last error</dt><dd>{s.last_error ?? "—"}</dd>
          {s.homepage && <><dt>Documentation</dt><dd><a href={s.homepage} target="_blank" rel="noreferrer noopener">{s.homepage}</a></dd></>}
        </dl>
        {s.limitations.length > 0 && <div><div className="upper faint">Known limitations</div><ul className="small muted" style={{ paddingLeft: 18 }}>{s.limitations.map((l) => <li key={l}>{l}</li>)}</ul></div>}
        {s.setup && <div><div className="upper faint" style={{ marginBottom: 4 }}>Setup</div><pre className="json" style={{ whiteSpace: "pre-wrap" }}>{s.setup}</pre></div>}
      </div>
    </Drawer>
  );
}

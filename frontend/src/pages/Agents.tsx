import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { AgentInfo } from "../api/types";
import { Drawer, EmptyState, ErrorState, LoadingBlock, StatusBadge } from "../components/ui";
import { ago, pct, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";

export default function AgentsPage() {
  const { cityId } = useCity();
  const [domain, setDomain] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["agents"], queryFn: () => api<{ items: AgentInfo[]; domains: Record<string, string>; registry_size: number }>("/agents"), refetchInterval: 30_000 });
  const items = useMemo(() => (list.data?.items ?? []).filter((a) =>
    (!domain || a.domain === domain) && (!q || `${a.name} ${a.description} ${a.keywords.join(" ")}`.toLowerCase().includes(q.toLowerCase()))), [list.data, domain, q]);
  const domains = list.data?.domains ?? {};

  return (
    <>
      <div className="page-head">
        <div><h1>Agent Fleet</h1><p>{list.data?.registry_size ?? 50} registered agents across 10 domains, one shared runtime. Health reflects the last 7 days of executions.</p></div>
        <Link className="btn primary" to={`/c/${cityId}/workspace`}><Icon name="workspace" />Run a task</Link>
      </div>
      <div className="row wrap">
        <button className="chip" aria-pressed={!domain} onClick={() => setDomain("")}>All</button>
        {Object.entries(domains).map(([id, label]) => <button key={id} className="chip" aria-pressed={domain === id} onClick={() => setDomain(id)}>{label}</button>)}
        <input className="input" style={{ width: 220, marginLeft: "auto" }} placeholder="Search agents…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search agents" />
      </div>
      {list.error ? <ErrorState error={list.error} onRetry={() => void list.refetch()} /> : !list.data ? <LoadingBlock rows={8} /> : !items.length ? <EmptyState title="No agents match">Try another domain or search term.</EmptyState> : (
        <div className="grid cols-3" data-testid="agent-grid">
          {items.map((a) => (
            <button key={a.id} className="panel" style={{ textAlign: "left", padding: 14, cursor: "pointer", color: "inherit", font: "inherit" }} onClick={() => setOpen(a.id)} aria-label={`${a.name} details`}>
              <div className="row between" style={{ marginBottom: 6 }}>
                <b>{a.name}</b>
                <StatusBadge s={a.stats_7d.health} />
              </div>
              <div className="xs faint" style={{ marginBottom: 6 }}>{a.domain_label}</div>
              <p className="small muted" style={{ minHeight: 40 }}>{a.description}</p>
              <div className="row wrap xs faint" style={{ marginTop: 8 }}>
                <span>{a.stats_7d.runs} runs</span>
                {a.stats_7d.useful_rate != null && <span>· useful {pct(a.stats_7d.useful_rate)}</span>}
                {a.model && <span className="badge b-records">model: {a.model}</span>}
                {a.depends_on.length > 0 && <span>· {a.depends_on.length} dependencies</span>}
                {a.min_role !== "viewer" && <span className="badge b-demo">{a.min_role}+</span>}
              </div>
            </button>
          ))}
        </div>
      )}
      {open && <AgentDrawer id={open} onClose={() => setOpen(null)} />}
    </>
  );
}

function AgentDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["agent", id], queryFn: () => api<AgentInfo>(`/agents/${id}`) });
  const a = q.data;
  return (
    <Drawer title={a?.name ?? "Agent"} onClose={onClose}>
      {q.error ? <ErrorState error={q.error} /> : !a ? <LoadingBlock /> : (
        <div className="stack-lg">
          <p className="muted">{a.description}</p>
          <dl className="kv">
            <dt>Agent id</dt><dd className="mono">{a.id}</dd>
            <dt>Domain</dt><dd>{a.domain_label}</dd>
            <dt>Capabilities</dt><dd className="mono">{a.capabilities.join(", ")}</dd>
            <dt>Data required</dt><dd>{a.data_requirements.map(titleCase).join(", ") || "Upstream agent outputs"}</dd>
            <dt>Depends on</dt><dd>{a.depends_on.join(", ") || (a.aggregate ? "All other agents in the plan" : "—")}</dd>
            <dt>Used by</dt><dd>{a.dependents?.join(", ") || "—"}</dd>
            <dt>Trained model</dt><dd>{a.model ?? "None (rules / statistics)"}</dd>
            <dt>Minimum role</dt><dd>{a.min_role}</dd>
            <dt>Timeout / retries</dt><dd className="num">{a.timeout_s ?? "default"} s · {a.max_retries} retries</dd>
            <dt>Trigger terms</dt><dd className="small">{a.keywords.join(", ")}</dd>
          </dl>
          <div><div className="upper faint" style={{ marginBottom: 4 }}>Input schema</div><pre className="json">{JSON.stringify(a.input_schema, null, 2)}</pre></div>
          <div><div className="upper faint" style={{ marginBottom: 4 }}>Output contract</div><pre className="json">{JSON.stringify(a.output_schema, null, 2)}</pre></div>
          <div>
            <div className="upper faint" style={{ marginBottom: 4 }}>Recent executions (cities you can access)</div>
            <ul className="list">
              {(a.recent_executions ?? []).map((e, i) => (
                <li key={i} className="small"><StatusBadge s={e.status} /><span style={{ flex: 1 }}>{e.city_id} · {e.duration_ms ?? "—"} ms{e.confidence != null ? ` · conf ${pct(e.confidence)}` : ""}{e.error ? ` · ${e.error}` : ""}</span><span className="xs faint">{ago(e.finished_at)}</span></li>
              ))}
              {!a.recent_executions?.length && <li className="small faint">Not run yet.</li>}
            </ul>
          </div>
        </div>
      )}
    </Drawer>
  );
}

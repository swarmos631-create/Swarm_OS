import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError } from "../api/client";
import { EmptyState, ErrorState, Field, LoadingBlock, Panel, ProvenanceBadge, StatusBadge } from "../components/ui";
import { fmtNum, fmtTime } from "../lib/format";
import { useCity } from "../state/city";
import { useSession } from "../state/session";
import { useToast } from "../state/ui";

interface ScenarioResult {
  status?: string;
  summary?: string;
  metrics?: {
    assumptions?: { extra_rainfall_mm_24h?: number; temperature_delta_c?: number };
    flood?: { baseline?: { score?: number; level?: string }; scenario?: { score?: number; level?: string }; delta?: number };
    heat?: { baseline?: { level?: string; [key: string]: unknown }; scenario?: { level?: string; [key: string]: unknown } };
  };
  provenance?: string[];
  limitations?: string[];
  missing?: string[];
}
interface ScenarioRun {
  id: string;
  name: string;
  task_id: string;
  status: string;
  params: { extra_rainfall_mm: number; temperature_delta_c: number };
  summary: string | null;
  result: ScenarioResult | null;
  created_at: string;
  finished_at: string | null;
  error: string | null;
}

export default function ScenarioLabPage() {
  const { cityId, city } = useCity();
  const { me } = useSession();
  const toast = useToast();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [rain, setRain] = useState(50);
  const [heat, setHeat] = useState(0);
  const runs = useQuery({
    queryKey: ["scenarios", cityId],
    queryFn: () => api<{ items: ScenarioRun[]; total: number }>(`/cities/${cityId}/scenarios`, { query: { limit: 50 } }),
    refetchInterval: (query) => query.state.data?.items.some((item) => ["queued", "running"].includes(item.status)) ? 3000 : false,
  });
  const create = useMutation({
    mutationFn: () => api<ScenarioRun>(`/cities/${cityId}/scenarios`, {
      method: "POST", body: { name: name.trim(), extra_rainfall_mm: rain, temperature_delta_c: heat },
    }),
    onSuccess: () => {
      setName("");
      toast("ok", "Scenario saved and queued");
      void qc.invalidateQueries({ queryKey: ["scenarios", cityId] });
      void qc.invalidateQueries({ queryKey: ["tasks", cityId] });
    },
    onError: (error) => toast("err", error instanceof ApiError ? error.message : "Scenario could not be started"),
  });
  const items = runs.data?.items ?? [];
  const canOperate = me?.role === "operator" || me?.role === "admin";

  return <>
    <div className="page-head">
      <div><h1>Scenario Lab — {city?.name}</h1><p>Save reproducible weather stress tests and compare each result with the measured or forecast baseline used by the agent.</p></div>
    </div>
    <div className="grid cols-3">
      {canOperate && <Panel title="Run a what-if scenario" className="span-1">
        <form className="stack-lg" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
          <p className="small muted" style={{ marginTop: 0 }}>These inputs perturb current weather inputs; they are not forecasts or physical flood simulations.</p>
          <Field label="Scenario name"><input className="input" value={name} minLength={3} maxLength={120} required onChange={(event) => setName(event.target.value)} placeholder="Example: Heavy evening rainfall" /></Field>
          <Field label="Additional rainfall in 24 h" hint="0–500 mm applied on top of the current rainfall inputs."><div className="row"><input className="input" type="number" min={0} max={500} step={1} value={rain} onChange={(event) => setRain(Number(event.target.value))} /><span className="small muted">mm</span></div></Field>
          <Field label="Temperature change" hint="−10 to +10 °C applied to the available forecast series."><div className="row"><input className="input" type="number" min={-10} max={10} step={0.5} value={heat} onChange={(event) => setHeat(Number(event.target.value))} /><span className="small muted">°C</span></div></Field>
          <button className="btn primary" type="submit" disabled={create.isPending || name.trim().length < 3}>{create.isPending ? "Queueing…" : "Save and run scenario"}</button>
        </form>
      </Panel>}
      <Panel title={`Saved runs (${runs.data?.total ?? "…"})`} className={canOperate ? "span-2" : "span-3"}>
        {runs.error ? <ErrorState error={runs.error} onRetry={() => void runs.refetch()} /> : !runs.data ? <LoadingBlock rows={5} /> : items.length ? (
          <div className="table-wrap"><table className="table">
            <thead><tr><th>Scenario</th><th>Inputs</th><th>Flood index</th><th>Heat</th><th>State</th><th>Started</th></tr></thead>
            <tbody>{items.map((run) => <ScenarioRow key={run.id} run={run} />)}</tbody>
          </table></div>
        ) : <EmptyState title="No saved scenarios yet">Run a rainfall or heat stress test to start building a comparable history for this city.</EmptyState>}
      </Panel>
    </div>
    <div className="notice info" style={{ marginTop: 16 }}><span>Every result is a deterministic rule-based perturbation of currently available inputs. It does not predict flood extent, casualties, or intervention effects.</span></div>
  </>;
}

function ScenarioRow({ run }: { run: ScenarioRun }) {
  const result = run.result;
  const metrics = result?.metrics;
  const flood = metrics?.flood;
  const heat = metrics?.heat;
  return <>
    <tr>
      <td><b>{run.name}</b><div className="xs faint">Task {run.task_id.slice(0, 10)} · {run.finished_at ? fmtTime(run.finished_at) : "Awaiting result"}</div></td>
      <td className="small">+{fmtNum(run.params.extra_rainfall_mm)} mm<br />{run.params.temperature_delta_c >= 0 ? "+" : ""}{fmtNum(run.params.temperature_delta_c)} °C</td>
      <td className="small">{flood ? <>{flood.baseline?.score ?? "—"} → {flood.scenario?.score ?? "—"}<div className="xs faint">Δ {flood.delta != null ? `${flood.delta > 0 ? "+" : ""}${flood.delta}` : "—"} · {flood.baseline?.level ?? "—"} → {flood.scenario?.level ?? "—"}</div></> : result?.missing?.length ? <span className="faint">Baseline data unavailable</span> : "Awaiting data"}</td>
      <td className="small">{heat ? <>{heat.baseline?.level ?? "—"} → {heat.scenario?.level ?? "—"}</> : result?.missing?.length ? <span className="faint">Baseline data unavailable</span> : "Awaiting data"}</td>
      <td><StatusBadge s={run.status} /></td>
      <td className="xs faint">{fmtTime(run.created_at)}</td>
    </tr>
    {run.status === "succeeded" || run.status === "partial" ? <tr><td colSpan={6}>
      <div className="stack" style={{ gap: 5 }}>
        {run.summary && <span className="small">{run.summary}</span>}
        <div className="row wrap">{(result?.provenance ?? []).map((p) => <ProvenanceBadge key={p} p={p} />)}</div>
        {!!result?.limitations?.length && <span className="xs muted">Limitations: {result.limitations.join(" · ")}</span>}
      </div>
    </td></tr> : run.error ? <tr><td colSpan={6}><span className="small" style={{ color: "var(--red)" }}>{run.error}</span></td></tr> : null}
  </>;
}

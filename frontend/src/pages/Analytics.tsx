import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { BarList, LineChart, StackedBars } from "../components/charts";
import { EmptyState, ErrorState, LoadingBlock, Panel, ProvenanceBadge } from "../components/ui";
import { fmtNum, fmtTime, pct, titleCase } from "../lib/format";
import { useCities, useCity } from "../state/city";

interface Analytics {
  range_days: number;
  incidents: { total: number; by_day: [string, Record<string, number>][]; severity: Record<string, number>; category: Record<string, number>; mttr_h: number | null; provenance: string[] };
  tasks: Record<string, number>;
  agent_executions: Record<string, Record<string, number>>;
  source_reliability: Record<string, { succeeded: number; failed: number; avg_ms: number | null; success_rate: number | null }>;
  environment: Record<string, { unit: string; provenance: string[]; daily: [string, number][]; aggregation: string }>;
}

const SEV = { critical: "var(--red)", high: "var(--orange)", moderate: "var(--amber)", low: "var(--blue)" };

export default function AnalyticsPage() {
  const { cityId, city } = useCity();
  const [days, setDays] = useState(14);
  const q = useQuery({ queryKey: ["analytics", cityId, days], queryFn: () => api<Analytics>(`/cities/${cityId}/analytics`, { query: { days } }) });
  const a = q.data;
  const execTotals = a ? Object.entries(a.agent_executions).map(([agent, s]) => {
    const total = Object.values(s).reduce((x, y) => x + y, 0);
    return { agent, total, useful: (s.succeeded ?? 0) + (s.degraded ?? 0), failed: (s.failed ?? 0) + (s.timed_out ?? 0), unavailable: s.unavailable ?? 0 };
  }) : [];

  return (
    <>
      <div className="page-head">
        <div><h1>Analytics — {city?.name}</h1><p>Only datasets that actually exist for this city are charted. No forecasts are fabricated to fill charts.</p></div>
        <div className="row" role="group" aria-label="Time range">
          {[7, 14, 30, 90].map((d) => <button key={d} className="chip" aria-pressed={days === d} onClick={() => setDays(d)}>{d} days</button>)}
        </div>
      </div>
      {q.error ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !a ? <LoadingBlock rows={8} /> : (
        <>
          <div className="grid cols-3">
            <Panel title={`Incidents per day (${a.incidents.total})`} className="span-2" actions={a.incidents.provenance.map((p) => <ProvenanceBadge key={p} p={p} />)}>
              {a.incidents.total ? <StackedBars data={a.incidents.by_day} keys={["critical", "high", "moderate", "low"]} colors={SEV} label="Incidents per day by severity" />
                : <EmptyState title="No incidents in this range" />}
            </Panel>
            <Panel title="Severity & category">
              <div className="stack-lg">
                <BarList items={Object.entries(a.incidents.severity).map(([k, v]) => ({ label: titleCase(k), value: v }))} color="var(--orange)" />
                <BarList items={Object.entries(a.incidents.category).sort((x, y) => y[1] - x[1]).slice(0, 8).map(([k, v]) => ({ label: titleCase(k), value: v }))} color="var(--violet)" />
                <div className="small">Mean time to resolve: <b className="num">{a.incidents.mttr_h != null ? `${fmtNum(a.incidents.mttr_h)} h` : "—"}</b></div>
              </div>
            </Panel>
          </div>
          <div className="grid cols-3">
            {(["temperature_2m", "pm2_5", "precipitation"] as const).map((m) => {
              const e = a.environment[m];
              const label = { temperature_2m: "Temperature", pm2_5: "PM2.5 (CAMS model)", precipitation: "Rainfall" }[m];
              return (
                <Panel key={m} title={label} actions={e?.provenance.map((p) => <ProvenanceBadge key={p} p={p} />)}>
                  {e && e.daily.length > 1 ? (
                    <>
                      <LineChart series={[{ name: label, points: e.daily.map(([d, v]) => [`${d}T12:00:00Z`, v]), color: m === "pm2_5" ? "var(--violet)" : m === "precipitation" ? "var(--blue)" : m === "temperature_2m" ? "var(--orange)" : "var(--cyan)" }]} height={170} unit={e.unit} label={`${label} ${e.aggregation}`} yZero={m === "precipitation"} />
                      <p className="xs faint">{titleCase(e.aggregation)} · {e.unit} · Open-Meteo model analysis</p>
                    </>
                  ) : <EmptyState title="Insufficient history">Live synchronisation for this city has not accumulated enough days yet.</EmptyState>}
                </Panel>
              );
            })}
          </div>
          <ForecastEvaluation days={days} />
          <CrossTaskReview days={days} />
          <div className="grid cols-2">
            <Panel title="Agent reliability (this city)">
              {execTotals.length ? (
                <div className="table-wrap"><table className="table">
                  <thead><tr><th>Agent</th><th>Runs</th><th>Useful</th><th>Unavailable</th><th>Failed</th></tr></thead>
                  <tbody>{execTotals.sort((x, y) => y.total - x.total).slice(0, 15).map((r) => (
                    <tr key={r.agent}><td className="small">{r.agent}</td><td className="num">{r.total}</td><td className="num">{pct(r.useful / r.total)}</td><td className="num">{r.unavailable}</td><td className="num" style={{ color: r.failed ? "var(--red)" : undefined }}>{r.failed}</td></tr>))}</tbody>
                </table></div>
              ) : <EmptyState title="No agent runs in this range" />}
            </Panel>
            <Panel title="Data-source reliability">
              {Object.keys(a.source_reliability).length ? (
                <BarList items={Object.entries(a.source_reliability).map(([k, v]) => ({ label: `${k} (${v.succeeded + v.failed} syncs, avg ${v.avg_ms ?? "—"} ms)`, value: v.success_rate ?? 0 }))} format={(v) => pct(v)} max={1} color="var(--green)" />
              ) : <EmptyState title="No synchronisation runs in this range" />}
            </Panel>
          </div>
          <CityCompare />
        </>
      )}
    </>
  );
}

interface CrossTaskReviewData {
  tasks_reviewed: number;
  metric_relationships: { signal: string; task_count: number; domains: string[]; relationship: string; instances: { task_id: string; agent_id: string; domain: string; task_created_at: string; execution_status: string; value: number; provenance: string[]; evidence_refs: Record<string, unknown>[] }[] }[];
  finding_relationships: { signal: string; task_count: number; domains: string[]; relationship: string; instances: { title: string; task_id: string; agent_id: string; domain: string; task_created_at: string; severity: string; provenance: string[]; evidence_refs: Record<string, unknown>[] }[] }[];
  limitations: string[];
}

function CrossTaskReview({ days }: { days: number }) {
  const { cityId } = useCity();
  const q = useQuery({
    queryKey: ["impact-review", cityId, days],
    queryFn: () => api<CrossTaskReviewData>(`/cities/${cityId}/impact-review`, { query: { days } }),
  });
  return <Panel title={`Cross-task impact review (${days} days)`} icon="analytics">
    {q.error ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !q.data ? <LoadingBlock rows={3} /> : <div className="stack-lg">
      <div className="row wrap"><span className="badge b-records">{q.data.tasks_reviewed} runs reviewed</span><span className="badge b-info">Exact-match descriptive review</span></div>
      <div className="grid cols-2">
        <Panel title={`Shared metric paths (${q.data.metric_relationships.length})`} flush>
          {q.data.metric_relationships.length ? <div className="stack">{q.data.metric_relationships.map((group) => <section key={group.signal} className="panel-body" style={{ borderBottom: "1px solid var(--line)" }}>
            <div className="row between wrap"><b className="small">{group.signal}</b><span className="badge b-records">{group.task_count} tasks</span></div>
            <div className="row wrap xs faint">{group.domains.map((domain) => <span key={domain}>{titleCase(domain)}</span>)}</div>
            <ul className="list">{group.instances.map((item, index) => <li key={`${item.task_id}-${item.agent_id}-${index}`} className="small">
              <span>{item.agent_id} · {fmtNum(item.value)}</span><span className="xs faint">{item.execution_status} · {item.provenance.join(", ") || "origin unknown"} · Task {item.task_id.slice(0, 10)} · {fmtTime(item.task_created_at)}</span>
            </li>)}</ul>
          </section>)}</div> : <EmptyState title="No repeated cross-domain metric paths">Two or more domains must report the same exact path across separate tasks.</EmptyState>}
        </Panel>
        <Panel title={`Shared finding titles (${q.data.finding_relationships.length})`} flush>
          {q.data.finding_relationships.length ? <div className="stack">{q.data.finding_relationships.map((group) => <section key={group.signal} className="panel-body" style={{ borderBottom: "1px solid var(--line)" }}>
            <div className="row between wrap"><b className="small">{group.signal}</b><span className="badge b-records">{group.task_count} tasks</span></div>
            <div className="row wrap xs faint">{group.domains.map((domain) => <span key={domain}>{titleCase(domain)}</span>)}</div>
            <ul className="list">{group.instances.map((item, index) => <li key={`${item.task_id}-${item.agent_id}-${index}`} className="small">
              <span>{item.agent_id} · {titleCase(item.severity)}</span><span className="xs faint">{item.provenance.join(", ") || "origin unknown"} · Task {item.task_id.slice(0, 10)} · {fmtTime(item.task_created_at)}</span>
            </li>)}</ul>
          </section>)}</div> : <EmptyState title="No repeated cross-domain findings">Finding titles are matched exactly after case and punctuation normalization.</EmptyState>}
        </Panel>
      </div>
      <div className="small muted"><b>Review limitations</b><ul>{q.data.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div>
    </div>}
  </Panel>;
}

interface ForecastEvaluationData {
  status: string;
  matched_count: number;
  valid_time_tolerance_hours: number;
  summary: { metric: string; unit: string; matched_count: number; mean_absolute_error: number; mean_bias: number }[];
  matches: { metric: string; entity: string; unit: string; forecast: number; observation: number; absolute_error: number; forecast_issued_at: string; observed_at: string; lead_hours: number }[];
  limitations: string[];
}

function ForecastEvaluation({ days }: { days: number }) {
  const { cityId } = useCity();
  const q = useQuery({
    queryKey: ["forecast-evaluation", cityId, days],
    queryFn: () => api<ForecastEvaluationData>(`/cities/${cityId}/forecast-evaluation`, { query: { days, window_hours: 3 } }),
  });
  return <Panel title={`Forecast evaluation (${days} days)`} icon="analytics">
    {q.error ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !q.data ? <LoadingBlock rows={3} /> : q.data.status !== "evaluated" ?
      <EmptyState title="Not enough matched live data">Forecasts and observations must share a metric, entity and unit, and their valid times must be within three hours.</EmptyState> : <div className="stack-lg">
        <div className="table-wrap"><table className="table"><thead><tr><th>Metric</th><th>Matched pairs</th><th>Mean absolute error</th><th>Mean bias</th></tr></thead>
          <tbody>{q.data.summary.map((item) => <tr key={`${item.metric}-${item.unit}`}><td>{item.metric} ({item.unit})</td><td className="num">{item.matched_count}</td><td className="num">{fmtNum(item.mean_absolute_error)} {item.unit}</td><td className="num">{fmtNum(item.mean_bias)} {item.unit}</td></tr>)}</tbody>
        </table></div>
        {!!q.data.matches.length && <details><summary className="small">Recent matched pairs ({q.data.matched_count})</summary><div className="table-wrap" style={{ marginTop: 8 }}><table className="table">
          <thead><tr><th>Metric / entity</th><th>Forecast</th><th>Observed</th><th>Abs. error</th><th>Lead</th><th>Times</th></tr></thead>
          <tbody>{q.data.matches.slice(0, 30).map((item, index) => <tr key={`${item.metric}-${item.entity}-${item.observed_at}-${index}`}>
            <td>{item.metric}<div className="xs faint">{item.entity}</div></td><td className="num">{fmtNum(item.forecast)} {item.unit}</td><td className="num">{fmtNum(item.observation)} {item.unit}</td>
            <td className="num">{fmtNum(item.absolute_error)}</td><td className="num">{fmtNum(item.lead_hours)} h</td>
            <td className="xs muted">Issued {item.forecast_issued_at}<br />Observed {item.observed_at}</td>
          </tr>)}</tbody>
        </table></div></details>}
        <div className="small muted"><b>Method and limitations</b><ul>{q.data.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div>
      </div>}
  </Panel>;
}

function CityCompare() {
  const { cityId } = useCity();
  const cities = useCities();
  const [metric, setMetric] = useState("pm2_5");
  const [picked, setPicked] = useState<string[]>(() => Array.from(new Set([cityId, "delhi", "mumbai", "bengaluru"])).slice(0, 4));
  const q = useQuery({
    queryKey: ["compare", metric, picked.join(",")],
    queryFn: () => api<{ items: { city_id: string; name: string; mean: number | null; points: number; unit: string | null; note: string | null }[]; source: string }>("/analytics/compare", { query: { metric, cities: picked.join(","), hours: 24 } }),
    enabled: picked.length > 0,
  });
  return (
    <Panel title="City comparison (24 h mean, same source & method)" actions={
      <select className="select" style={{ width: 200 }} value={metric} onChange={(e) => setMetric(e.target.value)} aria-label="Metric">
        <option value="pm2_5">PM2.5 (µg/m³)</option><option value="pm10">PM10 (µg/m³)</option><option value="temperature_2m">Temperature (°C)</option><option value="relative_humidity_2m">Humidity (%)</option><option value="precipitation">Rainfall (mm/h)</option>
      </select>}>
      <div className="row wrap" style={{ marginBottom: 10 }}>
        {(cities.data?.items ?? []).filter((c) => c.has_live_data || picked.includes(c.id)).map((c) => (
          <button key={c.id} className="chip" aria-pressed={picked.includes(c.id)} onClick={() => setPicked((p) => (p.includes(c.id) ? p.filter((x) => x !== c.id) : [...p, c.id].slice(0, 8)))}>{c.name}</button>
        ))}
      </div>
      {q.error ? <ErrorState error={q.error} /> : !q.data ? <LoadingBlock /> : (
        <>
          <BarList items={q.data.items.filter((i) => i.mean != null).map((i) => ({ label: i.name, value: i.mean!, hint: `${i.points} hourly points` }))} format={(v) => fmtNum(v)} color="var(--cyan)" />
          {q.data.items.filter((i) => i.mean == null).map((i) => <p key={i.city_id} className="xs faint">{i.name}: {i.note}</p>)}
          <p className="xs faint" style={{ marginTop: 6 }}>{q.data.source}. Cities without sufficient synchronised coverage are excluded rather than estimated.</p>
        </>
      )}
    </Panel>
  );
}

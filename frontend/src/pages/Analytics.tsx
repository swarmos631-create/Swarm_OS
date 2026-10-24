import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { BarList, LineChart, StackedBars } from "../components/charts";
import { EmptyState, ErrorState, LoadingBlock, Panel, ProvenanceBadge } from "../components/ui";
import { fmtNum, pct, titleCase } from "../lib/format";
import { useCities, useCity } from "../state/city";

interface Analytics {
  range_days: number;
  incidents: { total: number; by_day: [string, Record<string, number>][]; severity: Record<string, number>; category: Record<string, number>; mttr_h: number | null; provenance: string[] };
  tasks: Record<string, number>;
  agent_executions: Record<string, Record<string, number>>;
  source_reliability: Record<string, { succeeded: number; failed: number; avg_ms: number | null; success_rate: number | null }>;
  environment: Record<string, { unit: string; provenance: string[]; daily: [string, number][]; aggregation: string }>;
}

const SEV = { critical: "#f43f5e", high: "#fb923c", moderate: "#fbbf24", low: "#60a5fa" };

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
                      <LineChart series={[{ name: label, points: e.daily.map(([d, v]) => [`${d}T12:00:00Z`, v]), color: m === "pm2_5" ? "#8b5cf6" : m === "precipitation" ? "#38bdf8" : "#22d3ee" }]} height={170} unit={e.unit} label={`${label} ${e.aggregation}`} yZero={m === "precipitation"} />
                      <p className="xs faint">{titleCase(e.aggregation)} · {e.unit} · Open-Meteo model analysis</p>
                    </>
                  ) : <EmptyState title="Insufficient history">Live synchronisation for this city has not accumulated enough days yet.</EmptyState>}
                </Panel>
              );
            })}
          </div>
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

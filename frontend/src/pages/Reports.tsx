import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { Page, Provenance } from "../api/types";
import { EmptyState, ErrorState, LoadingBlock, Panel, ProvenanceBadge, StatusBadge } from "../components/ui";
import { fmtTime } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useSession } from "../state/session";
import { useOnline, useToast } from "../state/ui";

interface Indicator { id: string; label: string; status: string; value: string | number | null; unit?: string | null; provenance?: string; kind?: string; observed_at?: string | null; fetched_at?: string | null; note?: string | null }
interface SituationSnapshot {
  city?: { name: string; state: string };
  generated_at?: string;
  reporting_period?: { captured_at: string; description: string };
  indicators?: Indicator[];
  incidents?: { open: number; by_severity: Record<string, number>; recent: { title: string; severity: string; status: string; reported_at: string }[] };
  infrastructure?: { osm_feature_counts?: Record<string, number>; earthquakes_7d_500km?: number; strongest_quake?: { magnitude: number } | null };
  agents?: { recent_tasks?: { id: string; status: string; created_at: string }[]; executions_24h?: Record<string, number> };
  trends?: Record<string, { unit: string; provenance: string; kind: string; source_id: string; points: [string, number][] } | null>;
  alerts?: { id: string; level: string; title: string; body: string; created_at: string }[];
  source_health?: { id: string; name: string; status: string; status_detail: string | null; last_success_at: string | null; last_run: { status: string; started_at: string; error: string | null } | null; freshness: { state: string; age_s: number | null } | null }[];
  sync_failures?: { id: string; name: string; last_run: { status: string; started_at: string; error: string | null } | null }[];
  recommendations?: { id: string; title: string; kind: string; agent_id: string; rationale: string; risk: string; status: string; created_at: string; decided_at: string | null; executed_at: string | null }[];
  intervention_outcomes?: { action_id: string; outcome: string; recorded_at: string; linked_measurement_count: number }[];
  scenario_runs?: { id: string; task_id: string; name: string; inputs: Record<string, unknown>; status: string; created_at: string; finished_at: string | null; summary: string; metrics: Record<string, any>; provenance: string[]; limitations: string[]; missing: string[] }[];
  agent_reviews?: { task_id: string; status: string; created_at: string; finished_at: string | null; summary: string; agent_count: number; consensus: { status: string; shared_signals: number; material_differences: number; limitations: string[]; comparisons: { kind: string; signal: string; material_difference: boolean; values: { agent_id: string; value?: number; title?: string; severity?: string }[] }[] } }[];
  cross_domain_impacts?: { task_id: string; agent_id: string; summary: string; findings: { title?: string; detail?: string; severity?: string }[]; metrics: Record<string, unknown>; data_refs: unknown[]; provenance: string[]; limitations: string[] }[];
}
interface SituationReport { id: string; city_id: string; title: string; created_at: string; generated_at: string | null; snapshot?: SituationSnapshot }

export default function ReportsPage() {
  const { cityId, city } = useCity();
  const { me } = useSession();
  const online = useOnline();
  const toast = useToast();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["reports", cityId], queryFn: () => api<Page<SituationReport>>("/cities/" + cityId + "/reports", { query: { limit: 30 } }) });
  const report = useQuery({
    queryKey: ["situation-report", cityId, selected],
    queryFn: () => api<SituationReport>("/cities/" + cityId + "/reports/" + selected),
    enabled: !!selected,
  });
  const create = useMutation({
    mutationFn: () => api<SituationReport>("/cities/" + cityId + "/reports", { method: "POST" }),
    onSuccess: (value) => { setSelected(value.id); toast("ok", "Situation snapshot saved"); void qc.invalidateQueries({ queryKey: ["reports", cityId] }); },
    onError: () => toast("err", "Situation report could not be generated"),
  });
  const canCreate = me?.role === "operator" || me?.role === "admin";
  const download = () => {
    const value = report.data;
    if (!value?.snapshot) return;
    const blob = new Blob([reportMarkdown(value)], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "swarmos-situation-" + cityId + "-" + value.id.slice(0, 8) + ".md";
    link.click();
    URL.revokeObjectURL(url);
  };

  return <>
    <div className="page-head no-print">
      <div><h1>Situation Reports — {city?.name}</h1><p>Persisted city snapshots with source status, timestamps and missing-data notes. Reports use current records; no narrative is invented.</p></div>
      {canCreate && <button className="btn primary" disabled={!online || create.isPending} onClick={() => create.mutate()}><Icon name="plus" />{create.isPending ? "Capturing…" : "Capture current snapshot"}</button>}
    </div>
    <div className="grid cols-3">
      <Panel title={"Report history (" + (list.data?.total ?? "…") + ")"} className="span-1 no-print">
        {list.error ? <ErrorState error={list.error} onRetry={() => void list.refetch()} /> : !list.data ? <LoadingBlock rows={5} /> : list.data.items.length ? (
          <ul className="list">{list.data.items.map((item) => <li key={item.id} style={{ alignItems: "flex-start" }}>
            <button className="link" type="button" onClick={() => setSelected(item.id)} style={{ textAlign: "left" }}>
              <b className="small">{item.title}</b><span className="xs faint">{fmtTime(item.created_at)}</span>
            </button><button className="btn sm ghost" aria-label={"Open " + item.title} onClick={() => setSelected(item.id)}>Open</button>
          </li>)}</ul>
        ) : <EmptyState title="No reports saved">Operators can capture a point-in-time city snapshot.</EmptyState>}
      </Panel>
      <div className={selected ? "span-2 report-sheet" : "span-2"}>
        {report.error ? <ErrorState error={report.error} onRetry={() => void report.refetch()} />
          : selected && !report.data ? <LoadingBlock rows={7} />
          : report.data ? <ReportDetail report={report.data} onDownload={download} />
          : <Panel title="Snapshot preview"><EmptyState title="Select a report">Saved reports preserve a compact snapshot of available city data.</EmptyState></Panel>}
      </div>
    </div>
  </>;
}

function ReportDetail({ report, onDownload }: { report: SituationReport; onDownload: () => void }) {
  const snapshot = report.snapshot;
  return <Panel title={report.title} icon="analytics" actions={<div className="row no-print">
    <button className="btn sm" onClick={onDownload}><Icon name="download" />Download Markdown</button>
    <button className="btn sm ghost" onClick={() => window.print()}><Icon name="print" />Print / Save as PDF</button>
  </div>}>
    {!snapshot ? <EmptyState title="Report snapshot unavailable">The saved report does not contain its snapshot.</EmptyState> : <div className="stack-lg">
      <div className="row between wrap"><span className="small muted">{snapshot.city?.name}, {snapshot.city?.state} · Snapshot {fmtTime(snapshot.generated_at ?? report.created_at)}</span><span className="badge b-records">Saved snapshot</span></div>
      {snapshot.reporting_period && <p className="xs faint">{snapshot.reporting_period.description}</p>}
      <Panel title="Current indicators" flush>
        {snapshot.indicators?.length ? <div className="table-wrap"><table className="table">
          <thead><tr><th>Indicator</th><th>Value</th><th>Kind and origin</th><th>Limitations / time</th></tr></thead>
          <tbody>{snapshot.indicators.map((item) => <tr key={item.id}>
            <td>{item.label}<div className="xs faint">{item.id}</div></td>
            <td>{item.status === "available" ? <><b>{item.value ?? "—"}</b> {item.unit ?? ""}</> : <span className="faint">Unavailable</span>}</td>
            <td><StatusBadge s={item.kind ?? item.status} />{item.provenance && <div style={{ marginTop: 4 }}><ProvenanceBadge p={item.provenance as Provenance} /></div>}</td>
            <td className="xs muted">{item.note ?? "—"}{item.observed_at ? <div>Observed {fmtTime(item.observed_at)}</div> : null}{item.fetched_at ? <div>Fetched {fmtTime(item.fetched_at)}</div> : null}</td>
          </tr>)}</tbody>
        </table></div> : <EmptyState title="No indicator records in snapshot" />}
      </Panel>
      <div className="grid cols-2">
        <Panel title={"Open incidents (" + (snapshot.incidents?.open ?? 0) + ")"}>
          {snapshot.incidents?.recent.length ? <ul className="list">{snapshot.incidents.recent.map((incident) => <li key={incident.title}>
            <span className="small">{incident.title}</span><StatusBadge s={incident.severity} /><span className="xs faint">{incident.status} · {fmtTime(incident.reported_at)}</span>
          </li>)}</ul> : <p className="small muted">No open incidents in this snapshot.</p>}
        </Panel>
        <Panel title="Data-source status">
          {snapshot.source_health?.length ? <ul className="list">{snapshot.source_health.map((source) => <li key={source.id}>
            <div className="stack" style={{ gap: 4, flex: 1 }}><span className="small">{source.name}</span>
              {source.status_detail && <span className="xs faint">{source.status_detail}</span>}
              <span className="xs faint">{source.last_success_at ? `Last successful sync ${fmtTime(source.last_success_at)}` : "No successful sync recorded for this city"}</span>
              {source.last_run?.error && <span className="xs" style={{ color: "var(--red)" }}>Latest attempt: {source.last_run.error}</span>}
            </div><StatusBadge s={source.status} />
            {source.freshness && <span className="xs faint">{source.freshness.state} · {source.freshness.age_s == null ? "—" : `${Math.round(source.freshness.age_s / 3600)} h`}</span>}
          </li>)}</ul> : <p className="small muted">No source status records available.</p>}
          {!!snapshot.sync_failures?.length && <p className="xs faint">{snapshot.sync_failures.length} source sync(s) failed on the latest recorded attempt.</p>}
        </Panel>
      </div>
      <div className="grid cols-2">
        <Panel title="Infrastructure and alerts">
          <div className="stack">
            <span className="small">Mapped OSM feature counts</span>
            {Object.entries(snapshot.infrastructure?.osm_feature_counts ?? {}).length
              ? <ul className="list">{Object.entries(snapshot.infrastructure?.osm_feature_counts ?? {}).map(([layer, count]) => <li key={layer}><span className="small">{layer.replaceAll("_", " ")}</span><b>{count}</b></li>)}</ul>
              : <p className="xs faint">No mapped feature counts in this snapshot.</p>}
            <span className="xs faint">Community mapping is incomplete; a missing feature does not prove absence.</span>
            {!!snapshot.alerts?.length && <><div className="upper faint">Recent platform alerts</div><ul className="list">{snapshot.alerts.map((alert) => <li key={alert.id}><StatusBadge s={alert.level} /><span className="small" style={{ flex: 1 }}>{alert.title}</span><span className="xs faint">{fmtTime(alert.created_at)}</span></li>)}</ul></>}
          </div>
        </Panel>
        <Panel title="Agent runs and review">
          {snapshot.agent_reviews?.length ? <ul className="list">{snapshot.agent_reviews.map((item) => <li key={item.task_id} style={{ alignItems: "flex-start" }}>
            <div className="stack" style={{ gap: 4, flex: 1 }}><span className="small">{item.summary || `Task ${item.task_id.slice(0, 10)}`}</span>
              <span className="xs faint">{item.agent_count} agents · {fmtTime(item.created_at)}{item.finished_at ? ` · finished ${fmtTime(item.finished_at)}` : ""}</span>
              <span className="xs muted">Consensus review: {item.consensus.status.replaceAll("_", " ")} · {item.consensus.shared_signals} shared signals · {item.consensus.material_differences} material differences</span>
              {!!item.consensus.comparisons.length && <ul className="xs muted">{item.consensus.comparisons.slice(0, 5).map((signal) => <li key={`${signal.kind}-${signal.signal}`}>{signal.kind}: {signal.signal} · {signal.material_difference ? "difference flagged for review" : "no material difference detected"}</li>)}</ul>}
              {!!item.consensus.limitations.length && <span className="xs faint">{item.consensus.limitations[0]}</span>}
            </div><StatusBadge s={item.status} />
          </li>)}</ul> : <p className="small muted">No recent persisted agent runs in this snapshot.</p>}
          <div className="xs faint">Consensus is a deterministic review aid; it does not establish which finding is true.</div>
        </Panel>
      </div>
      <Panel title="Recommendations and decisions">
        {snapshot.recommendations?.length ? <div className="table-wrap"><table className="table"><thead><tr><th>Recommendation</th><th>State</th><th>Risk</th><th>Rationale</th><th>Decision time</th></tr></thead><tbody>
          {snapshot.recommendations.map((item) => <tr key={item.id}><td>{item.title}<div className="xs faint">{item.agent_id} · {item.kind}</div></td><td><StatusBadge s={item.status} /></td><td className="small">{item.risk}</td><td className="small muted">{item.rationale || "No rationale recorded"}</td><td className="xs faint">{item.decided_at ? fmtTime(item.decided_at) : "No decision recorded"}</td></tr>)}
        </tbody></table></div> : <p className="small muted">No saved recommendations in this snapshot.</p>}
        <p className="xs faint">An approval records a decision; it does not prove that an intervention was applied or effective.</p>
      </Panel>
      <div className="grid cols-2">
        <Panel title="Saved scenario comparisons">
          {snapshot.scenario_runs?.length ? <ul className="list">{snapshot.scenario_runs.map((item) => <li key={item.id} style={{ alignItems: "flex-start" }}><div className="stack" style={{ gap: 5, flex: 1 }}>
            <b className="small">{item.name}</b><span className="xs faint">{fmtTime(item.created_at)} · task {item.task_id.slice(0, 10)}</span><span className="xs muted">Inputs: {JSON.stringify(item.inputs)}</span>
            {item.summary && <span className="small">{item.summary}</span>}
            {scenarioFlood(item.metrics) && <span className="xs muted">Flood index: {scenarioFlood(item.metrics)} (simulated baseline comparison)</span>}
            {!!item.missing.length && <span className="xs faint">Missing: {item.missing.join(", ")}</span>}
            {!!item.limitations.length && <span className="xs faint">Limitations: {item.limitations.join(" · ")}</span>}
          </div><StatusBadge s={item.status} /></li>)}</ul> : <p className="small muted">No saved scenario runs in this snapshot.</p>}
        </Panel>
        <Panel title="Intervention learning">
          {snapshot.intervention_outcomes?.length ? <ul className="list">{snapshot.intervention_outcomes.map((item) => <li key={item.action_id}><StatusBadge s={item.outcome} /><span className="small" style={{ flex: 1 }}>Action {item.action_id.slice(0, 10)}</span><span className="xs faint">{item.linked_measurement_count} linked measurements · {fmtTime(item.recorded_at)}</span></li>)}</ul> : <p className="small muted">No operator-recorded intervention outcomes in this snapshot.</p>}
          <p className="xs faint">Operator reports are observations for learning, not causal proof. Private outcome notes are excluded.</p>
        </Panel>
      </div>
      <Panel title="Cross-domain findings">
        {snapshot.cross_domain_impacts?.length ? <ul className="list">{snapshot.cross_domain_impacts.map((item, index) => <li key={`${item.task_id}-${item.agent_id}-${index}`} style={{ alignItems: "flex-start" }}><div className="stack" style={{ gap: 5, flex: 1 }}>
          <b className="small">{item.agent_id.replaceAll("_", " ")}</b><span className="xs faint">Task {item.task_id.slice(0, 10)}</span>{item.summary && <span className="small">{item.summary}</span>}
          {item.findings.slice(0, 6).map((finding, i) => <span className="xs muted" key={i}>{finding.title ?? "Finding"}{finding.detail ? `: ${finding.detail}` : ""}</span>)}
          {!!item.data_refs.length && <span className="xs faint">{item.data_refs.length} evidence references recorded · {item.provenance.join(", ") || "provenance unavailable"}</span>}
          {!!item.limitations.length && <span className="xs faint">Limitations: {item.limitations.join(" · ")}</span>}
        </div></li>)}</ul> : <p className="small muted">No completed cross-domain analyses in the recent task snapshot.</p>}
        <p className="xs faint">Observed co-occurrence or model findings do not establish causation.</p>
      </Panel>
      <Panel title="Recent trends">
        {Object.entries(snapshot.trends ?? {}).some(([, series]) => !!series?.points.length) ? <div className="grid cols-2">{Object.entries(snapshot.trends ?? {}).filter((entry): entry is [string, NonNullable<typeof entry[1]>] => !!entry[1]?.points.length).map(([key, series]) => <div key={key} className="panel panel-body">
          <b className="small">{key.replaceAll("_", " ")}</b><div className="xs faint">{series.kind} · {series.provenance} · {series.source_id}</div><span className="small">{series.points[0][1]} to {series.points.at(-1)?.[1]} {series.unit}</span><span className="xs faint">{fmtTime(series.points[0][0])} to {fmtTime(series.points.at(-1)?.[0] ?? "")}</span>
        </div>)}</div> : <p className="small muted">No trend series were available at capture time.</p>}
      </Panel>
      <p className="xs faint">Saved SwarmOS records at capture time. Modelled, simulated, operator-entered and unavailable values retain their original labels. This report does not establish causality or independently verify reports.</p>
    </div>}
  </Panel>;
}

function scenarioFlood(metrics: Record<string, any>): string | null {
  const flood = metrics.flood;
  if (!flood?.baseline || !flood?.scenario) return null;
  return `${flood.baseline.score ?? "—"} → ${flood.scenario.score ?? "—"}${flood.delta == null ? "" : ` (Δ ${flood.delta > 0 ? "+" : ""}${flood.delta})`}`;
}

function reportMarkdown(report: SituationReport): string {
  const snapshot = report.snapshot ?? {};
  const lines = ["# " + report.title, "", "Generated: " + (snapshot.generated_at ?? report.created_at), "",
    "City: " + (snapshot.city?.name ?? report.city_id) + (snapshot.city?.state ? ", " + snapshot.city.state : ""),
    "Reporting period: " + (snapshot.reporting_period?.description ?? "Point-in-time snapshot"), "", "## Indicators", ""];
  for (const item of snapshot.indicators ?? []) {
    lines.push("- **" + item.label + ":** " + (item.status === "available" ? (item.value ?? "—") + " " + (item.unit ?? "") : "Unavailable") +
      "; kind=" + (item.kind ?? "unknown") + "; provenance=" + (item.provenance ?? "unknown") + "; note=" + (item.note ?? "—"));
  }
  lines.push("", "## Infrastructure", "");
  for (const [layer, count] of Object.entries(snapshot.infrastructure?.osm_feature_counts ?? {})) lines.push(`- ${layer}: ${count} mapped features`);
  lines.push("OSM features are community-mapped and may be incomplete.");
  lines.push("", "## Open incidents", "");
  for (const incident of snapshot.incidents?.recent ?? []) lines.push("- " + incident.title + " — " + incident.severity + ", " + incident.status + ", reported " + incident.reported_at);
  lines.push("", "## Platform alerts", "");
  for (const alert of snapshot.alerts ?? []) lines.push(`- ${alert.level}: ${alert.title} — ${alert.body} (${alert.created_at})`);
  lines.push("", "## Recommendations and decisions", "");
  for (const item of snapshot.recommendations ?? []) lines.push(`- ${item.title} — ${item.status}; risk=${item.risk}; rationale=${item.rationale || "not recorded"}; decision=${item.decided_at ?? "not decided"}`);
  lines.push("", "## Scenario comparisons", "");
  for (const item of snapshot.scenario_runs ?? []) lines.push(`- ${item.name} — ${item.status}; inputs=${JSON.stringify(item.inputs)}; ${item.summary || "No summary"}; metrics=${JSON.stringify(item.metrics)}; provenance=${item.provenance.join(", ") || "unknown"}; missing=${item.missing.join(", ") || "none reported"}; limitations=${item.limitations.join(" | ") || "none recorded"}`);
  lines.push("", "## Agent review and consensus", "");
  for (const item of snapshot.agent_reviews ?? []) {
    lines.push(`- Task ${item.task_id}: ${item.status}; ${item.consensus.status}; shared signals=${item.consensus.shared_signals}; material differences=${item.consensus.material_differences}; ${item.summary || "No summary"}`);
    for (const comparison of item.consensus.comparisons) lines.push(`  - ${comparison.kind}: ${comparison.signal}; ${comparison.material_difference ? "material difference flagged for review" : "no material difference detected"}; agents=${comparison.values.map((value) => value.agent_id).join(", ")}`);
  }
  lines.push("", "## Cross-domain findings", "");
  for (const item of snapshot.cross_domain_impacts ?? []) {
    lines.push(`- ${item.agent_id} (task ${item.task_id}): ${item.summary || "No summary"}; provenance=${item.provenance.join(", ") || "unknown"}; limitations=${item.limitations.join(" | ") || "none recorded"}`);
    for (const finding of item.findings) lines.push(`  - ${finding.title ?? "Finding"}${finding.detail ? `: ${finding.detail}` : ""}${finding.severity ? ` [${finding.severity}]` : ""}`);
    lines.push(`  - Evidence references recorded: ${item.data_refs.length}`);
  }
  lines.push("", "## Intervention outcomes", "");
  for (const item of snapshot.intervention_outcomes ?? []) lines.push(`- Action ${item.action_id}: ${item.outcome}, recorded ${item.recorded_at}, linked measurements=${item.linked_measurement_count}; operator note excluded`);
  lines.push("", "## Recent trends", "");
  for (const [key, series] of Object.entries(snapshot.trends ?? {})) {
    if (!series?.points.length) continue;
    lines.push(`- ${key}: ${series.points[0][1]} to ${series.points.at(-1)?.[1]} ${series.unit}; ${series.kind}; provenance=${series.provenance}; source=${series.source_id}; period=${series.points[0][0]} to ${series.points.at(-1)?.[0]}`);
  }
  lines.push("", "## Source health", "");
  for (const source of snapshot.source_health ?? []) lines.push("- " + source.name + ": " + source.status +
    (source.freshness ? "; freshness=" + source.freshness.state + "; age_s=" + (source.freshness.age_s ?? "unknown") : "") +
    "; last_success=" + (source.last_success_at ?? "never") + "; last_attempt=" + (source.last_run?.status ?? "never") +
    (source.last_run?.error ? "; error=" + source.last_run.error : ""));
  lines.push("", "## Limitations", "", "This is a saved snapshot of SwarmOS records. Simulated and modelled results retain their labels. It does not establish causality or independently verify reports. No emergency dispatch or verified personal-safety assessment is provided.", "");
  return lines.join("\n");
}

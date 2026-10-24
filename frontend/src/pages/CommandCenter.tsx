import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { Link } from "react-router-dom";

import { dashboardQuery } from "../api/queries";
import type { Indicator, Severity } from "../api/types";
import { BarList, Sparkline } from "../components/charts";
import { EmptyState, ErrorState, FreshnessBadge, Panel, ProvenanceBadge, SeverityBadge, Skeleton, StatusBadge, Timestamp } from "../components/ui";
import { Segmented, Switch } from "../components/ui/widgets";
import { ago, fmtNum, fmtTime, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";

// MapLibre is large, so the dashboard map is loaded after the rest of the page.
const OpsMap = lazy(() => import("../components/map/OpsMap"));

const ACCENT: Record<string, string> = {
  environment: "var(--cyan)", mobility: "var(--blue)", energy: "var(--amber)", water: "#38bdf8", safety: "var(--red)", civic: "var(--violet)",
};
const TODAY = new Intl.DateTimeFormat("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" });

function Kpi({ ind }: { ind: Indicator }) {
  const unavailable = ind.status === "unavailable";
  return (
    <div className={`panel kpi ${unavailable ? "unavailable" : ""}`} style={{ ["--accent" as string]: ACCENT[ind.domain] }} data-testid={`kpi-${ind.id}`}>
      <div className="row between"><span className="label">{ind.label}</span>{!unavailable && <ProvenanceBadge p={ind.provenance} />}</div>
      {unavailable ? (
        <div className="value">Not available</div>
      ) : (
        <div className="value">{fmtNum(ind.value)}<span className="unit">{ind.unit}</span></div>
      )}
      <div className="foot">
        {unavailable ? <span>{ind.note}</span> : (
          <>
            {ind.kind && ind.kind !== "records" && <span>{titleCase(ind.kind)}</span>}
            {ind.observed_at && <span>· {fmtTime(ind.observed_at)}</span>}
            {ind.note && <span title={ind.note}>· ⓘ</span>}
          </>
        )}
      </div>
    </div>
  );
}

export function CommandCenter() {
  const { cityId, city } = useCity();
  const q = useQuery({
    ...dashboardQuery(cityId),
    refetchInterval: 60_000,
  });
  const d = q.data;
  const [severity, setSeverity] = useState<"all" | Severity>("all");
  const [showIncidents, setShowIncidents] = useState(true);
  const [showFacilities, setShowFacilities] = useState(true);
  const [mode3d, setMode3d] = useState(true);
  const sev = d?.incidents.by_severity;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Command Center — {city?.name}</h1>
          <p>{TODAY.format(new Date())} · Real-time overview · {d ? <>Updated <Timestamp iso={d.generated_at} /></> : "Loading…"}</p>
        </div>
        <div className="row wrap">
          <Link className="btn" to={`/c/${cityId}/map`}><Icon name="map" />Open map</Link>
          <Link className="btn primary" to={`/c/${cityId}/workspace`}><Icon name="workspace" />Ask the agents</Link>
        </div>
      </div>

      {q.error && !d && <ErrorState error={q.error} onRetry={() => void q.refetch()} />}
      {q.error && d && <div className="notice offline"><Icon name="wifiOff" />Showing data from {fmtTime(d.generated_at)} — refresh failed: {(q.error as Error).message}</div>}
      {city?.is_pilot && (
        <div className="notice warn" role="note">
          <Icon name="alert" />
          <span><b>Demo data present.</b> {city.name} is a pilot city with SYNTHETIC fixtures for traffic, water, energy, incidents and complaints. Values tagged <span className="badge b-demo">Demo</span> are not real-world facts.</span>
        </div>
      )}

      <div className="toolbar">
        <Segmented label="Filter incidents on the map" value={severity} onChange={setSeverity} options={[
          { id: "all", label: "All", count: d?.incidents.open },
          { id: "critical", label: "Critical", count: sev?.critical },
          { id: "high", label: "High", count: sev?.high },
          { id: "moderate", label: "Moderate", count: sev?.moderate },
          { id: "low", label: "Low", count: sev?.low },
        ]} />
        <div className="spacer" />
        <Switch label="Incidents" checked={showIncidents} onChange={setShowIncidents} />
        <Switch label="Facilities" checked={showFacilities} onChange={setShowFacilities} />
        <Switch label="3D buildings" checked={mode3d} onChange={setMode3d} testId="dashboard-3d" />
      </div>

      {city && (
        <Suspense fallback={<div className="ops-map"><Skeleton h={560} /></div>}>
          <OpsMap city={city} dashboard={d} severity={severity} showIncidents={showIncidents} showFacilities={showFacilities} mode3d={mode3d} onToggle3d={() => setMode3d((m) => !m)} />
        </Suspense>
      )}

      <div className="grid kpi-grid">
        {d ? d.indicators.map((i) => <Kpi key={i.id} ind={i} />) : Array.from({ length: 10 }, (_, i) => <div key={i} className="panel kpi"><Skeleton h={14} w="50%" /><Skeleton h={30} w="60%" /></div>)}
      </div>

      <div className="grid cols-3">
        <Panel title="Active incidents" icon="incidents" className="span-2" actions={<Link className="btn sm" to={`/c/${cityId}/incidents`}>View all</Link>} flush>
          {!d ? <div className="panel-body"><Skeleton h={120} /></div> : d.incidents.recent.length === 0 ? (
            <EmptyState title="No open incidents">No open incidents are recorded for {city?.name}.</EmptyState>
          ) : (
            <>
              <div className="row wrap" style={{ padding: "10px 14px", gap: 6 }}>
                {(["critical", "high", "moderate", "low"] as const).map((s) => <span key={s} className={`badge sev-${s}`}>{d.incidents.by_severity[s]} {s}</span>)}
              </div>
              <ul className="list">
                {d.incidents.recent.map((i) => (
                  <li key={i.id}>
                    <SeverityBadge s={i.severity} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 500 }}>{i.title}</div>
                      <div className="xs faint">{titleCase(i.category)} · {titleCase(i.status)} · reported {ago(i.reported_at)}</div>
                    </div>
                    <ProvenanceBadge p={i.provenance} />
                  </li>
                ))}
              </ul>
            </>
          )}
        </Panel>

        <Panel title="Data-source health" icon="integrations" actions={<Link className="btn sm" to={`/c/${cityId}/integrations`}>Details</Link>} flush>
          {!d ? <div className="panel-body"><Skeleton h={160} /></div> : (
            <ul className="list">
              {d.source_health.filter((s) => s.applies_to_city || s.scope !== "none").map((s) => (
                <li key={s.id}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="small" style={{ fontWeight: 500 }}>{s.name}</div>
                    <div className="xs faint">{s.categories.join(", ")}</div>
                  </div>
                  <div className="stack" style={{ alignItems: "flex-end", gap: 4 }}>
                    <StatusBadge s={s.applies_to_city ? s.status : "unavailable"} label={s.applies_to_city ? undefined : "Not for this city"} />
                    {s.scope !== "none" && <FreshnessBadge f={s.freshness} />}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <div className="grid cols-4">
        {(["temperature", "pm2_5", "power_demand", "water_demand"] as const).map((k) => {
          const t = d?.trends[k];
          const label = { temperature: "Temperature (48 h)", pm2_5: "PM2.5 model (48 h)", power_demand: "Power demand (48 h)", water_demand: "Water demand (48 h)" }[k];
          return (
            <Panel key={k} title={label} actions={t ? <ProvenanceBadge p={t.provenance} /> : undefined}>
              {!d ? <Skeleton h={40} /> : t ? (
                <>
                  <Sparkline points={t.points} label={label} color={k === "power_demand" ? "var(--amber)" : k === "pm2_5" ? "var(--violet)" : "var(--cyan)"} />
                  <div className="row between xs faint"><span>{fmtNum(t.points.at(-1)?.[1])} {t.unit}</span><span>{t.source_id}</span></div>
                </>
              ) : <div className="small faint">No data source connected for this metric.</div>}
            </Panel>
          );
        })}
      </div>

      <div className="grid cols-3">
        <Panel title="AI agent activity" icon="agents" actions={<Link className="btn sm" to={`/c/${cityId}/agents`}>Fleet</Link>}>
          {!d ? <Skeleton h={100} /> : (
            <div className="stack">
              <div className="row wrap">
                {Object.entries(d.agents.executions_24h).length ? Object.entries(d.agents.executions_24h).map(([s, n]) => <StatusBadge key={s} s={s} label={`${n} ${s}`} />)
                  : <span className="small faint">No agent executions in the last 24 h.</span>}
              </div>
              <ul className="list" style={{ margin: "0 -14px" }}>
                {d.agents.recent_tasks.map((t) => (
                  <li key={t.id}>
                    <StatusBadge s={t.status} />
                    <Link to={`/c/${cityId}/workspace?task=${t.id}`} className="small" style={{ flex: 1, color: "var(--text)" }}>{t.prompt}</Link>
                    <span className="xs faint">{ago(t.created_at)}</span>
                  </li>
                ))}
                {!d.agents.recent_tasks.length && <li className="small faint">No tasks yet — try the AI Workspace.</li>}
              </ul>
            </div>
          )}
        </Panel>
        <Panel title="Mapped public infrastructure" icon="layers">
          {!d ? <Skeleton h={100} /> : Object.keys(d.infrastructure.osm_feature_counts).length ? (
            <>
              <BarList items={Object.entries(d.infrastructure.osm_feature_counts).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: titleCase(k), value: v }))} color="var(--blue)" />
              <p className="xs faint" style={{ marginTop: 8 }}>© OpenStreetMap contributors. Completeness varies by city.</p>
            </>
          ) : <EmptyState title="Not synchronised">Run the OpenStreetMap sync from Data Integrations to load facility locations.</EmptyState>}
          {d && <p className="small" style={{ marginTop: 10 }}>Earthquakes (M≥2.5, 7 days, ≤500 km): <b className="num">{d.infrastructure.earthquakes_7d_500km}</b>{d.infrastructure.strongest_quake ? <> · strongest M{d.infrastructure.strongest_quake.magnitude}</> : null}</p>}
        </Panel>
        <Panel title="Alerts & recent operator actions" icon="shield" flush>
          {!d ? <div className="panel-body"><Skeleton h={100} /></div> : (
            <ul className="list">
              {d.sync_failures.map((s) => <li key={s.id}><span className="badge b-error">Sync failed</span><span className="small">{s.name}: {s.last_run?.error}</span></li>)}
              {d.alerts.slice(0, 4).map((a) => <li key={a.id}><span className={`badge ${a.level === "warning" ? "b-demo" : "b-info"}`}>{a.level}</span><div className="small" style={{ flex: 1 }}>{a.title}<div className="xs faint">{ago(a.created_at)}</div></div></li>)}
              {d.audit.slice(0, 5).map((a, i) => <li key={i}><span className="badge b-records">Audit</span><div className="small" style={{ flex: 1 }}>{a.action} <span className="faint">by {a.actor ?? "system"}</span><div className="xs faint">{ago(a.at)}</div></div></li>)}
              {!d.alerts.length && !d.audit.length && !d.sync_failures.length && <li className="small faint">Nothing to report.</li>}
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api/client";
import type { City, Dashboard, Severity } from "../../api/types";
import { ago, fmtNum, titleCase } from "../../lib/format";
import { Icon } from "../../lib/icons";
import { useTheme } from "../../state/theme";
import { Gauge, Meter } from "../ui/widgets";
import { MapControls, type MapLike, POINT_COLORS } from "./mapkit";
import { type MapPoint, MapplsCanvas } from "./mappls";

type FC = GeoJSON.FeatureCollection<GeoJSON.Point, Record<string, any>>;
const WANTED = ["incidents", "earthquake", "hospital", "fire_station", "police", "traffic_sensors", "water_sensors"];

export interface OpsMapProps {
  city: City;
  dashboard: Dashboard | undefined;
  severity: "all" | Severity;
  showIncidents: boolean;
  showFacilities: boolean;
  mode3d: boolean;
  onToggle3d: () => void;
}

/** Dashboard hero map: MapmyIndia city map (tilts in 3D) with live incident/facility points, round controls and a summary card. */
export default function OpsMap({ city, dashboard: d, severity, showIncidents, showFacilities, mode3d, onToggle3d }: OpsMapProps) {
  const { theme } = useTheme();
  const pal = POINT_COLORS[theme];
  const wrap = useRef<HTMLDivElement>(null);
  const [mpl, setMpl] = useState<MapLike | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const layersQ = useQuery({
    queryKey: ["map-layers", city.id],
    queryFn: () => api<{ layers: { id: string }[] }>(`/cities/${city.id}/map/layers`),
  });
  const layers = (layersQ.data?.layers ?? []).map((l) => l.id).filter((id) => WANTED.includes(id));
  const featQ = useQuery({
    queryKey: ["map-features", city.id, layers.join(",")],
    queryFn: () => api<FC>(`/cities/${city.id}/map/features`, { query: { layers: layers.join(",") } }),
    enabled: layers.length > 0,
    refetchInterval: 120_000,
  });

  useEffect(() => setFailed(null), [city.id]);

  const visible = (p: Record<string, any>) => {
    if (p.layer === "earthquake") return true;
    if (p.layer === "incidents") return showIncidents && (severity === "all" || p.severity === severity);
    return showFacilities;
  };

  const points: MapPoint[] = useMemo(() => (featQ.data?.features ?? []).filter((f) => visible(f.properties)).map((f, i) => {
    const p = f.properties;
    const inc = p.layer === "incidents";
    return {
      id: String(p.id ?? i), lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1],
      color: inc ? pal.sev[p.severity as keyof typeof pal.sev] ?? pal.sev.low : pal.layer[p.layer] ?? pal.fallback,
      size: inc ? 7 : 5, title: String(p.title ?? titleCase(String(p.layer))), subtitle: `${titleCase(String(p.layer))}${p.severity ? ` · ${p.severity}` : ""}`, props: p,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [featQ.data, severity, showIncidents, showFacilities, theme]);

  const ctl: MapLike | null = mpl;
  const ind = (id: string) => d?.indicators.find((i) => i.id === id);
  const aqi = ind("aqi");
  const temp = ind("temperature");
  const sources = (d?.source_health ?? []).filter((s) => s.applies_to_city);
  const healthy = sources.filter((s) => ["connected", "internal", "demo"].includes(s.status)).length;
  const top = d?.incidents.recent.find((i) => i.severity === "critical" || i.severity === "high");
  const failing = d?.sync_failures[0];
  const count = featQ.data?.features.length ?? 0;
  const live = (p?: string) => (p === "live" || p === "cached" ? "Live" : p === "demo" ? "Simulated" : "Ops");

  return (
    <div className="ops-map" ref={wrap}>
      <div className="ops-map-canvas">
        <MapplsCanvas center={{ lat: city.lat, lon: city.lon }} zoom={mode3d ? 16.5 : 13} points={points} mode3d={mode3d} theme={theme} onReady={setMpl}
          label={`MapmyIndia map of ${city.name}`} onFail={(msg) => setFailed(`${msg}. Incidents and indicators are still shown on this page.`)} />
      </div>
      {failed && <div className="notice error ops-map-error"><Icon name="alert" />{failed}</div>}

      <aside className="ops-card" aria-label={`${city.name} summary`}>
        <div className="row between">
          <div>
            <div className="row" style={{ gap: 8 }}>
              <h2>{city.name}</h2>
              <span className={`badge ${city.has_live_data ? "b-live" : city.has_demo_data ? "b-demo" : "b-unavailable"}`}><span className="dot" />{city.has_live_data ? "Live" : city.has_demo_data ? "Simulated" : "No live data"}</span>
            </div>
            <div className="xs faint">{city.state} · {city.lat.toFixed(3)}°N, {city.lon.toFixed(3)}°E</div>
          </div>
        </div>
        <div className="tile">
          <div className="row between small"><b>Data sources healthy</b><span><b className="num">{healthy}</b><span className="faint"> / {sources.length}</span></span></div>
          <Meter value={healthy} max={sources.length || 1} label="Healthy data sources" />
          <div className="row between xs faint"><span><Icon name="refresh" width={12} /> Updated {d ? ago(d.generated_at) : "…"}</span><span>{count} points on map</span></div>
        </div>
        <div className="grid cols-2" style={{ gap: 8 }}>
          <div className="tile">
            <div className="row between xs"><span className="muted">Air quality</span>{aqi?.status === "available" && <span className={`badge ${aqi.provenance === "demo" ? "b-demo" : "b-live"}`}>{aqi.unit || "AQI"}</span>}</div>
            <Gauge value={aqi?.status === "available" ? Number(aqi.value) : null} max={500} label="Indicative AQI" unit="AQI" />
          </div>
          <div className="tile">
            {temp?.status === "available" ? (
              <>
                <div className="row between xs"><span className="muted">Temperature</span><span className={`badge ${temp.provenance === "demo" ? "b-demo" : "b-live"}`}>{live(temp.provenance)}</span></div>
                <Gauge value={Number(temp.value)} max={50} label="Temperature" unit={temp.unit ?? "°C"} display={fmtNum(temp.value)} bands={["var(--cyan)", "var(--amber)", "var(--red)"]} />
              </>
            ) : (
              <>
                <div className="row between xs"><span className="muted">Open incidents</span><span className="badge b-operator">Ops</span></div>
                <Gauge value={d?.incidents.open ?? null} max={Math.max(10, (d?.incidents.open ?? 0) * 2)} label="Open incidents" unit="open" />
              </>
            )}
          </div>
        </div>
        <div className={`strip ${top || failing ? "warn" : "ok"}`}>
          <Icon name={top || failing ? "alert" : "check"} width={16} />
          {top ? <span><b>{titleCase(top.severity)}:</b> {top.title}</span>
            : failing ? <span><b>Sync failing:</b> {failing.name}</span>
              : <span><b>All clear.</b> No high-severity incidents.</span>}
        </div>
      </aside>

      <MapControls map={ctl} mode3d={mode3d} onToggle3d={onToggle3d} onFit={() => ctl?.fitBounds(city.bbox as [number, number, number, number], { padding: 30, duration: 900 })} fullscreenTarget={wrap} />
      <div className="ops-map-foot xs faint"><span>© MapmyIndia · Scroll to zoom · 3D tilts the view</span><span>Points refresh every 2 minutes</span></div>
    </div>
  );
}

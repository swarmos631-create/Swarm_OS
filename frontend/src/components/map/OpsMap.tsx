import { useQuery } from "@tanstack/react-query";
import type { GeoJSONSource } from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api/client";
import type { City, Dashboard, Severity } from "../../api/types";
import { ago, fmtNum, titleCase } from "../../lib/format";
import { Icon } from "../../lib/icons";
import { useTheme } from "../../state/theme";
import { EmptyState } from "../ui";
import { Gauge, Meter } from "../ui/widgets";
import { Compass, ensureBuildings, MapControls, type MapLike, maplibregl, PITCH_3D, POINT_COLORS, set3D, styleFor, webglAvailable } from "./mapkit";
import { BaseSwitch, type MapPoint, MapplsCanvas, useBaseMap } from "./mappls";

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

/** Dashboard hero map: tilted 3D city with live incident/facility points, round controls, compass and a summary card. */
export default function OpsMap({ city, dashboard: d, severity, showIncidents, showFacilities, mode3d, onToggle3d }: OpsMapProps) {
  const { theme } = useTheme();
  const pal = POINT_COLORS[theme];
  const [base, setBase, fallbackTo] = useBaseMap();
  const wrap = useRef<HTMLDivElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<maplibregl.Map | null>(null);
  const [mpl, setMpl] = useState<MapLike | null>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const hasGL = useMemo(webglAvailable, []);

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

  useEffect(() => {
    if (!container.current || !hasGL || base !== "osm") return;
    let m: maplibregl.Map;
    try {
      m = new maplibregl.Map({
        container: container.current, style: styleFor(theme), center: [city.lon, city.lat], zoom: 15.3,
        pitch: mode3d ? PITCH_3D : 0, bearing: mode3d ? -20 : 0, maxBounds: [[60, 2], [101, 40]], attributionControl: { compact: true },
      });
    } catch {
      setFailed("Base map could not start.");
      return;
    }
    m.on("error", (e: maplibregl.ErrorEvent) => { if (/style|Failed to fetch|NetworkError/i.test(e?.error?.message ?? "")) setFailed("Base map tiles could not be loaded. Data points are still listed on the Operations Map page."); });
    m.on("load", () => {
      ensureBuildings(m, theme);
      m.setLayoutProperty("buildings-3d", "visibility", mode3d ? "visible" : "none");
      m.addSource("ops", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      const color: maplibregl.ExpressionSpecification = ["case",
        ["==", ["get", "layer"], "incidents"], ["match", ["get", "severity"], "critical", pal.sev.critical, "high", pal.sev.high, "moderate", pal.sev.moderate, pal.sev.low],
        ["match", ["get", "layer"], ...Object.entries(pal.layer).flat(), pal.fallback] as unknown as maplibregl.ExpressionSpecification];
      m.addLayer({ id: "ops-halo", type: "circle", source: "ops", paint: { "circle-radius": 18, "circle-color": color, "circle-opacity": 0.2, "circle-blur": 0.6 } });
      m.addLayer({
        id: "ops-points", type: "circle", source: "ops",
        paint: { "circle-radius": ["case", ["==", ["get", "layer"], "incidents"], 7, 5], "circle-color": color, "circle-stroke-color": pal.stroke, "circle-stroke-width": 2 },
      });
      m.on("click", "ops-points", (e: maplibregl.MapLayerMouseEvent) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties as Record<string, any>;
        new maplibregl.Popup({ closeButton: false, offset: 12, className: "ops-popup" })
          .setLngLat((f.geometry as GeoJSON.Point).coordinates as [number, number])
          .setHTML(`<b>${String(p.title ?? titleCase(String(p.layer))).replace(/</g, "&lt;")}</b><br><span>${titleCase(String(p.layer))}${p.severity ? ` · ${p.severity}` : ""}</span>`)
          .addTo(m);
      });
      m.on("mouseenter", "ops-points", () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", "ops-points", () => (m.getCanvas().style.cursor = ""));
      setReady(true);
    });
    setMap(m);
    return () => { m.remove(); setMap(null); setReady(false); };
    // The map is rebuilt per city, theme and base map; the 3D mode is applied by the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasGL, city.id, theme, base]);

  useEffect(() => {
    if (map && ready) set3D(map, mode3d, true, theme);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- theme changes rebuild the map, which re-applies 3D
  }, [map, ready, mode3d]);

  useEffect(() => {
    if (!map || !ready || !map.getStyle()) return;
    (map.getSource("ops") as GeoJSONSource | undefined)?.setData(featQ.data ?? { type: "FeatureCollection", features: [] });
  }, [map, ready, featQ.data]);

  const visible = (p: Record<string, any>) => {
    if (p.layer === "earthquake") return true;
    if (p.layer === "incidents") return showIncidents && (severity === "all" || p.severity === severity);
    return showFacilities;
  };

  useEffect(() => {
    if (!map || !ready || !map.getStyle()) return;
    const isInc: maplibregl.ExpressionSpecification = ["==", ["get", "layer"], "incidents"];
    const parts: maplibregl.ExpressionSpecification[] = [["==", ["get", "layer"], "earthquake"]];
    if (showIncidents) parts.push(severity === "all" ? isInc : ["all", isInc, ["==", ["get", "severity"], severity]]);
    if (showFacilities) parts.push(["!", ["in", ["get", "layer"], ["literal", ["incidents", "earthquake"]]]]);
    const filter = ["any", ...parts] as maplibregl.FilterSpecification;
    map.setFilter("ops-points", filter);
    map.setFilter("ops-halo", filter);
  }, [map, ready, severity, showIncidents, showFacilities]);

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

  if (!hasGL && base === "osm") return <EmptyState title="3D graphics unavailable" icon="map">This browser does not support WebGL, which the map needs.</EmptyState>;

  const ctl: MapLike | null = base === "osm" ? map : mpl;
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
      {base === "osm" ? (
        <div ref={container} className="ops-map-canvas" role="region" aria-label={`3D map of ${city.name}`} />
      ) : (
        <div className="ops-map-canvas">
          <MapplsCanvas center={{ lat: city.lat, lon: city.lon }} zoom={mode3d ? 16.5 : 13} points={points} mode3d={mode3d} theme={theme} onReady={setMpl}
            label={`MapmyIndia map of ${city.name}`} onFail={(msg) => { setFailed(`${msg}. Showing the OpenFreeMap 3D map instead.`); fallbackTo("osm"); }} />
        </div>
      )}
      {failed && <div className="notice error ops-map-error"><Icon name="alert" />{failed}</div>}
      <BaseSwitch base={base} onChange={(b) => { setFailed(null); setBase(b); }} className="map-source" />

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

      {base === "osm" && <Compass map={ctl} />}
      <MapControls map={ctl} mode3d={mode3d} onToggle3d={onToggle3d} onFit={() => ctl?.fitBounds(city.bbox as [number, number, number, number], { padding: 30, duration: 900 })} fullscreenTarget={wrap} />
      <div className="ops-map-foot xs faint"><span>{base === "osm" ? "Drag to pan · Right-drag to tilt and rotate · Scroll to zoom" : "© MapmyIndia · Scroll to zoom · 3D tilts the view"}</span><span>Points refresh every 2 minutes</span></div>
    </div>
  );
}

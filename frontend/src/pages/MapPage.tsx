import "maplibre-gl/dist/maplibre-gl.css";

import { useQuery } from "@tanstack/react-query";
import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource, MapGeoJSONFeature } from "maplibre-gl";
// MapLibre v6 resolves its worker relative to its own module URL, which bundling breaks; ship it as an asset instead.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api/client";
import { EmptyState, ErrorState, ProvenanceBadge, SeverityBadge } from "../components/ui";
import { fmtTime, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useOnline } from "../state/ui";

maplibregl.setWorkerUrl(workerUrl);

const STYLE_URL = import.meta.env.VITE_MAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/dark";
const DEFAULT_LAYERS = ["incidents", "earthquake", "hospital", "fire_station", "traffic_sensors", "water_sensors"];

const LAYER_COLORS: Record<string, string> = {
  earthquake: "#f43f5e", hospital: "#34d399", clinic: "#6ee7b7", fire_station: "#fb923c", police: "#60a5fa", school: "#c4b5fd",
  bus_station: "#38bdf8", rail_station: "#22d3ee", water_tower: "#7dd3fc", substation: "#fde047", public_toilet: "#a3a3a3",
  asset_bridge: "#e879f9", asset_road: "#d946ef", traffic_sensors: "#3b82f6", water_sensors: "#06b6d4",
};
// MapLibre "match" expression: layer id -> colour, with a neutral fallback.
const LAYER_MATCH = ["match", ["get", "layer"], ...Object.entries(LAYER_COLORS).flat(), "#94a3b8"] as unknown as maplibregl.ExpressionSpecification;
const SEV_COLORS = { low: "#60a5fa", moderate: "#fbbf24", high: "#fb923c", critical: "#f43f5e" };

interface Layer { id: string; label: string; group: string; count: number; provenance: (string | null)[] }
type FC = GeoJSON.FeatureCollection<GeoJSON.Point, Record<string, any>>;

function webglAvailable(): boolean {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch {
    return false;
  }
}

export default function MapPage() {
  const { cityId, city } = useCity();
  const online = useOnline();
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>(DEFAULT_LAYERS);
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [mode3d, setMode3d] = useState(false);
  const [search, setSearch] = useState("");
  const hasGL = useMemo(webglAvailable, []);

  const layersQ = useQuery({ queryKey: ["map-layers", cityId], queryFn: () => api<{ layers: Layer[]; note: string }>(`/cities/${cityId}/map/layers`) });
  const available = layersQ.data?.layers ?? [];
  const active = selected.filter((s) => available.some((l) => l.id === s));
  const featQ = useQuery({
    queryKey: ["map-features", cityId, active.join(",")],
    queryFn: () => api<FC>(`/cities/${cityId}/map/features`, { query: { layers: active.join(",") } }),
    enabled: active.length > 0,
  });

  // Create the map once.
  useEffect(() => {
    if (!container.current || !hasGL || !city) return;
    let map: maplibregl.Map;
    try {
      map = new maplibregl.Map({
        container: container.current,
        style: STYLE_URL,
        center: [city.lon, city.lat],
        zoom: city.zoom,
        maxBounds: [[60, 2], [101, 40]],
        attributionControl: { compact: true },
        cooperativeGestures: false,
      });
    } catch (e) {
      setMapError(`Map could not start: ${(e as Error).message}`);
      return;
    }
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.on("error", (e: maplibregl.ErrorEvent) => {
      const msg = e?.error?.message ?? "";
      if (/style|Failed to fetch|NetworkError/i.test(msg)) setMapError("Base map tiles could not be loaded (offline or tile server unavailable). Data layers below may still be listed.");
    });
    map.on("load", () => {
      map.addSource("ops", { type: "geojson", data: { type: "FeatureCollection", features: [] }, cluster: true, clusterRadius: 46, clusterMaxZoom: 13 });
      map.addLayer({
        id: "clusters", type: "circle", source: "ops", filter: ["has", "point_count"],
        paint: {
          "circle-color": "rgba(34,211,238,0.22)", "circle-stroke-color": "#22d3ee", "circle-stroke-width": 1.5,
          "circle-radius": ["step", ["get", "point_count"], 14, 20, 19, 100, 25],
        },
      });
      map.addLayer({
        id: "cluster-count", type: "symbol", source: "ops", filter: ["has", "point_count"],
        layout: { "text-field": ["get", "point_count_abbreviated"], "text-font": ["Noto Sans Regular"], "text-size": 11 },
        paint: { "text-color": "#e6edf7" },
      });
      map.addLayer({
        id: "points", type: "circle", source: "ops", filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-radius": ["case", ["==", ["get", "layer"], "incidents"], 7, ["==", ["get", "layer"], "earthquake"], ["+", 3, ["*", 1.6, ["coalesce", ["get", "magnitude"], 3]]], 5],
          "circle-color": ["case",
            ["==", ["get", "layer"], "incidents"], ["match", ["get", "severity"], "critical", SEV_COLORS.critical, "high", SEV_COLORS.high, "moderate", SEV_COLORS.moderate, SEV_COLORS.low],
            LAYER_MATCH],
          "circle-stroke-color": ["case", ["==", ["get", "provenance"], "demo"], "#fbbf24", "#0b1220"],
          "circle-stroke-width": ["case", ["==", ["get", "provenance"], "demo"], 2, 1.2],
          "circle-opacity": 0.92,
        },
      });
      map.on("click", "clusters", async (e: maplibregl.MapLayerMouseEvent) => {
        const f = map.queryRenderedFeatures(e.point, { layers: ["clusters"] })[0];
        const src = map.getSource("ops") as GeoJSONSource;
        const zoom = await src.getClusterExpansionZoom(f.properties.cluster_id);
        map.easeTo({ center: (f.geometry as GeoJSON.Point).coordinates as [number, number], zoom });
      });
      map.on("click", "points", (e: maplibregl.MapLayerMouseEvent) => {
        const f = e.features?.[0] as MapGeoJSONFeature | undefined;
        if (f) setDetail({ ...f.properties, _coords: (f.geometry as GeoJSON.Point).coordinates });
      });
      for (const l of ["clusters", "points"]) {
        map.on("mouseenter", l, () => (map.getCanvas().style.cursor = "pointer"));
        map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
      }
      setReady(true);
    });
    return () => { map.remove(); mapRef.current = null; setReady(false); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasGL, city?.id ? "has-city" : "no-city"]);

  // City switch: move the viewport.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !city) return;
    setDetail(null);
    map.fitBounds(city.bbox as [number, number, number, number], { padding: 20, duration: 900 });
  }, [city]);

  // Push data into the clustered source.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource("ops") as GeoJSONSource | undefined;
    src?.setData(active.length ? (featQ.data ?? { type: "FeatureCollection", features: [] }) : { type: "FeatureCollection", features: [] });
  }, [featQ.data, ready, active.length]);

  // 2D / 3D buildings
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const hasSource = !!map.getSource("openmaptiles");
    if (mode3d && hasSource && !map.getLayer("buildings-3d")) {
      map.addLayer({
        id: "buildings-3d", type: "fill-extrusion", source: "openmaptiles", "source-layer": "building", minzoom: 13,
        paint: {
          "fill-extrusion-color": ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 0], 0, "#13213b", 40, "#1e3a5f", 120, "#2c5b8a"],
          "fill-extrusion-height": ["coalesce", ["get", "render_height"], 0],
          "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
          "fill-extrusion-opacity": 0.85,
        },
      }, "clusters");
      map.easeTo({ pitch: 58, bearing: -18, zoom: Math.max(map.getZoom(), 14.5), duration: 900 });
    } else if (!mode3d && map.getLayer("buildings-3d")) {
      map.removeLayer("buildings-3d");
      map.easeTo({ pitch: 0, bearing: 0, duration: 700 });
    }
  }, [mode3d, ready]);

  const searchHits = useMemo(() => {
    const s = search.trim().toLowerCase();
    if (s.length < 2) return [];
    return (featQ.data?.features ?? []).filter((f: GeoJSON.Feature<GeoJSON.Point, Record<string, any>>) => String(f.properties.title ?? "").toLowerCase().includes(s)).slice(0, 8);
  }, [search, featQ.data]);

  const toggle = (id: string) => setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  const groups = Array.from(new Set(available.map((l) => l.group)));

  if (!hasGL) {
    return (
      <EmptyState title="3D graphics unavailable" icon="map">
        Your browser or device does not support WebGL, which the interactive map requires. City data remains available on the Command Center, Incidents and Analytics pages.
      </EmptyState>
    );
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Operations Map — {city?.name}</h1>
          <p>Clustered city layers from synchronised sources. Base map tiles carry no sensor data.</p>
        </div>
        <div className="row wrap">
          <div className="row" role="group" aria-label="Map mode">
            <button className="chip" aria-pressed={!mode3d} onClick={() => setMode3d(false)}><Icon name="map" width={14} />2D</button>
            <button className="chip" aria-pressed={mode3d} onClick={() => setMode3d(true)} data-testid="map-3d"><Icon name="cube" width={14} />3D buildings</button>
          </div>
        </div>
      </div>
      {mode3d && <div className="notice info"><Icon name="info" />3D view extrudes OpenStreetMap building footprints using mapped heights where available (zoom 13+). It is illustrative — not a survey-grade digital twin.</div>}
      {!online && <div className="notice offline"><Icon name="wifiOff" />Offline: only previously cached map tiles can be shown.</div>}
      {mapError && <div className="notice error"><Icon name="alert" />{mapError}</div>}

      <div className="map-wrap" data-testid="ops-map">
        <div ref={container} style={{ position: "absolute", inset: 0 }} aria-label={`Interactive map of ${city?.name}`} role="region" />
        <div className="map-overlay map-layers">
          <div className="field" style={{ marginBottom: 8, position: "relative" }}>
            <label className="sr-only" htmlFor="map-search">Search features</label>
            <input id="map-search" className="input" placeholder="Search loaded features…" value={search} onChange={(e) => setSearch(e.target.value)} />
            {searchHits.length > 0 && (
              <ul className="list panel popover" style={{ position: "absolute", top: 38, left: 0, right: 0, zIndex: 3 }}>
                {searchHits.map((f: GeoJSON.Feature<GeoJSON.Point, Record<string, any>>, i: number) => (
                  <li key={i} style={{ cursor: "pointer", padding: "6px 10px" }} onClick={() => {
                    mapRef.current?.flyTo({ center: f.geometry.coordinates as [number, number], zoom: 15 });
                    setDetail({ ...f.properties, _coords: f.geometry.coordinates });
                    setSearch("");
                  }}>
                    <span className="small">{f.properties.title}</span><span className="xs faint" style={{ marginLeft: "auto" }}>{titleCase(f.properties.layer)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="upper faint" style={{ margin: "4px 2px 6px" }}>Layers</div>
          {layersQ.isLoading && <div className="small faint">Loading layers…</div>}
          {layersQ.error && <ErrorState error={layersQ.error} onRetry={() => void layersQ.refetch()} />}
          {groups.map((g) => (
            <div key={g} style={{ marginBottom: 8 }}>
              <div className="xs faint" style={{ margin: "2px 2px 4px" }}>{titleCase(g)}</div>
              {available.filter((l) => l.group === g).map((l) => (
                <label key={l.id} className="row small" style={{ padding: "3px 2px", cursor: "pointer" }}>
                  <input type="checkbox" checked={selected.includes(l.id)} onChange={() => toggle(l.id)} />
                  <i style={{ width: 9, height: 9, borderRadius: 3, background: l.id === "incidents" ? SEV_COLORS.high : LAYER_COLORS[l.id] ?? "#94a3b8", display: "inline-block" }} />
                  <span style={{ flex: 1 }}>{l.label}</span>
                  <span className="num xs faint">{l.count}</span>
                  {l.provenance.includes("demo") && <span className="badge b-demo" style={{ height: 16 }}>demo</span>}
                </label>
              ))}
            </div>
          ))}
          {!layersQ.isLoading && available.length <= 1 && (
            <p className="xs faint">Only incidents are available for this city. Run the OpenStreetMap sync from Data Integrations to add facility layers.</p>
          )}
          {featQ.isFetching && <div className="xs faint">Loading features…</div>}
        </div>

        {detail && (
          <div className="map-overlay map-detail" role="dialog" aria-label="Feature details">
            <div className="row between" style={{ marginBottom: 8 }}>
              <b>{detail.title}</b>
              <button className="btn ghost icon sm" onClick={() => setDetail(null)} aria-label="Close details"><Icon name="close" /></button>
            </div>
            <div className="row wrap" style={{ marginBottom: 8 }}>
              <span className="badge b-info">{titleCase(String(detail.layer))}</span>
              {detail.severity && <SeverityBadge s={detail.severity} />}
              <ProvenanceBadge p={detail.provenance} />
            </div>
            <dl className="kv">
              {Object.entries(detail).filter(([k]) => !["title", "layer", "provenance", "_coords", "severity", "id"].includes(k)).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <dt>{titleCase(k)}</dt>
                  <dd>{k === "url" ? <a href={String(v)} target="_blank" rel="noreferrer noopener">Source ↗</a> : /_at$/.test(k) ? fmtTime(String(v)) : String(v)}</dd>
                </div>
              ))}
              <dt>Location</dt><dd className="num">{detail._coords?.[1]?.toFixed(5)}, {detail._coords?.[0]?.toFixed(5)}</dd>
            </dl>
          </div>
        )}

        <div className="map-overlay map-legend legend hide-sm">
          {Object.entries(SEV_COLORS).map(([k, c]) => <span key={k}><i style={{ background: c, borderRadius: "50%" }} />{k}</span>)}
          <span><i style={{ background: "transparent", border: "2px solid #fbbf24", borderRadius: "50%" }} />demo data</span>
        </div>
      </div>
    </>
  );
}

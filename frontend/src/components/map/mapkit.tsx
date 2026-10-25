import "maplibre-gl/dist/maplibre-gl.css";

import * as maplibregl from "maplibre-gl";
// MapLibre v6 resolves its worker relative to its own module URL, which bundling breaks; ship it as an asset instead.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useState } from "react";

import { Icon } from "../../lib/icons";

maplibregl.setWorkerUrl(workerUrl);

export { maplibregl };
const STYLE_OVERRIDE = import.meta.env.VITE_MAP_STYLE_URL as string | undefined;
export const STYLE_URL = STYLE_OVERRIDE ?? "https://tiles.openfreemap.org/styles/dark";
export type MapTheme = "light" | "dark";
/** OpenFreeMap base style per theme: "dark" at night, the colourful "liberty" style in light mode. */
export const styleFor = (theme: MapTheme) => STYLE_OVERRIDE ?? `https://tiles.openfreemap.org/styles/${theme === "dark" ? "dark" : "liberty"}`;

/** The subset of map methods the shared controls need; satisfied by MapLibre and by the Mappls adapter. */
export type MapLike = Pick<maplibregl.Map, "zoomIn" | "zoomOut" | "easeTo" | "getBearing" | "on" | "off" | "fitBounds" | "flyTo">;

/** Vivid point colours (MapLibre paint needs literal colours, so these mirror the CSS tokens per theme). */
export const POINT_COLORS: Record<MapTheme, { sev: Record<"low" | "moderate" | "high" | "critical", string>; layer: Record<string, string>; stroke: string; demo: string; fallback: string }> = {
  dark: {
    sev: { low: "#22d3ee", moderate: "#fbbf24", high: "#fb923c", critical: "#ff4d6d" },
    layer: {
      earthquake: "#ff4d6d", hospital: "#4ade80", clinic: "#86efac", fire_station: "#fb923c", police: "#5b8cff", school: "#c084fc",
      bus_station: "#38bdf8", rail_station: "#22d3ee", water_tower: "#67e8f9", substation: "#facc15", public_toilet: "#a8a29e",
      asset_bridge: "#f472b6", asset_road: "#e879f9", traffic_sensors: "#818cf8", water_sensors: "#2dd4bf",
    },
    stroke: "#0c0e1f", demo: "#fbbf24", fallback: "#94a3b8",
  },
  light: {
    sev: { low: "#0891b2", moderate: "#d97706", high: "#ea580c", critical: "#e11d48" },
    layer: {
      earthquake: "#e11d48", hospital: "#16a34a", clinic: "#22c55e", fire_station: "#ea580c", police: "#2563eb", school: "#9333ea",
      bus_station: "#0284c7", rail_station: "#0891b2", water_tower: "#0e7490", substation: "#ca8a04", public_toilet: "#78716c",
      asset_bridge: "#db2777", asset_road: "#c026d3", traffic_sensors: "#4f46e5", water_sensors: "#0d9488",
    },
    stroke: "#ffffff", demo: "#d97706", fallback: "#64748b",
  },
};

const BUILDING_RAMP: Record<MapTheme, string[]> = {
  dark: ["#262a4d", "#33386b", "#4b3f8f", "#7a4bb8", "#c04fa0"],
  light: ["#e3e5f4", "#d2d0f3", "#bdb0f2", "#d79be6", "#f29ac4"],
};
export const BUILDINGS_LAYER = "buildings-3d";
export const PITCH_3D = 60;

export function webglAvailable(): boolean {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch {
    return false;
  }
}

/** Find the vector source that carries OpenMapTiles' "building" layer (named "openmaptiles" in OpenFreeMap styles). */
function buildingSource(map: maplibregl.Map): string | null {
  const style = map.getStyle();
  if (!style) return null;
  if (style.sources?.openmaptiles) return "openmaptiles";
  for (const [id, src] of Object.entries(style.sources ?? {})) {
    if (src.type === "vector") return id;
  }
  return null;
}

/**
 * Extruded OpenStreetMap buildings. Heights come from OSM `render_height` where mapped (otherwise a low default so
 * the city still reads as 3D). Colours run from indigo to violet to pink as buildings get taller.
 */
export function ensureBuildings(map: maplibregl.Map, theme: MapTheme = "dark", beforeId?: string): boolean {
  // A map that was just removed (theme or city switch) has no style any more.
  if (!map.getStyle()) return false;
  if (map.getLayer(BUILDINGS_LAYER)) return true;
  const source = buildingSource(map);
  if (!source) return false;
  // Some styles (e.g. "liberty") ship their own extruded buildings; hide them so ours (with the colour ramp) is the only one.
  for (const l of map.getStyle().layers ?? []) {
    if (l.type === "fill-extrusion" && l.id !== BUILDINGS_LAYER) map.setLayoutProperty(l.id, "visibility", "none");
  }
  const ramp = BUILDING_RAMP[theme];
  map.setLight({ anchor: "viewport", color: "#ffffff", intensity: theme === "dark" ? 0.4 : 0.25, position: [1.4, 200, 35] });
  map.addLayer({
    id: BUILDINGS_LAYER, type: "fill-extrusion", source, "source-layer": "building", minzoom: 12.5,
    layout: { visibility: "none" },
    paint: {
      "fill-extrusion-color": ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 6],
        0, ramp[0], 15, ramp[1], 35, ramp[2], 70, ramp[3], 140, ramp[4]],
      "fill-extrusion-height": ["interpolate", ["linear"], ["zoom"], 12.5, 0, 14, ["coalesce", ["get", "render_height"], 6]],
      "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
      "fill-extrusion-opacity": 0.92,
      "fill-extrusion-vertical-gradient": true,
    },
  }, beforeId && map.getLayer(beforeId) ? beforeId : undefined);
  return true;
}

/** Switch between flat 2D and tilted 3D-buildings view. */
export function set3D(map: maplibregl.Map, on: boolean, animate = true, theme: MapTheme = "dark") {
  if (!map.getStyle()) return;
  if (on) ensureBuildings(map, theme);
  if (map.getLayer(BUILDINGS_LAYER)) map.setLayoutProperty(BUILDINGS_LAYER, "visibility", on ? "visible" : "none");
  const opts = { duration: animate ? 900 : 0 };
  if (on) map.easeTo({ pitch: PITCH_3D, bearing: map.getBearing() || -20, zoom: Math.max(map.getZoom(), 14.2), ...opts });
  else map.easeTo({ pitch: 0, bearing: 0, ...opts });
}

const HEADINGS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
export const headingLabel = (bearing: number) => HEADINGS[Math.round((((bearing % 360) + 360) % 360) / 45) % 8];

/** Track the map's bearing for the compass. */
export function useBearing(map: MapLike | null): number {
  const [b, setB] = useState(0);
  useEffect(() => {
    if (!map) return;
    const on = () => setB(map.getBearing());
    on();
    map.on("rotate", on);
    return () => { map.off("rotate", on); };
  }, [map]);
  return b;
}

/** Dial compass: the ring turns with the map, the centre names the current heading. Click to face north. */
export function Compass({ map }: { map: MapLike | null }) {
  const bearing = useBearing(map);
  const ticks = Array.from({ length: 60 }, (_, i) => i * 6);
  return (
    <button type="button" className="compass" onClick={() => map?.easeTo({ bearing: 0, duration: 600 })} aria-label={`Heading ${headingLabel(bearing)}. Reset to north`} title="Reset to north">
      <svg viewBox="0 0 120 120" aria-hidden="true">
        <circle cx="60" cy="60" r="57" fill="var(--surface)" stroke="var(--line-strong)" strokeWidth="2" />
        <g transform={`rotate(${-bearing} 60 60)`}>
          {ticks.map((t) => (
            <line key={t} x1="60" y1="9" x2="60" y2={t % 30 === 0 ? 17 : 13} stroke={t === 0 ? "var(--red)" : t % 90 === 0 ? "var(--accent)" : "var(--tick)"} strokeWidth={t % 30 === 0 ? 2 : 1} transform={`rotate(${t} 60 60)`} />
          ))}
          {["N", "E", "S", "W"].map((l, i) => (
            <text key={l} x="60" y="29" textAnchor="middle" fontSize="9" fontWeight="600" fill={l === "N" ? "var(--red)" : "var(--text-2)"} transform={`rotate(${i * 90} 60 60)`}>{l}</text>
          ))}
        </g>
        <circle cx="60" cy="60" r="27" fill="var(--surface-2)" stroke="var(--line)" />
        <text x="60" y="65" textAnchor="middle" fontSize="15" fontWeight="600" fill="var(--text)">{headingLabel(bearing)}</text>
      </svg>
    </button>
  );
}

/** Round floating map buttons (zoom, north, 2D/3D, fit city, fullscreen) stacked on the right edge. */
export function MapControls({ map, mode3d, onToggle3d, onFit, fullscreenTarget, testId }: {
  map: MapLike | null; mode3d: boolean; onToggle3d: () => void; onFit?: () => void; fullscreenTarget?: React.RefObject<HTMLElement | null>; testId?: string;
}) {
  const fs = () => {
    const el = fullscreenTarget?.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void el.requestFullscreen?.();
  };
  return (
    <div className="map-ctrls" role="toolbar" aria-label="Map controls">
      <button type="button" className={`map-btn label ${mode3d ? "on" : ""}`} aria-pressed={mode3d} onClick={onToggle3d} title={mode3d ? "Switch to flat 2D" : "Show 3D buildings"} data-testid={testId}>
        {mode3d ? "3D" : "2D"}
      </button>
      <button type="button" className="map-btn" onClick={() => map?.zoomIn()} aria-label="Zoom in" title="Zoom in"><Icon name="zoomIn" /></button>
      <button type="button" className="map-btn" onClick={() => map?.zoomOut()} aria-label="Zoom out" title="Zoom out"><Icon name="zoomOut" /></button>
      <button type="button" className="map-btn" onClick={() => map?.easeTo({ bearing: (map.getBearing() || 0) - 45, duration: 500 })} aria-label="Rotate view" title="Rotate"><Icon name="refresh" /></button>
      {onFit && <button type="button" className="map-btn" onClick={onFit} aria-label="Fit city" title="Fit city"><Icon name="target" /></button>}
      {fullscreenTarget && <button type="button" className="map-btn" onClick={fs} aria-label="Full screen" title="Full screen"><Icon name="expand" /></button>}
    </div>
  );
}

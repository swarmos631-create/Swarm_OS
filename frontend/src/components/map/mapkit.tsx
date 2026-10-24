import "maplibre-gl/dist/maplibre-gl.css";

import * as maplibregl from "maplibre-gl";
// MapLibre v6 resolves its worker relative to its own module URL, which bundling breaks; ship it as an asset instead.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useState } from "react";

import { Icon } from "../../lib/icons";

maplibregl.setWorkerUrl(workerUrl);

export { maplibregl };
export const STYLE_URL = import.meta.env.VITE_MAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/dark";
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
  if (style.sources?.openmaptiles) return "openmaptiles";
  for (const [id, src] of Object.entries(style.sources ?? {})) {
    if (src.type === "vector") return id;
  }
  return null;
}

/**
 * Extruded OpenStreetMap buildings. Heights come from OSM `render_height` where mapped (otherwise a low default so
 * the city still reads as 3D). Colours are graphite tones with a soft highlight for taller blocks.
 */
export function ensureBuildings(map: maplibregl.Map, beforeId?: string): boolean {
  if (map.getLayer(BUILDINGS_LAYER)) return true;
  const source = buildingSource(map);
  if (!source) return false;
  map.setLight({ anchor: "viewport", color: "#ffffff", intensity: 0.35, position: [1.4, 200, 35] });
  map.addLayer({
    id: BUILDINGS_LAYER, type: "fill-extrusion", source, "source-layer": "building", minzoom: 12.5,
    layout: { visibility: "none" },
    paint: {
      "fill-extrusion-color": ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 6],
        0, "#26272b", 20, "#303136", 60, "#3b3d43", 150, "#4b4e55"],
      "fill-extrusion-height": ["interpolate", ["linear"], ["zoom"], 12.5, 0, 14, ["coalesce", ["get", "render_height"], 6]],
      "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
      "fill-extrusion-opacity": 0.92,
      "fill-extrusion-vertical-gradient": true,
    },
  }, beforeId && map.getLayer(beforeId) ? beforeId : undefined);
  return true;
}

/** Switch between flat 2D and tilted 3D-buildings view. */
export function set3D(map: maplibregl.Map, on: boolean, animate = true) {
  if (on) ensureBuildings(map);
  if (map.getLayer(BUILDINGS_LAYER)) map.setLayoutProperty(BUILDINGS_LAYER, "visibility", on ? "visible" : "none");
  const opts = { duration: animate ? 900 : 0 };
  if (on) map.easeTo({ pitch: PITCH_3D, bearing: map.getBearing() || -20, zoom: Math.max(map.getZoom(), 15.3), ...opts });
  else map.easeTo({ pitch: 0, bearing: 0, ...opts });
}

const HEADINGS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
export const headingLabel = (bearing: number) => HEADINGS[Math.round((((bearing % 360) + 360) % 360) / 45) % 8];

/** Track the map's bearing for the compass. */
export function useBearing(map: maplibregl.Map | null): number {
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
export function Compass({ map }: { map: maplibregl.Map | null }) {
  const bearing = useBearing(map);
  const ticks = Array.from({ length: 60 }, (_, i) => i * 6);
  return (
    <button type="button" className="compass" onClick={() => map?.easeTo({ bearing: 0, duration: 600 })} aria-label={`Heading ${headingLabel(bearing)}. Reset to north`} title="Reset to north">
      <svg viewBox="0 0 120 120" aria-hidden="true">
        <circle cx="60" cy="60" r="57" fill="#141416" stroke="rgba(255,255,255,0.08)" strokeWidth="2" />
        <g transform={`rotate(${-bearing} 60 60)`}>
          {ticks.map((t) => (
            <line key={t} x1="60" y1="9" x2="60" y2={t % 30 === 0 ? 17 : 13} stroke={t === 0 ? "#ff6b5b" : "rgba(228,242,138,0.75)"} strokeWidth={t % 30 === 0 ? 2 : 1} transform={`rotate(${t} 60 60)`} />
          ))}
          {["N", "E", "S", "W"].map((l, i) => (
            <text key={l} x="60" y="29" textAnchor="middle" fontSize="9" fontWeight="600" fill={l === "N" ? "#ff6b5b" : "rgba(255,255,255,0.55)"} transform={`rotate(${i * 90} 60 60)`}>{l}</text>
          ))}
        </g>
        <circle cx="60" cy="60" r="27" fill="#1e1f22" stroke="rgba(255,255,255,0.06)" />
        <text x="60" y="65" textAnchor="middle" fontSize="15" fontWeight="600" fill="#f4f4f5">{headingLabel(bearing)}</text>
      </svg>
    </button>
  );
}

/** Round floating map buttons (zoom, north, 2D/3D, fit city, fullscreen) stacked on the right edge. */
export function MapControls({ map, mode3d, onToggle3d, onFit, fullscreenTarget, testId }: {
  map: maplibregl.Map | null; mode3d: boolean; onToggle3d: () => void; onFit?: () => void; fullscreenTarget?: React.RefObject<HTMLElement | null>; testId?: string;
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

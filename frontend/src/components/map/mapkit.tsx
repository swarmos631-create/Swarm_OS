import { Icon } from "../../lib/icons";

export type MapTheme = "light" | "dark";

/** The map methods the shared round controls need; implemented by the MapmyIndia adapter in mappls.tsx. */
export interface MapLike {
  zoomIn: () => void;
  zoomOut: () => void;
  getBearing: () => number;
  easeTo: (o: { bearing?: number; pitch?: number; center?: [number, number]; zoom?: number; duration?: number }) => void;
  flyTo: (o: { center?: [number, number]; zoom?: number }) => void;
  fitBounds: (b: [number, number, number, number], o?: { padding?: number; duration?: number }) => void;
}

/** Vivid point colours (marker HTML needs literal colours, so these mirror the CSS tokens per theme). */
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

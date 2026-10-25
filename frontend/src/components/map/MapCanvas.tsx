import { lazy, Suspense, useEffect, useRef, useState } from "react";

import type { MapTheme } from "./mapkit";
import { hasMappls, MapplsCanvas, type MapLine, type MapPoint } from "./mappls";
import type { MapLike } from "./mapkit";

const OpenFreeMapCanvas = lazy(() => import("./openfree"));

export interface MapCanvasProps {
  center: { lat: number; lon: number };
  zoom: number;
  points: MapPoint[];
  lines?: MapLine[];
  mode3d: boolean;
  theme: MapTheme;
  onSelect?: (point: MapPoint) => void;
  onReady?: (map: MapLike | null) => void;
  onFail: (message: string) => void;
  onMapClick?: (coordinate: [number, number]) => void;
  onProviderChange?: (provider: "mappls" | "openfreemap") => void;
  label: string;
}

/** Use the configured Mappls key when available and a keyless, attributed OpenFreeMap fallback otherwise. */
export function MapCanvas(props: MapCanvasProps) {
  const [provider, setProvider] = useState<"mappls" | "openfreemap">(hasMappls ? "mappls" : "openfreemap");
  const [mapplsError, setMapplsError] = useState("");
  const providerChange = useRef(props.onProviderChange);
  providerChange.current = props.onProviderChange;

  useEffect(() => {
    setProvider(hasMappls ? "mappls" : "openfreemap");
    setMapplsError("");
  }, [props.center.lat, props.center.lon]);

  useEffect(() => { providerChange.current?.(provider); }, [provider]);

  if (provider === "mappls") {
    return <MapplsCanvas {...props} onFail={(message) => {
      setMapplsError(message);
      setProvider("openfreemap");
    }} />;
  }

  return <Suspense fallback={<div className="map-canvas-loading" role="status">Loading OpenStreetMap-backed map…</div>}>
    <OpenFreeMapCanvas {...props} onFail={(message) => props.onFail(mapplsError ? `${message}. Mappls was also unavailable.` : message)} />
  </Suspense>;
}

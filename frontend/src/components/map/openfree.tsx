import { useEffect, useId, useRef, useState } from "react";
import { Map, Marker, Popup, setWorkerUrl } from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "maplibre-gl/dist/maplibre-gl.css";

import type { MapLike, MapTheme } from "./mapkit";
import type { MapLine, MapPoint } from "./mappls";

setWorkerUrl(workerUrl);

const escapeHtml = (value: string) => value.replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char] as string);

const EMPTY_LINES: MapLine[] = [];

function adapt(map: Map): MapLike {
  return {
    zoomIn: () => map.zoomIn(),
    zoomOut: () => map.zoomOut(),
    getBearing: () => map.getBearing(),
    easeTo: (options) => map.easeTo({
      bearing: options.bearing,
      pitch: options.pitch,
      center: options.center,
      zoom: options.zoom,
      duration: options.duration,
    }),
    flyTo: (options) => map.flyTo({ center: options.center, zoom: options.zoom }),
    fitBounds: (bounds, options) => map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]], {
      padding: options?.padding ?? 24, duration: options?.duration ?? 0,
    }),
  };
}

/** Keyless OpenFreeMap vector map backed by OpenStreetMap data, used when the optional Mappls key is unavailable. */
export default function OpenFreeMapCanvas({ center, zoom, points, lines = EMPTY_LINES, mode3d, theme, onSelect, onReady, onFail, onMapClick, onProviderChange, label }: {
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
  onProviderChange?: (provider: "openfreemap") => void;
  label: string;
}) {
  const mapId = `openfree-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const mapRef = useRef<Map | null>(null);
  const markers = useRef<Marker[]>([]);
  const callbacks = useRef({ onSelect, onReady, onFail, onMapClick, onProviderChange });
  const [loaded, setLoaded] = useState(false);
  callbacks.current = { onSelect, onReady, onFail, onMapClick, onProviderChange };

  useEffect(() => {
    let disposed = false;
    let reportedError = false;
    let lastLoadError = "";
    setLoaded(false);
    const map = new Map({
      container: mapId,
      style: `https://tiles.openfreemap.org/styles/${theme === "dark" ? "dark" : "positron"}`,
      center: [center.lon, center.lat],
      zoom,
      pitch: mode3d ? 50 : 0,
      maxPitch: 60,
      cooperativeGestures: true,
      trackResize: true,
    });
    mapRef.current = map;
    const ready = () => {
      if (disposed) return;
      window.clearTimeout(failureTimer);
      setLoaded(true);
      requestAnimationFrame(() => map.resize());
      callbacks.current.onReady?.(adapt(map));
      callbacks.current.onProviderChange?.("openfreemap");
    };
    map.once("load", ready);
    const failureTimer = window.setTimeout(() => {
      if (disposed || loaded) return;
      reportedError = true;
      callbacks.current.onFail(lastLoadError || "OpenFreeMap did not finish loading");
    }, 15000);
    map.on("error", (event) => {
      if (disposed || reportedError) return;
      // Tile requests can fail independently while the rest of the map still loads.
      // Only surface a fallback error if the complete style never becomes ready.
      lastLoadError = event.error?.message || "OpenFreeMap tiles could not be loaded";
    });
    return () => {
      disposed = true;
      window.clearTimeout(failureTimer);
      setLoaded(false);
      markers.current.forEach((marker) => marker.remove());
      markers.current = [];
      map.remove();
      mapRef.current = null;
      callbacks.current.onReady?.(null);
    };
    // Map style and city changes rebuild the renderer; data overlays update separately.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapId, center.lat, center.lon, theme]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded) return;
    markers.current.forEach((marker) => marker.remove());
    markers.current = points.slice(0, 900).map((point) => {
      const diameter = Math.max(8, point.size * 2);
      const element = document.createElement("button");
      element.type = "button";
      element.className = "mpl-dot openfree-marker";
      element.style.width = `${diameter}px`;
      element.style.height = `${diameter}px`;
      element.style.background = point.color;
      element.style.boxShadow = `0 0 0 2px ${point.ring ?? "rgba(255,255,255,.9)"}, 0 0 12px ${point.color}`;
      element.setAttribute("aria-label", point.subtitle ? `${point.title}, ${point.subtitle}` : point.title);
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        callbacks.current.onSelect?.(point);
      });
      element.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") callbacks.current.onSelect?.(point);
      });
      const popup = new Popup({ offset: 10, closeButton: true }).setHTML(
        `<b>${escapeHtml(point.title)}</b>${point.subtitle ? `<br><span>${escapeHtml(point.subtitle)}</span>` : ""}`,
      );
      return new Marker({ element, anchor: "center" }).setLngLat([point.lon, point.lat]).setPopup(popup).addTo(map);
    });
  }, [points, loaded]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded) return;
    const sourceId = `${mapId}-routes`;
    const layerId = `${mapId}-routes-line`;
    if (map.getLayer(layerId)) map.removeLayer(layerId);
    if (map.getSource(sourceId)) map.removeSource(sourceId);
    const valid = lines.filter((line) => line.coordinates.length >= 2 && line.coordinates.length <= 10000);
    if (!valid.length) return;
    map.addSource(sourceId, {
      type: "geojson",
      data: {
        type: "FeatureCollection",
        features: valid.map((line) => ({
          type: "Feature" as const,
          properties: { color: line.color, title: line.title },
          geometry: { type: "LineString" as const, coordinates: line.coordinates },
        })),
      },
    });
    map.addLayer({
      id: layerId,
      type: "line",
      source: sourceId,
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": ["get", "color"], "line-width": 5, "line-opacity": 0.92 },
    });
    const coords = valid.flatMap((line) => line.coordinates);
    const lngs = coords.map(([lng]) => lng), lats = coords.map(([, lat]) => lat);
    map.fitBounds([[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]], { padding: 56, maxZoom: 16 });
    return () => {
      if (map.getLayer(layerId)) map.removeLayer(layerId);
      if (map.getSource(sourceId)) map.removeSource(sourceId);
    };
  }, [lines, loaded, mapId]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded) return;
    const onClick = (event: { lngLat: { lng: number; lat: number } }) => callbacks.current.onMapClick?.([event.lngLat.lng, event.lngLat.lat]);
    map.on("click", onClick);
    return () => { map.off("click", onClick); };
  }, [loaded]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded) return;
    map.easeTo({ pitch: mode3d ? 50 : 0 });
  }, [mode3d, loaded]);

  return <div id={mapId} className="openfree-map-host" role="region" aria-label={label} />;
}

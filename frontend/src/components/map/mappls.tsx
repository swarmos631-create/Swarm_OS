// The Mappls Web SDK ships without TypeScript types, so its objects are typed as `any` here.
import { useEffect, useId, useRef, useState } from "react";

import type { MapLike, MapTheme } from "./mapkit";

/** MapmyIndia (Mappls) static key for the Web SDK — the only base map. Set VITE_MAPPLS_KEY in the hosting variables (build-time). */
export const MAPPLS_KEY = String(import.meta.env.VITE_MAPPLS_KEY ?? "").trim();
export const hasMappls = MAPPLS_KEY.length > 0;

declare global {
  interface Window { mappls?: any }
}

let loading: Promise<any> | null = null;

/** Load the Mappls Web SDK v3 once. Rejects after 15 s or on a script error (bad key, domain not whitelisted, offline). */
export function loadMappls(): Promise<any> {
  if (!hasMappls) return Promise.reject(new Error("MapmyIndia key is not configured — set VITE_MAPPLS_KEY in the Railway variables and redeploy"));
  if (window.mappls?.Map) return Promise.resolve(window.mappls);
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `https://sdk.mappls.com/map/sdk/web?v=3.0&access_token=${encodeURIComponent(MAPPLS_KEY)}`;
    s.async = true;
    const started = Date.now();
    const poll = () => {
      if (window.mappls?.Map) resolve(window.mappls);
      else if (Date.now() - started > 15_000) reject(new Error("MapmyIndia SDK did not initialise (check the key and its allowed domains)"));
      else setTimeout(poll, 100);
    };
    s.onload = poll;
    s.onerror = () => {
      // The SDK answers errors as JSON (e.g. 401 "IP/Domain validation failed"); read it to explain the failure.
      fetch(s.src)
        .then((r) => r.json())
        .then((j: { error_code?: string; error_description?: string }) => {
          if (j.error_code === "ASSET_ACCESS_DENIED" || /domain/i.test(j.error_description ?? "")) {
            reject(new Error(`MapmyIndia rejected this website — add ${window.location.host} to the key's allowed domains in the Mappls console`));
          } else if (/token|credential/i.test(`${j.error_code} ${j.error_description}`)) {
            reject(new Error("MapmyIndia did not recognise the key in VITE_MAPPLS_KEY (use the project's static key)"));
          } else {
            reject(new Error(`MapmyIndia SDK error: ${j.error_description ?? j.error_code ?? "unknown"}`));
          }
        })
        .catch(() => reject(new Error("MapmyIndia SDK could not be downloaded")));
    };
    document.head.appendChild(s);
  }).catch((e) => {
    loading = null;
    throw e;
  });
  return loading;
}

const call = (obj: any, name: string, ...args: any[]) => {
  try {
    return typeof obj?.[name] === "function" ? obj[name](...args) : undefined;
  } catch {
    return undefined;
  }
};

/** Wrap a Mappls map so the shared round controls and compass can drive it. Native MapLibre-style methods are used when present. */
function adapt(m: any): MapLike {
  const zoom = () => Number(call(m, "getZoom") ?? 12);
  const a: MapLike = {
    zoomIn: () => (typeof m.zoomIn === "function" ? call(m, "zoomIn") : call(m, "setZoom", zoom() + 1)),
    zoomOut: () => (typeof m.zoomOut === "function" ? call(m, "zoomOut") : call(m, "setZoom", zoom() - 1)),
    getBearing: () => Number(call(m, "getBearing") ?? call(m, "getHeading") ?? 0),
    easeTo: (o: { bearing?: number; pitch?: number; center?: [number, number]; zoom?: number }) => {
      if (typeof m.easeTo === "function") return call(m, "easeTo", o);
      if (o.bearing != null && call(m, "setHeading", o.bearing) === undefined) call(m, "setBearing", o.bearing);
      if (o.pitch != null && call(m, "setTilt", o.pitch) === undefined) call(m, "setPitch", o.pitch);
      if (o.center) call(m, "setCenter", { lat: o.center[1], lng: o.center[0] });
      if (o.zoom != null) call(m, "setZoom", o.zoom);
    },
    flyTo: (o: { center?: [number, number]; zoom?: number }) => {
      if (typeof m.flyTo === "function") return call(m, "flyTo", o);
      if (o.center) call(m, "setCenter", { lat: o.center[1], lng: o.center[0] });
      if (o.zoom != null) call(m, "setZoom", o.zoom);
    },
    fitBounds: (b: [number, number, number, number], o?: { padding?: number }) => call(m, "fitBounds", [[b[0], b[1]], [b[2], b[3]]], { padding: o?.padding ?? 20 }),
  };
  return a;
}

export interface MapPoint {
  id: string;
  lat: number;
  lon: number;
  color: string;
  size: number;
  ring?: string;
  title: string;
  subtitle?: string;
  props: Record<string, any>;
}

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] as string);

/**
 * MapmyIndia vector map with SwarmOS points as coloured markers. Tilts to 60° in 3D mode (Mappls renders its
 * building models at street zoom). Calls `onFail` if the SDK cannot start, so the page can explain why.
 */
export function MapplsCanvas({ center, zoom, points, mode3d, theme, onSelect, onReady, onFail, label }: {
  center: { lat: number; lon: number };
  zoom: number;
  points: MapPoint[];
  mode3d: boolean;
  theme: MapTheme;
  onSelect?: (p: MapPoint) => void;
  onReady?: (m: MapLike | null) => void;
  onFail: (message: string) => void;
  label: string;
}) {
  const id = `mappls-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const mapRef = useRef<any>(null);
  const markers = useRef<any[]>([]);
  const [loaded, setLoaded] = useState(false);
  const cbs = useRef({ onSelect, onReady, onFail });
  cbs.current = { onSelect, onReady, onFail };
  // The latest view, read when the map is (re)built so a theme change keeps the current city and zoom.
  const view = useRef({ center, zoom, mode3d });
  view.current = { center, zoom, mode3d };
  const shown = useRef<{ lat: number; lon: number } | null>(null); // the centre the map is showing

  useEffect(() => {
    let dead = false;
    loadMappls()
      .then((mappls) => {
        if (dead) return;
        const v = view.current;
        const m = new mappls.Map(id, {
          center: { lat: v.center.lat, lng: v.center.lon }, zoom: v.zoom, zoomControl: false, location: false, geolocation: false,
          fullscreenControl: false, traffic: false, tilt: v.mode3d ? 60 : 0, backgroundColor: theme === "dark" ? "#0e1124" : "#e9ecf5",
        });
        mapRef.current = m;
        shown.current = { lat: v.center.lat, lon: v.center.lon };
        const ready = () => {
          if (dead) return;
          call(mappls, "setStyle", theme === "dark" ? "standard-night" : "standard-day");
          setLoaded(true);
          cbs.current.onReady?.(adapt(m));
        };
        if (typeof m.addListener === "function") m.addListener("load", ready);
        else if (typeof m.on === "function") m.on("load", ready);
        else ready();
      })
      .catch((e: Error) => !dead && cbs.current.onFail(e.message));
    return () => {
      dead = true;
      setLoaded(false);
      markers.current.forEach((mk) => call(mk, "remove"));
      markers.current = [];
      call(mapRef.current, "remove");
      mapRef.current = null;
      cbs.current.onReady?.(null);
    };
    // Built once (and again only when the theme changes). A city switch moves the existing map instead of
    // rebuilding it — re-creating a Mappls map re-downloads its style, sprites and tiles, which took seconds.
  }, [id, theme]);

  // City switch: fly the loaded map to the new centre (also covers a switch made while the map was still loading).
  useEffect(() => {
    const m = mapRef.current;
    const at = shown.current;
    if (!m || !loaded || (at && at.lat === center.lat && at.lon === center.lon)) return;
    shown.current = { lat: center.lat, lon: center.lon };
    const z = view.current.mode3d ? Math.max(16.5, zoom) : zoom;
    if (typeof m.flyTo === "function") call(m, "flyTo", { center: [center.lon, center.lat], zoom: z, duration: 1200, essential: true });
    else {
      call(m, "setCenter", { lat: center.lat, lng: center.lon });
      call(m, "setZoom", z);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [center.lat, center.lon, loaded]);

  // Markers: drawn in small batches per animation frame so a city with hundreds of points never freezes the page.
  useEffect(() => {
    const m = mapRef.current;
    const mappls = window.mappls;
    if (!m || !mappls || !loaded) return;
    let cancelled = false;
    markers.current.forEach((mk) => call(mk, "remove"));
    markers.current = [];
    const todo = points.slice(0, 900);
    let i = 0;
    const step = () => {
      if (cancelled) return;
      const end = Math.min(todo.length, i + 120);
      for (; i < end; i++) {
        const p = todo[i];
        const d = p.size * 2;
        const html = `<div class="mpl-dot" style="width:${d}px;height:${d}px;background:${p.color};box-shadow:0 0 0 2px ${p.ring ?? "rgba(255,255,255,.85)"},0 0 12px ${p.color}"></div>`;
        try {
          const mk = new mappls.Marker({
            map: m, position: { lat: p.lat, lng: p.lon }, html, width: d, height: d, fitbounds: false,
            popupHtml: `<b>${esc(p.title)}</b>${p.subtitle ? `<br><span>${esc(p.subtitle)}</span>` : ""}`,
          });
          call(mk, "addListener", "click", () => cbs.current.onSelect?.(p));
          markers.current.push(mk);
        } catch {
          /* skip a point the SDK rejects */
        }
      }
      if (i < todo.length) requestAnimationFrame(step);
    };
    step();
    return () => { cancelled = true; };
  }, [points, loaded]);

  useEffect(() => {
    const m = mapRef.current;
    if (!m || !loaded) return;
    if (call(m, "setTilt", mode3d ? 60 : 0) === undefined) call(m, "setPitch", mode3d ? 60 : 0);
    if (mode3d && Number(call(m, "getZoom") ?? 0) < 16) call(m, "setZoom", 16.5);
  }, [mode3d, loaded]);

  return <div id={id} className="mappls-canvas-host" style={{ position: "absolute", inset: 0 }} role="region" aria-label={label} />;
}

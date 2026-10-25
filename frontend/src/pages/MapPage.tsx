import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api/client";
import { mapLayersQuery } from "../api/queries";
import { MapCanvas } from "../components/map/MapCanvas";
import { MapControls, type MapLike, POINT_COLORS } from "../components/map/mapkit";
import { type MapPoint } from "../components/map/mappls";
import { ErrorState, ProvenanceBadge, SeverityBadge } from "../components/ui";
import { fmtTime, titleCase } from "../lib/format";
import { Icon } from "../lib/icons";
import { useCity } from "../state/city";
import { useTheme } from "../state/theme";
import { useOnline } from "../state/ui";

const DEFAULT_LAYERS = ["incidents", "earthquake", "hospital", "fire_station", "traffic_sensors", "water_sensors"];

interface Layer { id: string; label: string; group: string; count: number; provenance: (string | null)[] }
type FC = GeoJSON.FeatureCollection<GeoJSON.Point, Record<string, any>>;

export default function MapPage() {
  const { cityId, city } = useCity();
  const online = useOnline();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [mapError, setMapError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>(DEFAULT_LAYERS);
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [mode3d, setMode3d] = useState(true);
  const [search, setSearch] = useState("");
  const { theme } = useTheme();
  const pal = POINT_COLORS[theme];
  const [mpl, setMpl] = useState<MapLike | null>(null);
  const [mapProvider, setMapProvider] = useState<"mappls" | "openfreemap">("openfreemap");

  const layersQ = useQuery(mapLayersQuery<{ layers: Layer[]; note: string }>(cityId));
  const available = layersQ.data?.layers ?? [];
  const active = selected.filter((s) => available.some((l) => l.id === s));
  const featQ = useQuery({
    queryKey: ["map-features", cityId, active.join(",")],
    queryFn: () => api<FC>(`/cities/${cityId}/map/features`, { query: { layers: active.join(",") } }),
    enabled: active.length > 0,
    placeholderData: keepPreviousData,
  });

  // City switch: the active map renderer is rebuilt around the new city; clear the old selection and error.
  useEffect(() => { setDetail(null); setMapError(null); }, [cityId]);

  const ctl: MapLike | null = mpl;
  const points: MapPoint[] = useMemo(() => (active.length ? featQ.data?.features ?? [] : []).map((f: GeoJSON.Feature<GeoJSON.Point, Record<string, any>>, i: number) => {
    const p = f.properties;
    const inc = p.layer === "incidents";
    return {
      id: String(p.id ?? i), lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1],
      color: inc ? pal.sev[p.severity as keyof typeof pal.sev] ?? pal.sev.low : pal.layer[p.layer] ?? pal.fallback,
      size: inc ? 7 : p.layer === "earthquake" ? 3 + 1.6 * Number(p.magnitude ?? 3) : 5, ring: p.provenance === "demo" ? pal.demo : undefined,
      title: String(p.title ?? titleCase(String(p.layer))), subtitle: titleCase(String(p.layer)), props: p,
    };
  }), [featQ.data, active.length, pal]);

  const searchHits = useMemo(() => {
    const s = search.trim().toLowerCase();
    if (s.length < 2) return [];
    return (featQ.data?.features ?? []).filter((f: GeoJSON.Feature<GeoJSON.Point, Record<string, any>>) => String(f.properties.title ?? "").toLowerCase().includes(s)).slice(0, 8);
  }, [search, featQ.data]);

  const toggle = (id: string) => setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  const groups = Array.from(new Set(available.map((l) => l.group)));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Operations Map — {city?.name}</h1>
          <p>City layers from synchronised sources over {mapProvider === "mappls" ? "Mappls" : "OpenFreeMap"} base tiles. Tile requests expose the viewed area to that provider; base tiles are separate from SwarmOS sensor and incident data.</p>
        </div>
        <div className="row wrap">
          <div className="row" role="group" aria-label="Map mode">
            <button className="chip" aria-pressed={!mode3d} onClick={() => setMode3d(false)}><Icon name="map" width={14} />2D</button>
            <button className="chip" aria-pressed={mode3d} onClick={() => setMode3d(true)} data-testid="map-3d"><Icon name="cube" width={14} />3D tilt</button>
          </div>
        </div>
      </div>
      {mode3d && <div className="notice info"><Icon name="info" />3D mode tilts the {mapProvider === "mappls" ? "Mappls" : "OpenFreeMap"} view. Building detail depends on the map provider and zoom level; this is illustrative, not a survey-grade digital twin.</div>}
      {!online && <div className="notice offline"><Icon name="wifiOff" />Offline: only previously cached map tiles can be shown.</div>}
      {mapError && <div className="notice error"><Icon name="alert" />{mapError}</div>}

      <div className="map-wrap" data-testid="ops-map" ref={wrapRef}>
        {city && (
          <MapCanvas center={{ lat: city.lat, lon: city.lon }} zoom={mode3d ? 15.5 : city.zoom} points={points} mode3d={mode3d} theme={theme} onReady={setMpl} onProviderChange={setMapProvider}
            label={`Map of ${city.name}`} onSelect={(p) => setDetail({ ...p.props, _coords: [p.lon, p.lat] })}
            onFail={(msg) => setMapError(`${msg}. City layers are still listed and searchable on the left.`)} />
        )}
        <div className="map-overlay map-layers">
          <div className="field" style={{ marginBottom: 8, position: "relative" }}>
            <label className="sr-only" htmlFor="map-search">Search features</label>
            <input id="map-search" className="input" placeholder="Search loaded features…" value={search} onChange={(e) => setSearch(e.target.value)} />
            {searchHits.length > 0 && (
              <ul className="list panel popover" style={{ position: "absolute", top: 38, left: 0, right: 0, zIndex: 3 }}>
                {searchHits.map((f: GeoJSON.Feature<GeoJSON.Point, Record<string, any>>, i: number) => (
                  <li key={i} style={{ cursor: "pointer", padding: "6px 10px" }} onClick={() => {
                    ctl?.flyTo({ center: f.geometry.coordinates as [number, number], zoom: 15 });
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
                  <i style={{ width: 9, height: 9, borderRadius: 3, background: l.id === "incidents" ? pal.sev.high : pal.layer[l.id] ?? pal.fallback, display: "inline-block" }} />
                  <span style={{ flex: 1 }}>{l.label}</span>
                  <span className="num xs faint">{l.count}</span>
                  {l.provenance.includes("demo") && <span className="badge b-demo" style={{ height: 16 }}>simulated</span>}
                </label>
              ))}
            </div>
          ))}
          {!layersQ.isLoading && available.length <= 1 && (
            <p className="xs faint">Only incidents are available for this city. Run the Mappls or OpenStreetMap sync from Data Integrations to add facility layers.</p>
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

        <MapControls map={ctl} mode3d={mode3d} onToggle3d={() => setMode3d((m) => !m)} fullscreenTarget={wrapRef}
          onFit={() => city && ctl?.fitBounds(city.bbox as [number, number, number, number], { padding: 20, duration: 900 })} />

        <div className="map-overlay map-legend legend hide-sm">
          {Object.entries(pal.sev).map(([k, c]) => <span key={k}><i style={{ background: c, borderRadius: "50%" }} />{k}</span>)}
          <span><i style={{ background: "transparent", border: `2px solid ${pal.demo}`, borderRadius: "50%" }} />simulated</span>
        </div>
      </div>
    </>
  );
}

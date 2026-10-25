import { useMutation } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api/client";
import { MapCanvas } from "../components/map/MapCanvas";
import { loadMapplsSearch, hasMappls, type MapLine, type MapPoint } from "../components/map/mappls";
import { ErrorState, Field, Panel, ProvenanceBadge, StatusBadge } from "../components/ui";
import { fmtNum } from "../lib/format";
import { useCity } from "../state/city";
import { useTheme } from "../state/theme";

interface RouteOption {
  id: string;
  distance_m: number;
  duration_s: number;
  geometry: { type: "LineString"; coordinates: [number, number][] };
  context: {
    open_incident_points_within_250m: number;
    mapped_help_facilities_within_500m: Record<string, number>;
    mapped_transit_stations_within_500m: Record<string, number>;
    facility_features_checked: number;
    transit_features_checked: number;
    mapped_wheelchair_accessibility_within_500m?: Record<string, number>;
    wheelchair_features_near_route?: number;
    mapped_feature_evidence?: MappedFeatureEvidence[];
    mapped_feature_evidence_truncated?: boolean;
    context_truncated?: boolean;
    provenance: string[];
  };
}
interface MappedFeatureEvidence {
  source_id: string;
  external_id: string;
  name: string | null;
  layer: string;
  provenance: string;
  distance_to_sampled_route_m: number;
  wheelchair: string | null;
  observed_at: string | null;
  fetched_at: string | null;
}
interface RouteResponse {
  routes: RouteOption[];
  source: string;
  attribution: string;
  source_url: string;
  limitations: string[];
}
type Endpoint = { lat: string; lon: string };

const colors = ["var(--cyan)", "var(--orange)", "var(--violet)"];

export default function SakhiRoutePage() {
  const { cityId, city } = useCity();
  const [origin, setOrigin] = useState<Endpoint>({ lat: "", lon: "" });
  const [destination, setDestination] = useState<Endpoint>({ lat: "", lon: "" });
  const [mapTarget, setMapTarget] = useState<"origin" | "destination">("origin");
  const [mapError, setMapError] = useState<string | null>(null);
  const [mode, setMode] = useState<"foot" | "bike" | "car">("foot");
  const { theme } = useTheme();
  const routes = useMutation({
    mutationFn: () => api<RouteResponse>("/public/cities/" + cityId + "/routes/compare", {
      method: "POST",
      body: { origin: { lat: Number(origin.lat), lon: Number(origin.lon) }, destination: { lat: Number(destination.lat), lon: Number(destination.lon) }, mode },
    }),
  });
  const complete = [origin.lat, origin.lon, destination.lat, destination.lon].every((value) => value.trim() !== "" && Number.isFinite(Number(value)));
  const mapPoints: MapPoint[] = [
    ...(origin.lat && origin.lon ? [{ id: "origin", lat: Number(origin.lat), lon: Number(origin.lon), color: "#22d3ee", size: 7, title: "Start", subtitle: "Selected route origin", props: {} }] : []),
    ...(destination.lat && destination.lon ? [{ id: "destination", lat: Number(destination.lat), lon: Number(destination.lon), color: "#fb923c", size: 7, title: "Destination", subtitle: "Selected route destination", props: {} }] : []),
  ];
  const mapLines = useMemo<MapLine[]>(() => (routes.data?.routes ?? []).map((route, index) => ({
    id: route.id, coordinates: route.geometry.coordinates, color: colors[index % colors.length], title: `Route option ${index + 1}`,
  })), [routes.data]);

  return <>
    <div className="page-head">
      <div><h1>SakhiRoute — {city?.name}</h1><p>Compare real walking, cycling or driving paths with mapped incident and public-help context.</p></div>
    </div>
    <div className="notice info" style={{ marginBottom: 14 }}>
      Routes are not safety guarantees. Map tiles come from the configured Mappls provider or the keyless OpenFreeMap/OpenStreetMap fallback; tile requests expose the viewed area to that provider. When enabled, address search text is sent to Mappls for place suggestions. When you request a route, start and destination coordinates are sent to the FOSSGIS routing service, which may retain request logs. SwarmOS does not store route history.
    </div>
    <div className="grid cols-3">
      <Panel title="Compare routes" className="span-1">
        <form className="stack-lg" onSubmit={(event) => { event.preventDefault(); routes.mutate(); }}>
          <EndpointFields label="Start" value={origin} cityLat={city?.lat} cityLon={city?.lon} onChange={setOrigin} />
          <EndpointFields label="Destination" value={destination} cityLat={city?.lat} cityLon={city?.lon} onChange={setDestination} />
          <Field label="Travel mode"><select className="select" value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>
            <option value="foot">Walking</option><option value="bike">Cycling</option><option value="car">Driving</option>
          </select></Field>
          <p className="xs faint" style={{ margin: 0 }}>Enter WGS84 latitude/longitude. Points must be within 150 km of {city?.name ?? "this city"}. No address or identity is requested.</p>
          <button className="btn primary" type="submit" disabled={!complete || routes.isPending}>{routes.isPending ? "Finding routes…" : "Compare route options"}</button>
          {routes.error && <ErrorState error={routes.error} />}
        </form>
      </Panel>
      <Panel title="Route comparison" className="span-2">
        {!routes.data ? <div className="small muted">Choose endpoints and request route alternatives to see path geometry, estimated travel time, and nearby mapped context.</div> : <>
          {mapError ? <RouteSketch routes={routes.data.routes} /> : <p className="small muted">Route alternatives are drawn to scale on the map below. Colors identify each option; the map fits to their returned geometry.</p>}
          <RouteLegend routes={routes.data.routes} />
          <div className="grid cols-3" style={{ marginTop: 12 }}>
            {routes.data.routes.map((route, index) => <RouteCard key={route.id} route={route} index={index} />)}
          </div>
          <p className="xs faint" style={{ marginBottom: 0 }}>{routes.data.attribution} · <a href={routes.data.source_url} target="_blank" rel="noreferrer">Provider policy and source ↗</a></p>
          {routes.data.limitations.map((limitation) => <p key={limitation} className="xs muted" style={{ margin: "6px 0 0" }}>• {limitation}</p>)}
        </>}
      </Panel>
    </div>
    <Panel title="Map routes and choose endpoints" icon="map">
      <div className="row wrap between" style={{ marginBottom: 10 }}>
        <span className="small muted">Returned route geometry is overlaid here. Click the map to set the selected endpoint; clicking does not request a route.</span>
        <Field label="Click sets"><select className="select" value={mapTarget} onChange={(event) => setMapTarget(event.target.value as typeof mapTarget)}>
          <option value="origin">Start</option><option value="destination">Destination</option>
        </select></Field>
      </div>
      {mapError && <div className="notice error" role="alert">{mapError}</div>}
      {city && <div className="map-wrap" style={{ height: 360, position: "relative", borderRadius: 12, overflow: "hidden" }}>
        <MapCanvas center={{ lat: city.lat, lon: city.lon }} zoom={city.zoom} points={mapPoints} lines={mapLines} mode3d={false} theme={theme}
          label={`Choose route endpoints on the ${city.name} map`} onFail={setMapError}
          onMapClick={([lon, lat]) => {
            const endpoint = { lat: lat.toFixed(6), lon: lon.toFixed(6) };
            if (mapTarget === "origin") setOrigin(endpoint); else setDestination(endpoint);
          }} />
      </div>}
      {routes.data && <RouteLegend routes={routes.data.routes} />}
      <div className="row wrap xs faint" style={{ marginTop: 8 }}>
        <span>Start: {origin.lat && origin.lon ? `${origin.lat}, ${origin.lon}` : "not selected"}</span>
        <span>Destination: {destination.lat && destination.lon ? `${destination.lat}, ${destination.lon}` : "not selected"}</span>
      </div>
    </Panel>
  </>;
}

function EndpointFields({ label, value, cityLat, cityLon, onChange }: { label: string; value: Endpoint; cityLat?: number; cityLon?: number; onChange: (value: Endpoint) => void }) {
  const inputId = `sakhi-search-${label.toLowerCase()}`;
  const inputRef = useRef<HTMLInputElement>(null);
  const [searchError, setSearchError] = useState("");
  const [searchReady, setSearchReady] = useState(false);

  useEffect(() => {
    if (!hasMappls || !inputRef.current) return;
    let disposed = false;
    let plugin: any;
    const input = inputRef.current;
    const callback = (result: unknown) => {
      if (disposed) return;
      const point = readMapplsPoint(result);
      if (!point) {
        setSearchError("Mappls did not return coordinates for that result. Enter coordinates or choose a point on the map.");
        return;
      }
      setSearchError("");
      onChange({ lat: point.lat.toFixed(6), lon: point.lon.toFixed(6) });
    };
    loadMapplsSearch().then((mappls) => {
      if (disposed || !input) return;
      plugin = new mappls.search(input, { location: [cityLat ?? 28.6139, cityLon ?? 77.209], region: "IND", geolocation: false, searchChars: 3, height: 220 }, callback);
      setSearchReady(true);
    }).catch((error: Error) => {
      if (!disposed) setSearchError(error.message);
    });
    return () => {
      disposed = true;
      try {
        if (plugin && window.mappls?.removeLayer) window.mappls.removeLayer({ layer: plugin });
      } catch { /* The provider plugin may not expose a removable layer for standalone searches. */ }
    };
  }, [cityLat, cityLon, onChange]);

  return <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
    <legend className="small" style={{ marginBottom: 6 }}>{label}</legend>
    {hasMappls && <Field label="Find an address or place">
      <input id={inputId} ref={inputRef} className="input" type="search" autoComplete="off" placeholder="Search Indian addresses and places" aria-describedby={`${inputId}-help`} />
    </Field>}
    {hasMappls && <div id={`${inputId}-help`} className="xs faint">{searchError || (searchReady ? "Search powered by Mappls. Selected coordinates fill the fields below." : "Loading Mappls place search…")}</div>}
    <div className="xs faint">Or enter coordinates</div>
    <div className="grid cols-2">
      <Field label="Latitude"><input className="input num" type="number" min={-90} max={90} step="any" value={value.lat} required onChange={(event) => onChange({ ...value, lat: event.target.value })} /></Field>
      <Field label="Longitude"><input className="input num" type="number" min={-180} max={180} step="any" value={value.lon} required onChange={(event) => onChange({ ...value, lon: event.target.value })} /></Field>
    </div>
  </fieldset>;
}

function readMapplsPoint(value: unknown): { lat: number; lon: number } | null {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (!candidate || typeof candidate !== "object") return null;
  const item = candidate as Record<string, any>;
  const nested = item.results?.[0] ?? item.data?.[0] ?? item.data ?? item.result ?? item;
  if (!nested || typeof nested !== "object") return null;
  const coordinates = nested.geometry?.coordinates ?? nested.coordinates ?? nested.location;
  const lon = Number(nested.longitude ?? nested.lon ?? nested.lng ?? (Array.isArray(coordinates) ? coordinates[0] : coordinates?.lng ?? coordinates?.lon));
  const lat = Number(nested.latitude ?? nested.lat ?? (Array.isArray(coordinates) ? coordinates[1] : coordinates?.lat));
  return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? { lat, lon } : null;
}

function RouteSketch({ routes }: { routes: RouteOption[] }) {
  const all = routes.flatMap((route) => route.geometry.coordinates);
  const xs = all.map((point) => point[0]), ys = all.map((point) => point[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const width = maxX - minX || 1, height = maxY - minY || 1;
  return <svg viewBox="0 0 320 200" role="img" aria-label="Relative geometry of the returned route alternatives" style={{ width: "100%", maxHeight: 260, background: "var(--surface-2)", borderRadius: 12 }}>
    {routes.map((route, index) => <polyline key={route.id} fill="none" stroke={colors[index % colors.length]} strokeWidth={index === 0 ? 4 : 3} strokeOpacity={index === 0 ? 1 : 0.75}
      points={route.geometry.coordinates.map(([lon, lat]) => String(12 + (lon - minX) / width * 296) + "," + String(188 - (lat - minY) / height * 176)).join(" ")} />)}
    <text x="12" y="18" fill="var(--text-2)" fontSize="11">Start</text><text x="270" y="190" fill="var(--text-2)" fontSize="11">Destination</text>
  </svg>;
}

function RouteLegend({ routes }: { routes: RouteOption[] }) {
  return <div className="row wrap" aria-label="Route option colors" style={{ gap: 12 }}>
    {routes.map((route, index) => <span className="row xs" key={route.id} style={{ gap: 6 }}>
      <span aria-hidden="true" style={{ display: "inline-block", width: 18, height: 4, borderRadius: 4, background: colors[index % colors.length] }} />
      <span>Option {index + 1}</span>
    </span>)}
  </div>;
}

function RouteCard({ route, index }: { route: RouteOption; index: number }) {
  const counts = route.context.mapped_help_facilities_within_500m;
  const facilityCount = Object.values(counts).reduce((sum, count) => sum + count, 0);
  const transitCounts = route.context.mapped_transit_stations_within_500m ?? {};
  const transitCount = Object.values(transitCounts).reduce((sum, count) => sum + count, 0);
  const wheelchair = route.context.mapped_wheelchair_accessibility_within_500m ?? {};
  const mappedEvidence = route.context.mapped_feature_evidence ?? [];
  return <section className="panel panel-body" style={{ borderTop: "3px solid " + colors[index % colors.length] }}>
    <div className="row between"><b>Option {index + 1}</b><StatusBadge s={index === 0 ? "succeeded" : "candidate"} label={index === 0 ? "Router's first choice" : "Alternative"} /></div>
    <div style={{ fontSize: 22, fontWeight: 650, margin: "8px 0 4px" }}>{fmtNum(route.duration_s / 60)} min</div>
    <div className="small muted">{fmtNum(route.distance_m / 1000)} km</div>
    <div className="stack" style={{ gap: 5, marginTop: 12 }}>
      <span className="small">Open incident points within 250 m: <b>{route.context.open_incident_points_within_250m}</b></span>
      <span className="small">Mapped public-help facilities within 500 m: <b>{facilityCount}</b></span>
      {Object.entries(counts).map(([layer, count]) => <span key={layer} className="xs faint">{layer.replaceAll("_", " ")}: {count}</span>)}
      {!facilityCount && <span className="xs faint">No matching mapped features returned; this does not mean help is absent.</span>}
      <span className="small">Mapped transit stations within 500 m: <b>{transitCount}</b></span>
      {Object.entries(transitCounts).map(([layer, count]) => <span key={layer} className="xs faint">{layer.replaceAll("_", " ")}: {count}</span>)}
      {!transitCount && <span className="xs faint">No bus or rail stations were found in synced records near the sampled route; coverage may be incomplete.</span>}
      <span className="xs faint">Station counts do not show routes, schedules, accessibility, hours, or whether service is operating.</span>
      <span className="small">Nearby mapped wheelchair tags: {(["yes", "limited", "no", "designated", "unknown"] as const).map((value) => `${value} ${wheelchair[value] ?? 0}`).join(" · ")}</span>
      <span className="xs faint">Facility tags only; “unknown” means no recognized tag was mapped. This does not assess the route’s surfaces, slopes, curb ramps, crossings, or step-free continuity.</span>
      <details className="small">
        <summary>Inspect mapped facility evidence ({mappedEvidence.length}{route.context.mapped_feature_evidence_truncated ? "+" : ""})</summary>
        {mappedEvidence.length ? <ul className="stack" style={{ paddingLeft: 18, margin: "8px 0" }}>
          {mappedEvidence.map((item) => <li key={`${item.source_id}/${item.external_id}`}>
            <b>{item.name || item.layer.replaceAll("_", " ")}</b> · {item.distance_to_sampled_route_m} m from sampled route · wheelchair: {item.wheelchair || "unknown"}<br />
            <span className="xs faint">{item.source_id} · {item.provenance} · fetched {item.fetched_at ? new Date(item.fetched_at).toLocaleString() : "time unavailable"}</span><br />
            {item.source_id === "osm-overpass" && /^(node|way|relation)\/\d+$/.test(item.external_id)
              ? <a className="xs" href={`https://www.openstreetmap.org/${item.external_id}`} target="_blank" rel="noreferrer">Inspect OpenStreetMap feature ↗</a>
              : <code className="xs">{item.external_id}</code>}
          </li>)}
        </ul> : <p className="xs faint">No mapped facility or transit records were found within the context radius.</p>}
        {route.context.mapped_feature_evidence_truncated && <p className="xs faint">Evidence is capped at 25 nearest records; {route.context.context_truncated ? "the candidate scan also hit its cap, so counts and evidence may be incomplete." : "the counts above include all matching records in the context area."}</p>}
        {route.context.context_truncated && <p className="xs faint">The route context scan hit its record cap. Counts and evidence are incomplete lower bounds.</p>}
      </details>
      <div className="row wrap">{route.context.provenance.map((item) => <ProvenanceBadge key={item} p={item} />)}</div>
      <span className="xs faint">Context radius is measured from sampled route geometry; coverage depends on mapped records.</span>
    </div>
  </section>;
}

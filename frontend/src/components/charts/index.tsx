import { useEffect, useId, useMemo, useRef, useState } from "react";

import { fmtNum, fmtTime } from "../../lib/format";

type Pt = [string, number];

/** Render charts at their real pixel width so axis text stays legible in narrow panels. */
function useWidth(fallback = 640): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(240, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function scale(d0: number, d1: number, r0: number, r1: number) {
  const span = d1 - d0 || 1;
  return (v: number) => r0 + ((v - d0) / span) * (r1 - r0);
}

export function Sparkline({ points, color = "var(--cyan)", height = 36, label }: { points: Pt[]; color?: string; height?: number; label: string }) {
  const id = useId();
  if (points.length < 2) return <div className="xs faint" style={{ height }}>Not enough data</div>;
  const w = 160;
  const vals = points.map((p) => p[1]);
  const y = scale(Math.min(...vals), Math.max(...vals), height - 3, 3);
  const x = scale(0, points.length - 1, 0, w);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" height={height} preserveAspectRatio="none" role="img" aria-label={label}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.28" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d}L${w},${height}L0,${height}Z`} fill={`url(#${id})`} />
      <path d={d} fill="none" stroke={color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export interface LineSeries { name: string; points: Pt[]; color: string; dashed?: boolean }

export function LineChart({ series, height = 220, unit, label, yZero = false }: { series: LineSeries[]; height?: number; unit?: string; label: string; yZero?: boolean }) {
  const [hover, setHover] = useState<number | null>(null);
  const [boxRef, width] = useWidth();
  const all = series.flatMap((s) => s.points);
  const geom = useMemo(() => {
    if (!all.length) return null;
    const ts = all.map((p) => new Date(p[0]).getTime());
    const vs = all.map((p) => p[1]);
    const t0 = Math.min(...ts), t1 = Math.max(...ts);
    let v0 = Math.min(...vs), v1 = Math.max(...vs);
    if (yZero) v0 = Math.min(0, v0);
    if (v0 === v1) { v0 -= 1; v1 += 1; }
    const pad = (v1 - v0) * 0.08;
    return { t0, t1, v0: v0 - (yZero ? 0 : pad), v1: v1 + pad };
  }, [all, yZero]);
  if (!geom) return <div className="empty small" ref={boxRef}>No data for this range</div>;
  const W = width, H = height, L = 44, R = 10, T = 10, B = 24;
  const x = scale(geom.t0, geom.t1, L, W - R);
  const y = scale(geom.v0, geom.v1, H - B, T);
  const ticks = Array.from({ length: 4 }, (_, i) => geom.v0 + ((geom.v1 - geom.v0) * i) / 3);
  const nTicks = W < 420 ? 3 : 5;
  const xt = Array.from({ length: nTicks }, (_, i) => geom.t0 + ((geom.t1 - geom.t0) * i) / (nTicks - 1));
  const base = series[0]?.points ?? [];
  return (
    <div style={{ position: "relative" }} ref={boxRef}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={label}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const rect = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const tx = geom.t0 + (((e.clientX - rect.left) / rect.width) * W - L) / (W - L - R) * (geom.t1 - geom.t0);
          let best = 0;
          base.forEach((p, i) => { if (Math.abs(new Date(p[0]).getTime() - tx) < Math.abs(new Date(base[best][0]).getTime() - tx)) best = i; });
          setHover(base.length ? best : null);
        }}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="rgba(120,160,220,0.1)" />
            <text x={L - 6} y={y(t) + 3} textAnchor="end" fontSize="10" fill="var(--text-3)" fontFamily="var(--font-mono)">{fmtNum(t)}</text>
          </g>
        ))}
        {xt.map((t) => (
          <text key={t} x={x(t)} y={H - 6} textAnchor={t === geom.t0 ? "start" : t === geom.t1 ? "end" : "middle"} fontSize="10" fill="var(--text-3)">
            {new Date(t).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: geom.t1 - geom.t0 < 4 * 86400e3 ? "2-digit" : undefined })}
          </text>
        ))}
        {series.map((s) => (
          <path key={s.name} fill="none" stroke={s.color} strokeWidth="1.8" strokeDasharray={s.dashed ? "4 4" : undefined}
            d={s.points.map((p, i) => `${i ? "L" : "M"}${x(new Date(p[0]).getTime()).toFixed(1)},${y(p[1]).toFixed(1)}`).join("")} />
        ))}
        {hover != null && base[hover] && (
          <line x1={x(new Date(base[hover][0]).getTime())} x2={x(new Date(base[hover][0]).getTime())} y1={T} y2={H - B} stroke="rgba(230,237,247,0.3)" />
        )}
      </svg>
      {hover != null && base[hover] && (
        <div className="xs" style={{ position: "absolute", top: 4, right: 8, background: "var(--surface-solid)", border: "1px solid var(--line-strong)", borderRadius: 8, padding: "4px 8px" }}>
          <div className="faint">{fmtTime(base[hover][0])}</div>
          {series.map((s) => {
            const p = s.points.find((q) => q[0] === base[hover][0]);
            return p ? <div key={s.name}><span style={{ color: s.color }}>●</span> {s.name}: <b className="num">{fmtNum(p[1], 2)}</b> {unit}</div> : null;
          })}
        </div>
      )}
      {series.length > 1 && (
        <div className="legend" style={{ marginTop: 6 }}>
          {series.map((s) => <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>)}
        </div>
      )}
    </div>
  );
}

export function StackedBars({ data, keys, colors, height = 180, label }: { data: [string, Record<string, number>][]; keys: string[]; colors: Record<string, string>; height?: number; label: string }) {
  const [boxRef, W] = useWidth();
  const H = height, L = 30, B = 22, T = 8;
  const max = Math.max(1, ...data.map(([, v]) => keys.reduce((a, k) => a + (v[k] ?? 0), 0)));
  const bw = (W - L) / Math.max(1, data.length);
  const y = scale(0, max, H - B, T);
  return (
    <div ref={boxRef}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={label}>
        {[0, max / 2, max].map((t) => (
          <g key={t}>
            <line x1={L} x2={W} y1={y(t)} y2={y(t)} stroke="rgba(120,160,220,0.1)" />
            <text x={L - 4} y={y(t) + 3} textAnchor="end" fontSize="10" fill="var(--text-3)">{Math.round(t)}</text>
          </g>
        ))}
        {data.map(([d, v], i) => {
          let acc = 0;
          return (
            <g key={d}>
              <title>{`${d}: ${keys.map((k) => `${k} ${v[k] ?? 0}`).join(", ")}`}</title>
              {keys.map((k) => {
                const val = v[k] ?? 0;
                const y0 = y(acc), y1 = y(acc + val);
                acc += val;
                return val ? <rect key={k} x={L + i * bw + bw * 0.15} width={bw * 0.7} y={y1} height={Math.max(0, y0 - y1)} fill={colors[k]} rx="2" /> : null;
              })}
              {(i % Math.ceil(data.length / 8) === 0) && <text x={L + i * bw + bw / 2} y={H - 6} textAnchor="middle" fontSize="10" fill="var(--text-3)">{d.slice(5)}</text>}
            </g>
          );
        })}
      </svg>
      <div className="legend">{keys.map((k) => <span key={k}><i style={{ background: colors[k] }} />{k}</span>)}</div>
    </div>
  );
}

export function BarList({ items, color = "var(--cyan)", format = (v: number) => String(v), max }: { items: { label: string; value: number; hint?: string }[]; color?: string; format?: (v: number) => string; max?: number }) {
  const m = max ?? Math.max(1, ...items.map((i) => i.value));
  if (!items.length) return <div className="small faint">No data</div>;
  return (
    <div className="stack" style={{ gap: 7 }}>
      {items.map((i) => (
        <div key={i.label} title={i.hint}>
          <div className="row between small"><span className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{i.label}</span><b className="num">{format(i.value)}</b></div>
          <div className="progress" style={{ height: 5 }}><span style={{ width: `${Math.min(100, (i.value / m) * 100)}%`, background: color }} /></div>
        </div>
      ))}
    </div>
  );
}

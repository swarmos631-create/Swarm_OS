import { useId, type ReactNode } from "react";

/** Speedometer-style gauge: a 240° arc with a green→amber→red track, tick marks and a needle. */
export function Gauge({ value, max, label, unit, display, bands = ["#7ee08a", "#f5c04a", "#ff6b5b"] }: {
  value: number | null | undefined; max: number; label: string; unit?: string; display?: ReactNode; bands?: [string, string, string] | string[];
}) {
  const id = useId();
  const size = 168;
  const c = size / 2;
  const r = 64;
  const start = 150; // degrees, measured clockwise from 3 o'clock
  const sweep = 240;
  const v = value == null ? null : Math.max(0, Math.min(max, value));
  const pt = (deg: number, rad: number) => [c + rad * Math.cos((deg * Math.PI) / 180), c + rad * Math.sin((deg * Math.PI) / 180)];
  const arc = (from: number, to: number, rad: number) => {
    const [x0, y0] = pt(from, rad);
    const [x1, y1] = pt(to, rad);
    return `M${x0.toFixed(2)},${y0.toFixed(2)}A${rad},${rad} 0 ${to - from > 180 ? 1 : 0} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
  };
  const ang = v == null ? start : start + (v / max) * sweep;
  const [nx, ny] = pt(ang, r - 22);
  const ticks = Array.from({ length: 25 }, (_, i) => start + (i * sweep) / 24);
  return (
    <figure className="gauge" aria-label={`${label}: ${v == null ? "not available" : `${v}${unit ? ` ${unit}` : ""}`}`}>
      <svg viewBox={`0 0 ${size} ${size - 18}`} width="100%" role="img" aria-hidden="true">
        <defs>
          <linearGradient id={id} x1="0" y1="1" x2="1" y2="0">
            <stop offset="0" stopColor={bands[0]} />
            <stop offset="0.55" stopColor={bands[1]} />
            <stop offset="1" stopColor={bands[2]} />
          </linearGradient>
        </defs>
        <path d={arc(start, start + sweep, r)} stroke="rgba(255,255,255,0.08)" strokeWidth="9" fill="none" strokeLinecap="round" />
        {v != null && v > 0 && <path d={arc(start, ang, r)} stroke={`url(#${id})`} strokeWidth="9" fill="none" strokeLinecap="round" />}
        {ticks.map((t, i) => {
          const [x0, y0] = pt(t, r - 10);
          const [x1, y1] = pt(t, r - (i % 4 === 0 ? 17 : 13));
          return <line key={i} x1={x0} y1={y0} x2={x1} y2={y1} stroke="rgba(255,255,255,0.28)" strokeWidth={i % 4 === 0 ? 1.4 : 0.8} />;
        })}
        {v != null && (
          <>
            <line x1={c} y1={c} x2={nx} y2={ny} stroke="#f4f4f5" strokeWidth="3" strokeLinecap="round" />
            <circle cx={c} cy={c} r="5.5" fill="#f4f4f5" />
          </>
        )}
      </svg>
      <figcaption>
        <b>{v == null ? "—" : display ?? v}</b>{v != null && unit && <span> {unit}</span>}
      </figcaption>
    </figure>
  );
}

/** Toggle switch styled like the reference dashboard (green when on). */
export function Switch({ checked, onChange, label, testId }: { checked: boolean; onChange: (v: boolean) => void; label: string; testId?: string }) {
  return (
    <label className="switch-row">
      <span>{label}</span>
      <button type="button" role="switch" aria-checked={checked} className="switch" onClick={() => onChange(!checked)} data-testid={testId}>
        <span />
      </button>
    </label>
  );
}

/** Segmented filter pills with count badges ("All 10 · Active 6 · …"). */
export function Segmented<T extends string>({ options, value, onChange, label }: {
  options: { id: T; label: string; count?: number }[]; value: T; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.id} type="button" aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}{o.count != null && <span className="count">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

/** Striped progress bar (amber by default), as in the route-progress card of the reference. */
export function Meter({ value, max = 100, color, label }: { value: number; max?: number; color?: string; label: string }) {
  const pct = Math.max(0, Math.min(100, (value / (max || 1)) * 100));
  return (
    <div className="meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
      <span style={{ width: `${pct}%`, ...(color ? { background: color } : {}) }} />
    </div>
  );
}

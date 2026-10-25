import { cloneElement, isValidElement, useEffect, useId, useRef, type ReactNode } from "react";

import { ApiError } from "../../api/client";
import type { Freshness, Provenance } from "../../api/types";
import { ago, duration, titleCase } from "../../lib/format";
import { Icon, type IconName } from "../../lib/icons";

export function Panel({ title, icon, actions, children, className = "", flush = false, id }: {
  title?: ReactNode; icon?: IconName; actions?: ReactNode; children: ReactNode; className?: string; flush?: boolean; id?: string;
}) {
  return (
    <section className={`panel ${className}`} aria-labelledby={title ? `${id ?? ""}h` : undefined} id={id}>
      {title && (
        <header className="panel-head">
          <h2 id={`${id ?? ""}h`}>{icon && <Icon name={icon} width={16} height={16} />}{title}</h2>
          {actions && <div className="row">{actions}</div>}
        </header>
      )}
      <div className={`panel-body ${flush ? "flush" : ""}`}>{children}</div>
    </section>
  );
}

const PROV_LABEL: Record<string, string> = {
  live: "Live", cached: "Cached", demo: "Simulated", synthetic: "Synthetic", operator: "Operator", derived: "Derived", records: "Records", mixed: "Mixed",
};

/** Every value shown in SwarmOS carries a provenance badge; demo/synthetic is always visually distinct. */
export function ProvenanceBadge({ p, title }: { p: Provenance | undefined | null; title?: string }) {
  if (!p) return null;
  const cls = p === "synthetic" ? "b-demo" : p === "mixed" ? "b-records" : `b-${p}`;
  const tip = title ?? ({
    live: "Fetched from a connected external source", demo: "Simulated — generated from climate normals and city size, not a live reading", synthetic: "SYNTHETIC data — not real",
    operator: "Entered by an authorised operator", derived: "Computed by SwarmOS from other data", cached: "Previously synchronised data",
  } as Record<string, string>)[p];
  return <span className={`badge ${cls}`} title={tip}><span className="dot" />{PROV_LABEL[p] ?? p}</span>;
}

export function SeverityBadge({ s }: { s: string }) {
  return <span className={`badge sev-${s}`}>{s}</span>;
}

const STATUS_CLASS: Record<string, string> = {
  succeeded: "b-ok", executed: "b-ok", connected: "b-ok", healthy: "b-ok", resolved: "b-ok", closed: "b-unavailable", ok: "b-ok", promoted: "b-ok", fresh: "b-ok",
  running: "b-info", queued: "b-info", pending: "b-info", acknowledged: "b-info", in_progress: "b-info", configured: "b-info", approved: "b-info",
  partial: "b-demo", degraded: "b-demo", awaiting_manual: "b-demo", awaiting_credentials: "b-demo", awaiting_access: "b-demo", stale: "b-stale", candidate: "b-records",
  demo: "b-demo", internal: "b-records", open: "b-stale",
  failed: "b-error", failing: "b-error", timed_out: "b-error", rejected: "b-unavailable", cancelled: "b-unavailable", unavailable: "b-unavailable",
  disabled: "b-unavailable", idle: "b-unavailable", never: "b-unavailable", archived: "b-unavailable",
};

export function StatusBadge({ s, label }: { s: string; label?: string }) {
  return <span className={`badge ${STATUS_CLASS[s] ?? "b-unavailable"}`}><span className="dot" />{label ?? (s === "demo" ? "Simulated" : titleCase(s))}</span>;
}

export function FreshnessBadge({ f }: { f: Freshness | null | undefined }) {
  if (!f) return null;
  if (f.state === "never") return <span className="badge b-unavailable">Never synced</span>;
  return (
    <span className={`badge ${f.state === "fresh" ? "b-live" : "b-stale"}`} title={`Threshold ${duration(f.threshold_s)}`}>
      <span className="dot" />{f.state === "fresh" ? "Fresh" : "Stale"} · {duration(f.age_s)}
    </span>
  );
}

export function Skeleton({ h = 16, w = "100%", style }: { h?: number; w?: number | string; style?: React.CSSProperties }) {
  return <div className="skeleton" style={{ height: h, width: w, ...style }} aria-hidden="true" />;
}

export function LoadingBlock({ rows = 3 }: { rows?: number }) {
  return (
    <div className="stack" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} h={18} w={`${90 - i * 12}%`} />)}
    </div>
  );
}

export function EmptyState({ title, children, icon = "info", action }: { title: string; children?: ReactNode; icon?: IconName; action?: ReactNode }) {
  return (
    <div className="empty">
      <Icon name={icon} />
      <h3>{title}</h3>
      {children && <div className="small" style={{ maxWidth: 460 }}>{children}</div>}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const e = error as ApiError;
  return (
    <div className="notice error" role="alert">
      <Icon name="alert" />
      <div className="stack" style={{ gap: 4 }}>
        <b>{e?.status === 0 ? "Connection problem" : "Something went wrong"}</b>
        <span>{e?.message ?? String(error)}</span>
        {e?.correlationId && <span className="xs faint">Reference: <code>{e.correlationId}</code></span>}
        {onRetry && <div><button className="btn sm" onClick={onRetry}><Icon name="refresh" />Retry</button></div>}
      </div>
    </div>
  );
}

export function Dialog({ title, onClose, children, footer, wide }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>("input, textarea, select, button:not([data-close])");
    (first ?? ref.current)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && ref.current) {
        const f = Array.from(ref.current.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")).filter((el) => !el.hasAttribute("disabled"));
        if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); prev?.focus(); };
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={id} ref={ref} tabIndex={-1} style={wide ? { width: "min(820px, 100%)" } : undefined}>
        <header className="panel-head">
          <h2 id={id}>{title}</h2>
          <button className="btn ghost icon" onClick={onClose} aria-label="Close dialog" data-close><Icon name="close" /></button>
        </header>
        <div className="panel-body">{children}</div>
        {footer && <div className="dialog-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function ConfirmDialog({ title, body, confirmLabel, danger, busy, onConfirm, onClose, children }: {
  title: string; body: ReactNode; confirmLabel: string; danger?: boolean; busy?: boolean; onConfirm: () => void; onClose: () => void; children?: ReactNode;
}) {
  return (
    <Dialog title={title} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
      <button className={`btn ${danger ? "danger" : "primary"}`} onClick={onConfirm} disabled={busy}>{busy ? "Working…" : confirmLabel}</button>
    </>}>
      <div className="stack">{body}{children}</div>
    </Dialog>
  );
}

export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="overlay light" style={{ zIndex: 44 }} onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : "Details"}>
        <header className="panel-head" style={{ position: "sticky", top: 0, background: "var(--surface-solid)", zIndex: 1 }}>
          <h2>{title}</h2>
          <button className="btn ghost icon" onClick={onClose} aria-label="Close panel"><Icon name="close" /></button>
        </header>
        <div className="panel-body">{children}</div>
      </aside>
    </>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: { id: T; label: string }[]; value: T; onChange: (v: T) => void; label: string }) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} role="tab" className="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}>{t.label}</button>
      ))}
    </div>
  );
}

export function Timestamp({ iso, prefix }: { iso: string | null | undefined; prefix?: string }) {
  if (!iso) return <span className="faint">{prefix} never</span>;
  return <time dateTime={iso} title={new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" }) + " IST"}>{prefix} {ago(iso)}</time>;
}

/** Labelled form field: links <label> to the control and wires hint/error via aria-describedby. */
export function Field({ label, error, children, hint }: { label: string; error?: string | null; children: ReactNode; hint?: string }) {
  const autoId = useId();
  const child = isValidElement<Record<string, unknown>>(children) ? children : null;
  const id = (child?.props.id as string | undefined) ?? autoId;
  const descId = `${id}-desc`;
  const control = child
    ? cloneElement(child, { id, "aria-invalid": error ? true : undefined, "aria-describedby": hint || error ? descId : undefined })
    : children;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {control}
      {hint && !error && <span id={descId} className="xs faint">{hint}</span>}
      {error && <span id={descId} className="err" role="alert">{error}</span>}
    </div>
  );
}

export function OfflineGuard({ online, children }: { online: boolean; children: ReactNode }) {
  return online ? <>{children}</> : <span title="Unavailable while offline — actions are not queued">{children}</span>;
}

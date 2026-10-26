import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api } from "../../api/client";
import { dashboardQuery } from "../../api/queries";
import { ago } from "../../lib/format";
import { Icon, type IconName } from "../../lib/icons";
import { useCity } from "../../state/city";
import { useSession } from "../../state/session";
import { useTheme, type ThemePref } from "../../state/theme";
import { useOnline } from "../../state/ui";
import { CitySelector } from "./CitySelector";
import { PwaControls } from "./PwaControls";

const NAV: { to: string; label: string; icon: IconName; color: string; section?: string }[] = [
  { to: "", label: "Command Center", color: "var(--violet)", icon: "command", section: "Operate" },
  { to: "map", label: "Operations Map", color: "var(--cyan)", icon: "map" },
  { to: "incidents", label: "Incidents", color: "var(--red)", icon: "incidents" },
  { to: "workspace", label: "AI Assistant", color: "var(--pink)", icon: "workspace", section: "Intelligence" },
  { to: "agents", label: "Agent Fleet", color: "var(--orange)", icon: "agents" },
  { to: "mission-control", label: "Mission Control", color: "var(--pink)", icon: "grid" },
  { to: "scenarios", label: "Scenario Lab", color: "var(--amber)", icon: "analytics" },
  { to: "safe-route", label: "SafeRoute", color: "var(--cyan)", icon: "map" },
  { to: "reports", label: "Situation Reports", color: "var(--blue)", icon: "analytics" },
  { to: "analytics", label: "Analytics", color: "var(--blue)", icon: "analytics" },
  { to: "integrations", label: "Data Integrations", color: "var(--teal)", icon: "integrations", section: "Platform" },
  { to: "models", label: "Model Lab", color: "var(--amber)", icon: "models" },
  { to: "system", label: "System", color: "var(--green)", icon: "system" },
];

function Notifications({ cityId }: { cityId: string }) {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["notifications", cityId],
    queryFn: () => api<{ items: { id: number; level: string; title: string; body: string; created_at: string }[] }>("/notifications", { query: { city_id: cityId } }),
    refetchInterval: 30_000,
  });
  const n = q.data?.items.length ?? 0;
  return (
    <div style={{ position: "relative" }}>
      <button className="round-btn" aria-label={`Notifications (${n})`} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon name="bell" />
        {n > 0 && <span className="count-dot">+{Math.min(n, 9)}</span>}
      </button>
      {open && (
        <div className="panel popover" style={{ position: "absolute", right: 0, top: 42, width: "min(340px, calc(100vw - 24px))", zIndex: 30, maxHeight: 420, overflowY: "auto" }}>
          <ul className="list">
            {(q.data?.items ?? []).map((x) => (
              <li key={x.id}>
                <span className={`badge ${x.level === "warning" ? "b-demo" : "b-info"}`}>{x.level}</span>
                <div className="stack" style={{ gap: 2 }}><b className="small">{x.title}</b><span className="xs muted">{x.body}</span><span className="xs faint">{ago(x.created_at)}</span></div>
              </li>
            ))}
            {!n && <li className="small faint">No notifications.</li>}
          </ul>
        </div>
      )}
    </div>
  );
}

const THEMES: { id: ThemePref; label: string; icon: IconName }[] = [
  { id: "light", label: "Light theme", icon: "sun" },
  { id: "dark", label: "Dark theme", icon: "moon" },
  { id: "system", label: "Match system theme", icon: "monitor" },
];

/** Light / dark / system appearance switch, remembered on this device. */
export function ThemeSwitch() {
  const { pref, setPref } = useTheme();
  return (
    <div className="theme-switch" role="group" aria-label="Appearance">
      {THEMES.map((t) => (
        <button key={t.id} type="button" aria-pressed={pref === t.id} onClick={() => setPref(t.id)} title={t.label} aria-label={t.label} data-testid={`theme-${t.id}`}>
          <Icon name={t.icon} />
        </button>
      ))}
    </div>
  );
}

/** Live summary pills across the top bar ("Active: 6/10" style). */
function StatPills({ cityId }: { cityId: string }) {
  const q = useQuery({ ...dashboardQuery(cityId), refetchInterval: 60_000 });
  const d = q.data;
  if (!d) return <div className="stat-pills hide-sm" aria-hidden="true"><span className="pill skeleton" style={{ width: 360 }} /></div>;
  const sources = d.source_health.filter((s) => s.applies_to_city);
  const ok = sources.filter((s) => ["connected", "internal", "demo"].includes(s.status)).length;
  const aqi = d.indicators.find((i) => i.id === "aqi");
  const runs = Object.values(d.agents.executions_24h).reduce((a, b) => a + b, 0);
  return (
    <div className="stat-pills hide-sm" aria-label="City summary">
      <span className="pill" style={{ ["--c" as string]: "var(--red)" }}><Icon name="incidents" />Incidents: <b>{d.incidents.open}</b></span>
      <span className="pill" style={{ ["--c" as string]: "var(--orange)" }}><Icon name="alert" />Critical: <b>{d.incidents.by_severity.critical}</b></span>
      <span className="pill" style={{ ["--c" as string]: "var(--teal)" }}><Icon name="integrations" />Sources: <b>{ok}/{sources.length}</b></span>
      <span className="pill" style={{ ["--c" as string]: "var(--violet)" }}><Icon name="agents" />Agent runs 24h: <b>{runs}</b></span>
      {aqi?.status === "available" && <span className="pill" style={{ ["--c" as string]: "var(--cyan)" }}><Icon name="analytics" />AQI: <b>{String(aqi.value)}</b></span>}
    </div>
  );
}

export function AppShell() {
  const { cityId } = useCity();
  const { me, logout } = useSession();
  const online = useOnline();
  const [menu, setMenu] = useState(false);
  const loc = useLocation();
  useEffect(() => setMenu(false), [loc.pathname]);
  const initials = (me?.display_name ?? "?").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();

  return (
    <div className="shell">
      <a href="#main" className="sr-only">Skip to content</a>
      <aside className={`sidebar ${menu ? "open" : ""}`} aria-label="Primary navigation">
        <div className="brand" title="SwarmOS — City operations">
          <img src="/icons/favicon.svg" alt="" />
          <div className="brand-text"><b>SwarmOS</b><small>City operations</small></div>
        </div>
        <nav className="rail-nav">
          {NAV.map((n) => (
            <Section key={n.label} title={n.section}>
              <NavLink end={n.to === ""} to={`/c/${cityId}${n.to ? `/${n.to}` : ""}`} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`} title={n.label} style={{ ["--c" as string]: n.color }}>
                <span className="nav-ico"><Icon name={n.icon} /></span><span className="nav-label">{n.label}</span>
              </NavLink>
            </Section>
          ))}
        </nav>
        <div className="sidebar-foot">
          <NavLink to={`/c/${cityId}/settings`} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`} title="Settings — colour themes and appearance" style={{ ["--c" as string]: "var(--accent)" }}><span className="nav-ico"><Icon name="settings" /></span><span className="nav-label">Settings</span></NavLink>
          <a className="nav-link" style={{ ["--c" as string]: "var(--text-2)" }} href="/api/health/ready" target="_blank" rel="noreferrer" title="API health"><span className="nav-ico"><Icon name="help" /></span><span className="nav-label">API health</span></a>
          {!me?.auth_disabled && <button className="nav-link" style={{ ["--c" as string]: "var(--text-2)" }} onClick={() => void logout()} title="Sign out"><span className="nav-ico"><Icon name="logout" /></span><span className="nav-label">Sign out</span></button>}
        </div>
      </aside>
      {menu && <div className="overlay" style={{ zIndex: 39 }} onClick={() => setMenu(false)} />}
      <div className="main">
        <header className="topbar">
          <button className="round-btn mobile-only" aria-label="Open navigation" onClick={() => setMenu(true)}><Icon name="menu" /></button>
          <StatPills cityId={cityId} />
          <div className="spacer" />
          <CitySelector />
          <span className={`pill hide-sm ${online ? "" : "warn"}`} aria-live="polite"><span className={`pulse ${online ? "" : "off"}`} />{online ? "Online" : "Offline"}</span>
          <ThemeSwitch />
          <NavLink to={`/c/${cityId}/settings`} className="round-btn" aria-label="Colour themes" title="Colour themes"><Icon name="palette" /></NavLink>
          <PwaControls />
          <Notifications cityId={cityId} />
          <div className="user-chip hide-sm" title={me?.email}>
            <span className="avatar">{initials}</span>
            <span className="stack" style={{ gap: 0 }}><b>{me?.display_name}</b><span className="xs faint">{me?.role === "admin" ? "Administrator" : me?.role}{me?.environment !== "production" ? " · dev" : ""}</span></span>
          </div>
        </header>
        {!online && (
          <div className="content" style={{ paddingBottom: 0 }}>
            <div className="notice offline" role="status">
              <Icon name="wifiOff" />
              <span>You are offline. Data shown is the last copy fetched in this session and may be out of date — check each panel's timestamp. Actions are disabled until you reconnect; nothing is queued.</span>
            </div>
          </div>
        )}
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function Section({ title, children }: { title?: string; children: ReactNode }) {
  return <>{title && <div className="nav-section" aria-hidden="true"><span>{title}</span></div>}{children}</>;
}

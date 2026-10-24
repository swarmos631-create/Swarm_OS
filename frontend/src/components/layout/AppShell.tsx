import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api } from "../../api/client";
import { ago } from "../../lib/format";
import { Icon, type IconName } from "../../lib/icons";
import { useCity } from "../../state/city";
import { useSession } from "../../state/session";
import { useOnline } from "../../state/ui";
import { CitySelector } from "./CitySelector";
import { PwaControls } from "./PwaControls";

const NAV: { to: string; label: string; icon: IconName; section?: string }[] = [
  { to: "", label: "Command Center", icon: "command", section: "Operate" },
  { to: "map", label: "Operations Map", icon: "map" },
  { to: "incidents", label: "Incidents", icon: "incidents" },
  { to: "workspace", label: "AI Workspace", icon: "workspace", section: "Intelligence" },
  { to: "agents", label: "Agent Fleet", icon: "agents" },
  { to: "analytics", label: "Analytics", icon: "analytics" },
  { to: "integrations", label: "Data Integrations", icon: "integrations", section: "Platform" },
  { to: "models", label: "Model Lab", icon: "models" },
  { to: "system", label: "System", icon: "system" },
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
      <button className="btn ghost icon" aria-label={`Notifications (${n})`} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon name="bell" />
        {n > 0 && <span className="badge b-info" style={{ position: "absolute", top: -2, right: -6, height: 16, padding: "0 5px" }}>{Math.min(n, 9)}</span>}
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

export function AppShell() {
  const { cityId } = useCity();
  const { me, logout } = useSession();
  const online = useOnline();
  const [menu, setMenu] = useState(false);
  const loc = useLocation();
  useEffect(() => setMenu(false), [loc.pathname]);

  return (
    <div className="shell">
      <a href="#main" className="sr-only">Skip to content</a>
      <aside className={`sidebar ${menu ? "open" : ""}`} aria-label="Primary navigation">
        <div className="brand">
          <img src="/icons/favicon.svg" alt="" />
          <div><b>SwarmOS</b><small>City operations</small></div>
        </div>
        <nav className="stack" style={{ gap: 2 }}>
          {NAV.map((n) => (
            <Section key={n.label} title={n.section}>
              <NavLink end={n.to === ""} to={`/c/${cityId}${n.to ? `/${n.to}` : ""}`} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
                <Icon name={n.icon} />{n.label}
              </NavLink>
            </Section>
          ))}
        </nav>
        <div className="sidebar-foot stack">
          <div className="row"><b className="small">{me?.display_name}</b><span className="badge b-records">{me?.role}</span></div>
          <span className="xs faint" style={{ overflowWrap: "anywhere" }}>{me?.email}</span>
          {me?.environment !== "production" && <span className="xs" style={{ color: "var(--amber)" }}>Development environment{me?.demo_mode ? " · demo mode" : ""}</span>}
          {!me?.auth_disabled && <button className="btn sm" onClick={() => void logout()}><Icon name="logout" />Sign out</button>}
        </div>
      </aside>
      {menu && <div className="overlay" style={{ zIndex: 39 }} onClick={() => setMenu(false)} />}
      <div className="main">
        <header className="topbar">
          <button className="btn ghost icon mobile-only" aria-label="Open navigation" onClick={() => setMenu(true)}><Icon name="menu" /></button>
          <CitySelector />
          <div className="spacer" />
          <span className="row small hide-sm" aria-live="polite">
            <span className={`pulse ${online ? "" : "off"}`} />{online ? "Online" : "Offline"}
          </span>
          <PwaControls />
          <Notifications cityId={cityId} />
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
  return <>{title && <div className="nav-section">{title}</div>}{children}</>;
}

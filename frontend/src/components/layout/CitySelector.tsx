import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import type { City } from "../../api/types";
import { Icon } from "../../lib/icons";
import { useCities, useCity } from "../../state/city";

/** Accessible combobox. Switching city rewrites the /c/:cityId segment so every page, query and map follows. */
export function CitySelector() {
  const { cityId, city } = useCity();
  const cities = useCities();
  const nav = useNavigate();
  const loc = useLocation();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const results = useMemo(() => {
    const list = cities.data?.items ?? [];
    const s = q.trim().toLowerCase();
    if (!s) return list;
    return list.filter((c) => c.name.toLowerCase().includes(s) || c.state.toLowerCase().includes(s) || c.aliases.some((a) => a.toLowerCase().includes(s)));
  }, [cities.data, q]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => !boxRef.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    setTimeout(() => inputRef.current?.focus(), 0);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const choose = (c: City) => {
    setOpen(false);
    setQ("");
    const rest = loc.pathname.replace(/^\/c\/[^/]+/, "");
    nav(`/c/${c.id}${rest}${loc.search}`);
  };

  return (
    <div className="city-select" ref={boxRef} style={{ position: "relative" }}>
      <button className="btn" onClick={() => setOpen((o) => !o)} aria-haspopup="listbox" aria-expanded={open} aria-label={`Selected city: ${city?.name ?? cityId}. Change city`}
        data-testid="city-selector">
        <Icon name="pin" />
        <span style={{ fontWeight: 600 }}>{city?.name ?? cityId}</span>
        <span className="faint hide-sm small">{city?.state}</span>
        <Icon name="chevronDown" />
      </button>
      {open && (
        <div className="panel popover" style={{ position: "absolute", top: 40, left: 0, width: "min(340px, calc(100vw - 24px))", zIndex: 30, padding: 8 }}>
          <input ref={inputRef} className="input" placeholder="Search Indian cities or states…" value={q} role="combobox" aria-expanded="true"
            aria-controls="city-listbox" aria-activedescendant={results[active] ? `city-opt-${results[active].id}` : undefined}
            onChange={(e) => { setQ(e.target.value); setActive(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, results.length - 1)); }
              if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
              if (e.key === "Enter" && results[active]) choose(results[active]);
              if (e.key === "Escape") setOpen(false);
            }} />
          <ul id="city-listbox" role="listbox" className="list" style={{ maxHeight: 340, overflowY: "auto", marginTop: 6 }}>
            {results.map((c, i) => (
              <li key={c.id} id={`city-opt-${c.id}`} role="option" aria-selected={c.id === cityId}
                onMouseEnter={() => setActive(i)} onClick={() => choose(c)}
                style={{ cursor: "pointer", padding: "7px 10px", borderRadius: 8, background: i === active ? "var(--surface-hover)" : undefined, borderBottom: 0 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: c.id === cityId ? 700 : 500 }}>{c.name}</div>
                  <div className="xs faint">{c.state}{c.aliases.length ? ` · ${c.aliases.join(", ")}` : ""}</div>
                </div>
                <div className="row" style={{ gap: 4 }}>
                  {c.has_live_data && <span className="badge b-live" title="Live data has been synchronised">Live</span>}
                  {c.has_demo_data && <span className="badge b-demo" title="Has synthetic demo fixtures">Demo</span>}
                </div>
              </li>
            ))}
            {!results.length && <li className="small faint">No matching city in the catalog.</li>}
          </ul>
          <p className="xs faint" style={{ padding: "6px 4px 0" }}>
            {cities.data?.total ?? 0} cities in the catalog. Listing a city does not imply live integrations — check its Integrations page.
          </p>
        </div>
      )}
    </div>
  );
}

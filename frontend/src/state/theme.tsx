import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type ThemePref = "system" | "light" | "dark";
export type Theme = "light" | "dark";

/** Colour themes: each replaces the brand gradient, accents, glows and highlights across the whole site. */
export const ACCENTS = [
  { id: "aurora", name: "Aurora", hint: "Violet → pink", from: "#8b6cff", to: "#ff5cad" },
  { id: "ocean", name: "Ocean", hint: "Blue → cyan", from: "#3b82f6", to: "#22d3ee" },
  { id: "emerald", name: "Emerald", hint: "Green → lime", from: "#10b981", to: "#a3e635" },
  { id: "sunset", name: "Sunset", hint: "Orange → rose", from: "#fb923c", to: "#f43f5e" },
  { id: "rose", name: "Rose", hint: "Rose → purple", from: "#f43f5e", to: "#a855f7" },
  { id: "saffron", name: "Saffron", hint: "Amber → orange", from: "#fbbf24", to: "#f97316" },
  { id: "indigo", name: "Indigo", hint: "Indigo → sky", from: "#6366f1", to: "#0ea5e9" },
  { id: "tricolour", name: "Tiranga", hint: "Saffron → green", from: "#ff9933", to: "#138808" },
  { id: "graphite", name: "Graphite", hint: "Monochrome", from: "#71717a", to: "#d4d4d8" },
] as const;
export type Accent = (typeof ACCENTS)[number]["id"];

const KEY = "swarmos.theme";
const ACCENT_KEY = "swarmos.accent";
const media = () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null);

function read<T extends string>(key: string, allowed: readonly string[], fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    if (v && allowed.includes(v)) return v as T;
  } catch { /* storage unavailable (private mode, blocked) */ }
  return fallback;
}

function write(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* not persisted; still applied for this visit */ }
}

function systemTheme(): Theme {
  return media()?.matches ? "light" : "dark";
}

function apply(t: Theme, accent: Accent) {
  const root = document.documentElement;
  root.dataset.theme = t;
  root.dataset.accent = accent;
  root.style.colorScheme = t;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t === "dark" ? "#0c0e1f" : "#f5f6fc");
}

interface ThemeCtx { pref: ThemePref; theme: Theme; accent: Accent; setPref: (p: ThemePref) => void; setAccent: (a: Accent) => void }
const Ctx = createContext<ThemeCtx>({ pref: "system", theme: "dark", accent: "aurora", setPref: () => {}, setAccent: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPrefState] = useState<ThemePref>(() => read<ThemePref>(KEY, ["system", "light", "dark"], "system"));
  const [accent, setAccentState] = useState<Accent>(() => read<Accent>(ACCENT_KEY, ACCENTS.map((a) => a.id), "aurora"));
  const [sys, setSys] = useState<Theme>(systemTheme);
  const theme: Theme = pref === "system" ? sys : pref;

  useEffect(() => {
    const m = media();
    if (!m) return;
    const on = () => setSys(m.matches ? "light" : "dark");
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);

  useEffect(() => apply(theme, accent), [theme, accent]);

  const setPref = useCallback((p: ThemePref) => { setPrefState(p); write(KEY, p); }, []);
  const setAccent = useCallback((a: Accent) => { setAccentState(a); write(ACCENT_KEY, a); }, []);

  return <Ctx.Provider value={{ pref, theme, accent, setPref, setAccent }}>{children}</Ctx.Provider>;
}

export const useTheme = () => useContext(Ctx);

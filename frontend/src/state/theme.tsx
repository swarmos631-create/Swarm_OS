import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type ThemePref = "system" | "light" | "dark";
export type Theme = "light" | "dark";

const KEY = "swarmos.theme";
const media = () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null);

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch { /* storage unavailable (private mode, blocked) */ }
  return "system";
}

function systemTheme(): Theme {
  return media()?.matches ? "light" : "dark";
}

function apply(t: Theme) {
  const root = document.documentElement;
  root.dataset.theme = t;
  root.style.colorScheme = t;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t === "dark" ? "#0c0e1f" : "#f5f6fc");
}

const Ctx = createContext<{ pref: ThemePref; theme: Theme; setPref: (p: ThemePref) => void }>({ pref: "system", theme: "dark", setPref: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPrefState] = useState<ThemePref>(readPref);
  const [sys, setSys] = useState<Theme>(systemTheme);
  const theme: Theme = pref === "system" ? sys : pref;

  useEffect(() => {
    const m = media();
    if (!m) return;
    const on = () => setSys(m.matches ? "light" : "dark");
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);

  useEffect(() => apply(theme), [theme]);

  const setPref = useCallback((p: ThemePref) => {
    setPrefState(p);
    try { localStorage.setItem(KEY, p); } catch { /* not persisted; still applied for this visit */ }
  }, []);

  return <Ctx.Provider value={{ pref, theme, setPref }}>{children}</Ctx.Provider>;
}

export const useTheme = () => useContext(Ctx);

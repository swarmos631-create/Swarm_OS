import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useParams } from "react-router-dom";

import { api } from "../api/client";
import type { City } from "../api/types";

const KEY = "swarmos.city";

export function rememberCity(id: string) {
  try {
    localStorage.setItem(KEY, id);
  } catch {
    /* storage unavailable (private mode) — preference simply isn't persisted */
  }
}

export function rememberedCity(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function useCities() {
  return useQuery({ queryKey: ["cities"], queryFn: () => api<{ items: City[]; total: number }>("/cities"), staleTime: 10 * 60_000 });
}

interface CityValue {
  cityId: string;
  city: City | undefined;
}

const CityCtx = createContext<CityValue | null>(null);

/** The city comes from the URL (/c/:cityId/...), so deep links and navigation always carry the right context. */
export function CityProvider({ children }: { children: ReactNode }) {
  const { cityId = "" } = useParams();
  const cities = useCities();
  const city = cities.data?.items.find((c) => c.id === cityId);
  useEffect(() => {
    if (city) rememberCity(city.id);
  }, [city]);
  return <CityCtx.Provider value={{ cityId, city }}>{children}</CityCtx.Provider>;
}

export function useCity(): CityValue {
  const v = useContext(CityCtx);
  if (!v) throw new Error("useCity outside CityProvider");
  return v;
}

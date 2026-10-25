/** Query definitions shared between components and the start-up prefetch in main.tsx. */

import { keepPreviousData } from "@tanstack/react-query";

import { api } from "./client";
import type { City, Dashboard } from "./types";

export const citiesQuery = {
  queryKey: ["cities"],
  queryFn: () => api<{ items: City[]; total: number }>("/cities"),
  staleTime: 10 * 60_000,
};

// While a newly chosen city loads, pages keep showing the previous city's data (dimmed) instead of going blank.
export const dashboardQuery = (cityId: string) => ({
  queryKey: ["dashboard", cityId],
  queryFn: () => api<Dashboard>(`/cities/${cityId}/dashboard`),
  placeholderData: keepPreviousData,
});

export const mapLayersQuery = <T,>(cityId: string) => ({
  queryKey: ["map-layers", cityId],
  queryFn: () => api<T>(`/cities/${cityId}/map/layers`),
  staleTime: 5 * 60_000,
  placeholderData: keepPreviousData,
});

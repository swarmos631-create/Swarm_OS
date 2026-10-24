/** Query definitions shared between components and the start-up prefetch in main.tsx. */

import { api } from "./client";
import type { City, Dashboard } from "./types";

export const citiesQuery = {
  queryKey: ["cities"],
  queryFn: () => api<{ items: City[]; total: number }>("/cities"),
  staleTime: 10 * 60_000,
};

export const dashboardQuery = (cityId: string) => ({
  queryKey: ["dashboard", cityId],
  queryFn: () => api<Dashboard>(`/cities/${cityId}/dashboard`),
});

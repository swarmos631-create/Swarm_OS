import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api, ApiError } from "../api/client";
import type { Me, Role } from "../api/types";

interface SessionValue {
  me: Me | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<Me>;
  logout: () => Promise<void>;
  can: (role: Role) => boolean;
}

const SessionCtx = createContext<SessionValue | null>(null);
const RANK: Record<Role, number> = { viewer: 1, operator: 2, admin: 3 };

export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const meQ = useQuery({
    queryKey: ["me"],
    queryFn: () => api<Me>("/auth/me"),
    retry: (n, e) => !(e instanceof ApiError && e.status === 401) && n < 2,
    staleTime: 5 * 60_000,
  });
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    if (meQ.data) setMe(meQ.data);
    if (meQ.error instanceof ApiError && meQ.error.status === 401) setMe(null);
  }, [meQ.data, meQ.error]);

  // Any 401 elsewhere means the session expired: drop to the login screen.
  useEffect(() => {
    return qc.getQueryCache().subscribe((ev) => {
      const err = ev.query.state.error;
      if (err instanceof ApiError && err.status === 401 && ev.query.queryKey[0] !== "me") {
        setMe(null);
        qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "me" });
      }
    });
  }, [qc]);

  const login = useCallback(async (email: string, password: string) => {
    const m = await api<Me>("/auth/login", { method: "POST", body: { email, password } });
    qc.setQueryData(["me"], m);
    setMe(m);
    return m;
  }, [qc]);

  const logout = useCallback(async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      setMe(null);
      qc.clear();
    }
  }, [qc]);

  const value = useMemo<SessionValue>(() => ({
    me,
    loading: meQ.isLoading,
    login,
    logout,
    can: (role) => !!me && RANK[me.role] >= RANK[role],
  }), [me, meQ.isLoading, login, logout]);

  return <SessionCtx.Provider value={value}>{children}</SessionCtx.Provider>;
}

export function useSession(): SessionValue {
  const v = useContext(SessionCtx);
  if (!v) throw new Error("useSession outside SessionProvider");
  return v;
}

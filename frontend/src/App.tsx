import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/layout/AppShell";
import { EmptyState, ErrorState, LoadingBlock } from "./components/ui";
import { CommandCenter } from "./pages/CommandCenter";
import { LoginPage } from "./pages/Login";
import { CityProvider, rememberedCity, useCities } from "./state/city";
import { useSession } from "./state/session";

// Heavy pages (MapLibre, model lab) are code-split and loaded on demand.
const MapPage = lazy(() => import("./pages/MapPage"));
const IncidentsPage = lazy(() => import("./pages/Incidents"));
const WorkspacePage = lazy(() => import("./pages/Workspace"));
const AgentsPage = lazy(() => import("./pages/Agents"));
const AnalyticsPage = lazy(() => import("./pages/Analytics"));
const IntegrationsPage = lazy(() => import("./pages/Integrations"));
const ModelLabPage = lazy(() => import("./pages/ModelLab"));
const SystemPage = lazy(() => import("./pages/System"));
const SettingsPage = lazy(() => import("./pages/Settings"));

function DefaultCity() {
  const cities = useCities();
  if (cities.isLoading) return <div className="content"><LoadingBlock /></div>;
  const items = cities.data?.items ?? [];
  const remembered = rememberedCity();
  const target = items.find((c) => c.id === remembered) ?? items[0];
  if (!target) return <div className="content"><EmptyState title="No cities available">Your account is not authorised for any city. Ask an administrator to grant access.</EmptyState></div>;
  return <Navigate to={`/c/${target.id}`} replace />;
}

function CityGate() {
  const cities = useCities();
  return (
    <CityProvider>
      {cities.isSuccess ? <AppShell /> : cities.error
        ? <div className="content"><ErrorState error={cities.error} onRetry={() => void cities.refetch()} /></div>
        : <div className="content"><LoadingBlock rows={5} /></div>}
    </CityProvider>
  );
}

function ApiUnavailable({ error, onRetry }: { error: Error; onRetry: () => void }) {
  return (
    <div className="content stack">
      <h1>SwarmOS cannot reach its API</h1>
      <ErrorState error={error} onRetry={onRetry} />
      <p className="small muted">Operators: check <a href="/api/health/ready">/api/health/ready</a> and the server logs.</p>
    </div>
  );
}

export function App() {
  const { me, loading, error, retry } = useSession();
  if (loading) return <div className="content"><LoadingBlock rows={4} /></div>;
  if (error) return <ApiUnavailable error={error} onRetry={retry} />;
  if (!me) return <Routes><Route path="*" element={<LoginPage />} /></Routes>;
  const page = (el: React.ReactNode) => <Suspense fallback={<LoadingBlock rows={6} />}>{el}</Suspense>;
  return (
    <Routes>
      <Route path="/" element={<DefaultCity />} />
      <Route path="/c/:cityId" element={<CityGate />}>
        <Route index element={<CommandCenter />} />
        <Route path="map" element={page(<MapPage />)} />
        <Route path="incidents" element={page(<IncidentsPage />)} />
        <Route path="workspace" element={page(<WorkspacePage />)} />
        <Route path="agents" element={page(<AgentsPage />)} />
        <Route path="analytics" element={page(<AnalyticsPage />)} />
        <Route path="integrations" element={page(<IntegrationsPage />)} />
        <Route path="models" element={page(<ModelLabPage />)} />
        <Route path="system" element={page(<SystemPage />)} />
        <Route path="settings" element={page(<SettingsPage />)} />
        <Route path="*" element={<EmptyState title="Page not found">This page does not exist.</EmptyState>} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

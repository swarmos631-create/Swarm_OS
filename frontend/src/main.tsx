import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import "./styles/global.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { ApiError } from "./api/client";
import { citiesQuery, dashboardQuery } from "./api/queries";
import { App } from "./App";
import { initServiceWorker } from "./components/layout/PwaControls";
import { rememberedCity } from "./state/city";
import { SessionProvider } from "./state/session";
import { ThemeProvider } from "./state/theme";
import { ToastProvider } from "./state/ui";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 20_000,
      refetchOnWindowFocus: true,
      retry: (count, err) => !(err instanceof ApiError && [401, 403, 404, 422].includes(err.status)) && count < 2,
    },
    mutations: { networkMode: "online" },
  },
});

// Start the first screen's requests together instead of one after another (session → cities → dashboard): each
// sequential round trip to a distant API region adds visible delay. Signed out, these simply fail with 401.
void queryClient.prefetchQuery(citiesQuery);
const bootCity = window.location.pathname.match(/^\/c\/([^/]+)/)?.[1] ?? rememberedCity();
if (bootCity) void queryClient.prefetchQuery(dashboardQuery(decodeURIComponent(bootCity)));

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ToastProvider>
          <SessionProvider>
            <App />
          </SessionProvider>
        </ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
);

void initServiceWorker();

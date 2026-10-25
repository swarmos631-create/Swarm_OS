import { expect, test, type Page } from "@playwright/test";

const PASSWORD = process.env.E2E_PASSWORD ?? "E2eTestPassw0rd";

async function login(page: Page, email = "admin@swarmos.local", path = "/") {
  await page.goto(path);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("complementary", { name: "Primary navigation" })).toBeAttached();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}

test("rejects bad credentials with an actionable error", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Email").fill("admin@swarmos.local");
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Invalid email or password");
});

test("command center, city switching and honest unavailable states", async ({ page }) => {
  await login(page);
  await expect(page).toHaveURL(/\/c\/bengaluru$/);
  await expect(page.getByTestId("kpi-power_demand")).toContainText("Simulated");
  await expect(page.getByText(/Simulated data in use\./)).toBeVisible();

  await page.getByTestId("city-selector").click();
  await page.getByRole("combobox").fill("mumb");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/c\/mumbai$/);
  await expect(page.getByRole("heading", { name: "Command Center — Mumbai" })).toBeVisible();

  await page.getByTestId("city-selector").click();
  await page.getByRole("combobox").fill("jaipur");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Command Center — Jaipur" })).toBeVisible();
  await expect(page.getByTestId("kpi-power_demand")).toContainText("Simulated");
  await expect(page.getByTestId("kpi-temperature")).toContainText("Simulated");
  await expect(page.getByText(/Simulated data in use\./)).toBeVisible();
});

test("city context persists across navigation and deep links", async ({ page }) => {
  await login(page, "admin@swarmos.local", "/c/mumbai/incidents");
  await expect(page).toHaveURL(/\/c\/mumbai\/incidents$/);
  await page.getByRole("link", { name: "Agent Fleet" }).click();
  await expect(page).toHaveURL(/\/c\/mumbai\/agents$/);
});

test("operations map renders layers and shows feature details", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  await login(page, "admin@swarmos.local", "/c/bengaluru/map");
  await expect(page.getByTestId("ops-map")).toBeVisible();
  await expect(page.getByText("Layers", { exact: true })).toBeVisible();
  await expect(page.getByText("Traffic corridors")).toBeVisible();
  await page.getByLabel("Search features").fill("Corridor D");
  await page.getByText("Corridor D (CBD approach)").first().click();
  const detail = page.getByRole("dialog", { name: "Feature details" });
  await expect(detail).toContainText("Corridor D");
  await expect(detail).toContainText("Simulated");
  await page.getByTestId("map-3d").click();
  await expect(page.getByText(/illustrative, not a survey-grade digital twin/)).toBeVisible();
  // The keyless OpenFreeMap/OSM base map remains available without VITE_MAPPLS_KEY.
  if (!process.env.VITE_MAPPLS_KEY) await expect(page.getByRole("link", { name: "OpenFreeMap" })).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("50-agent registry is inspectable", async ({ page }) => {
  await login(page, "admin@swarmos.local", "/c/bengaluru/agents");
  await expect(page.getByTestId("agent-grid").getByRole("button")).toHaveCount(50);
  await page.getByRole("button", { name: "Flood Risk Analyst details" }).click();
  await expect(page.getByRole("dialog")).toContainText("flood_risk_analyst");
  await expect(page.getByRole("dialog")).toContainText("Input schema");
});

test("submit an AI task, observe execution and review proposed actions", async ({ page }) => {
  await login(page, "operator@swarmos.local", "/c/bengaluru/workspace");
  await page.getByTestId("task-prompt").fill("Check water quality and leaks");
  await page.getByTestId("run-task").click();
  const taskDetails = page.getByRole("button", { name: "View agents, evidence and proposed actions" });
  await expect(taskDetails).toBeVisible({ timeout: 60_000 });
  await taskDetails.click();
  await expect(page).toHaveURL(/task=/);
  await expect(page.getByTestId("task-summary")).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText(/agents finished/)).toContainText(/(\d+) \/ \1 agents finished/);
  await expect(page.getByText(/awaiting approval/)).toBeVisible();
  await expect(page.getByText("Based on synthetic demo data").first()).toBeVisible();

  // Approve a create-incident recommendation through the confirmation flow.
  const approve = page.getByRole("button", { name: "Approve" }).first();
  await approve.click();
  await page.getByRole("dialog").getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText(/Action (executed|awaiting manual)/)).toBeVisible();
});

test("operator can record an approved manual action and its execution time", async ({ page }) => {
  const taskId = "manual-learning-task";
  const actionId = "manual-learning-action";
  let manuallyExecuted = false;
  const action = () => ({
    id: actionId, task_id: taskId, city_id: "bengaluru", agent_id: "mobility_optimization_agent",
    agent_name: "Mobility Optimization Agent", kind: "manual", title: "Inspect the signal cabinet",
    rationale: "A field inspection requires an operator.", payload: {}, risk: "low", required_role: "operator",
    status: manuallyExecuted ? "executed" : "awaiting_manual", created_at: "2026-10-25T10:00:00Z",
    decided_by: "operator", decided_at: "2026-10-25T10:05:00Z", decision_note: "Approved for manual work.",
    executed_at: manuallyExecuted ? "2026-10-25T11:00:00Z" : null,
    execution_result: manuallyExecuted ? { method: "operator_reported_manual", note: "Field team confirmed the cabinet inspection." } : { note: "Approved for execution by the responsible department." },
    outcome: null,
  });
  await page.route(/\/api\/v1\/cities\/bengaluru\/tasks(?:\/[^?]*)?(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === `/api/v1/cities/bengaluru/tasks/${taskId}`) {
      await route.fulfill({ json: {
        id: taskId, city_id: "bengaluru", prompt: "Review approved field operation", status: "succeeded",
        created_at: "2026-10-25T10:00:00Z", started_at: "2026-10-25T10:00:00Z", finished_at: "2026-10-25T10:01:00Z",
        cancel_requested: false, agent_count: 0, error: null, summary: "Manual field inspection recommended.",
        plan: { agents: [], edges: [], stages: [] }, params: {}, result: null, executions: [], handoffs: [], actions: [action()],
      } });
    } else if (path === "/api/v1/cities/bengaluru/tasks") {
      await route.fulfill({ json: {
        items: [{ id: taskId, city_id: "bengaluru", prompt: "Review approved field operation", status: "succeeded",
          created_at: "2026-10-25T10:00:00Z", started_at: "2026-10-25T10:00:00Z", finished_at: "2026-10-25T10:01:00Z",
          cancel_requested: false, agent_count: 0, error: null, summary: "Manual field inspection recommended." }],
        total: 1, limit: 50, offset: 0,
      } });
    } else {
      await route.continue();
    }
  });
  await page.route(/\/api\/v1\/cities\/bengaluru\/actions\/manual-learning-action\/manual-execution$/, async (route) => {
    expect(route.request().method()).toBe("POST");
    const body = route.request().postDataJSON() as { note: string };
    expect(body.note).toContain("Field team confirmed");
    manuallyExecuted = true;
    await route.fulfill({ json: action() });
  });
  await login(page, "operator@swarmos.local", "/c/bengaluru/mission-control");
  const taskRow = page.getByRole("row").filter({ hasText: taskId });
  await taskRow.getByRole("button", { name: "Inspect" }).click();
  await expect(page.getByText("Record manual execution")).toBeVisible();
  await expect(page.getByText(/SwarmOS does not contact or operate field systems/)).toBeVisible();
  await page.getByLabel("Execution note").fill("Field team confirmed the cabinet inspection.");
  await page.getByRole("button", { name: "Mark manually executed" }).click();
  await expect(page.getByText(/Operator-reported manual execution at/)).toBeVisible();
  await expect(page.getByText("Observed outcome")).toBeVisible();
});

test("integrations show honest connection states", async ({ page }) => {
  await login(page, "admin@swarmos.local", "/c/bengaluru/integrations");
  await expect(page.getByRole("row", { name: /CPCB real-time air quality/ })).toContainText("Awaiting Credentials");
  await expect(page.getByRole("row", { name: /India Meteorological Department/ })).toContainText("Awaiting Access");
  await expect(page.getByRole("row", { name: /SwarmOS simulation/ })).toContainText("fixture");
});

test("model lab shows the trained model and local runtime", async ({ page }) => {
  await login(page, "admin@swarmos.local", "/c/bengaluru/models");
  await expect(page.getByText("External LLM providers: none")).toBeVisible();
  await expect(page.getByRole("heading", { name: "complaint_classifier" })).toBeVisible();
  await expect(page.getByText(/Promoted v\d{8}T/).first()).toBeVisible();
  await page.getByRole("button", { name: "Report" }).first().click();
  await expect(page.getByRole("dialog")).toContainText("Synthetic data: True");
});

test("viewer role cannot reach privileged controls", async ({ page }) => {
  await login(page, "viewer@swarmos.local", "/c/bengaluru/incidents");
  await expect(page.getByTestId("new-incident")).toHaveCount(0);
  await page.goto("/c/bengaluru/system");
  await expect(page.getByText("Operator access required")).toBeVisible();
  await page.goto("/c/jaipur");
  await expect(page.getByRole("alert")).toContainText(/not authorised/i);
});

test("responsive mobile layout uses a drawer navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  const nav = page.getByRole("complementary", { name: "Primary navigation" });
  await expect(page.getByRole("button", { name: "Open navigation" })).toBeVisible();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await nav.getByRole("link", { name: "Incidents" }).click();
  await expect(page).toHaveURL(/\/incidents$/);
  await expect(nav).not.toHaveClass(/open/);
  await expect(nav).toHaveCSS("transform", /matrix\(1, 0, 0, 1, -283/);
  const { overflow, elements } = await page.evaluate(() => {
    const elements = Array.from(document.querySelectorAll("body *")).map((element) => {
      const rect = element.getBoundingClientRect();
      return { tag: element.tagName, className: typeof element.className === "string" ? element.className : "",
        left: Math.round(rect.left), right: Math.round(rect.right), width: Math.round(rect.width) };
    }).filter((element) => element.right > window.innerWidth + 1).slice(0, 10);
    return { overflow: document.documentElement.scrollWidth - window.innerWidth, elements };
  });
  expect(overflow, JSON.stringify(elements)).toBeLessThanOrEqual(1);
});

test("offline mode warns about stale data and disables actions", async ({ page, context }) => {
  await login(page, "operator@swarmos.local", "/c/bengaluru/incidents");
  await expect(page.getByTestId("new-incident")).toBeEnabled();
  await context.setOffline(true);
  await expect(page.getByText(/You are offline/)).toBeVisible();
  await expect(page.getByTestId("new-incident")).toBeDisabled();
  await context.setOffline(false);
  await expect(page.getByTestId("new-incident")).toBeEnabled();
});

test("PWA manifest, icons and service worker", async ({ page, request }) => {
  const res = await request.get("/manifest.webmanifest");
  expect(res.ok()).toBeTruthy();
  const manifest = await res.json();
  expect(manifest.name).toContain("SwarmOS");
  expect(manifest.display).toBe("standalone");
  const sizes = manifest.icons.map((i: { sizes: string }) => i.sizes);
  expect(sizes).toEqual(expect.arrayContaining(["192x192", "512x512"]));
  expect(manifest.icons.some((i: { purpose?: string }) => i.purpose === "maskable")).toBeTruthy();
  for (const icon of manifest.icons) expect((await request.get(icon.src)).headers()["content-type"]).toContain("image/png");
  await login(page);
  const sw = await page.evaluate(async () => {
    const reg = await Promise.race([navigator.serviceWorker.ready, new Promise((r) => setTimeout(() => r(null), 10000))]);
    return reg ? (reg as ServiceWorkerRegistration).active?.scriptURL ?? "registered" : null;
  });
  expect(sw).toContain("sw.js");
  const apiCache = await request.get("/api/v1/cities");
  expect(apiCache.headers()["cache-control"]).toBe("no-store");
});

test("SakhiRoute compares real-route response context and opens mapped evidence", async ({ page }) => {
  await page.route("**/api/v1/public/cities/bengaluru/routes/compare", async (route) => {
    const context = {
      open_incident_points_within_250m: 1,
      incident_points_by_provenance_within_250m: { live: 1 },
      mapped_help_facilities_within_500m: { clinic: 1 },
      mapped_transit_stations_within_500m: { bus_station: 1 },
      facility_features_checked: 1, transit_features_checked: 1,
      street_light_features_checked: 1, mapped_street_lamp_points_within_100m: 2,
      mapped_wheelchair_accessibility_within_500m: { yes: 1, limited: 0, no: 0, designated: 0, unknown: 1 },
      mapped_transit_wheelchair_tags_within_500m: { yes: 1, limited: 0, no: 0, designated: 0, unknown: 0 },
      wheelchair_features_near_route: 2,
      mapped_feature_evidence: [
        { source_id: "osm-overpass", external_id: "node/123456", name: "Central Clinic",
          layer: "clinic", provenance: "live", distance_to_sampled_route_m: 86, wheelchair: "yes",
          observed_at: null, fetched_at: "2026-10-25T10:00:00Z" },
        { source_id: "osm-overpass", external_id: "node/123457", name: "Mapped street lamp",
          layer: "street_light", provenance: "live", distance_to_sampled_route_m: 95, wheelchair: null,
          lighting_tag: "yes", lamp_type: "LED", observed_at: null, fetched_at: "2026-10-25T10:00:00Z" },
      ],
      mapped_feature_evidence_truncated: true, context_truncated: true,
      provenance: ["live"],
    };
    await route.fulfill({ json: {
      city_id: "bengaluru", mode: "foot", source: "FOSSGIS OSRM (mocked test response)",
      attribution: "© OpenStreetMap contributors", source_url: "https://routing.openstreetmap.de/about.html",
      routes: [
        { id: "route-1", distance_m: 1450, duration_s: 900,
          geometry: { type: "LineString", coordinates: [[77.59, 12.97], [77.595, 12.975], [77.60, 12.98]] }, context },
        { id: "route-2", distance_m: 1000, duration_s: 600,
          geometry: { type: "LineString", coordinates: [[77.59, 12.97], [77.594, 12.974], [77.60, 12.98]] },
          context: { ...context, mapped_transit_wheelchair_tags_within_500m: { yes: 2, limited: 0, no: 0, designated: 0, unknown: 0 } } },
      ],
      limitations: ["Mock response for UI test.", "Mapped street-lamp points are community-mapped OSM infrastructure."],
    } });
  });
  await login(page, "viewer@swarmos.local", "/c/bengaluru/sakhi-route");
  const start = page.getByRole("group", { name: "Start" });
  const destination = page.getByRole("group", { name: "Destination" });
  await start.getByLabel("Latitude").fill("12.97");
  await start.getByLabel("Longitude").fill("77.59");
  await destination.getByLabel("Latitude").fill("12.98");
  await destination.getByLabel("Longitude").fill("77.60");
  await page.getByRole("button", { name: "Compare route options" }).click();
  await expect(page.getByText("Open incident points within 250 m:").first()).toBeVisible();
  const routeCard = page.locator("section.panel.panel-body").filter({ hasText: "Router's first choice" });
  await expect(routeCard).toContainText("Nearby mapped wheelchair tags:");
  await expect(routeCard).toContainText("Record origin: live 1");
  await expect(routeCard).toContainText("Origin is not verification");
  await expect(routeCard).toContainText("yes 1");
  await expect(routeCard).toContainText("Community-mapped street-lamp points within 100 m: 2");
  await routeCard.getByText("Inspect mapped facility evidence (2+)").click();
  await expect(routeCard.getByText("Central Clinic")).toBeVisible();
  await expect(routeCard).toContainText("Mapped lit tag: yes (unverified) · lamp type: LED");
  await expect(routeCard.getByRole("link", { name: "Inspect OpenStreetMap feature ↗" }).first()).toHaveAttribute("href", "https://www.openstreetmap.org/node/123456");
  await expect(routeCard).toContainText("Counts and evidence are incomplete lower bounds.");
  await expect(page.getByText(/Mapped street-lamp points are community-mapped/)).toBeVisible();
  await page.getByLabel("Compare preference").selectOption("fastest");
  const preferenceCard = page.locator("section.panel.panel-body").filter({ hasText: "Top by preference" });
  await expect(preferenceCard).toContainText("10 min");
  await expect(page.getByText("Sorted by the routing provider's estimated duration; not live traffic.")).toBeVisible();
  await page.getByLabel("Compare preference").selectOption("transit_wheelchair");
  await expect(preferenceCard).toContainText("Transit wheelchair tags within 500 m: yes 2");
  await page.getByLabel("Compare preference").selectOption("mapped_context");
  await expect(preferenceCard).toContainText("10 min");
  await expect(page.getByText(/Sorted by mapped help facilities and bus\/rail stations/)).toBeVisible();
});

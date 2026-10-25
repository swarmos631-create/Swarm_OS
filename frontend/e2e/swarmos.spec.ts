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
  await expect(page.getByTestId("kpi-power_demand")).toContainText("Demo");
  await expect(page.getByText("Demo data present.")).toBeVisible();

  await page.getByTestId("city-selector").click();
  await page.getByRole("combobox").fill("mumb");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/c\/mumbai$/);
  await expect(page.getByRole("heading", { name: "Command Center — Mumbai" })).toBeVisible();

  await page.getByTestId("city-selector").click();
  await page.getByRole("combobox").fill("jaipur");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Command Center — Jaipur" })).toBeVisible();
  await expect(page.getByTestId("kpi-power_demand")).toContainText("Not available");
  await expect(page.getByTestId("kpi-temperature")).toContainText("Not available");
  await expect(page.getByText("Demo data present.")).toHaveCount(0);
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
  await expect(detail).toContainText("Demo");
  await page.getByTestId("map-3d").click();
  await expect(page.getByText(/illustrative — not a survey-grade digital twin/)).toBeVisible();
  // MapmyIndia is the only base map; without VITE_MAPPLS_KEY the page says so and keeps the layers usable.
  if (!process.env.VITE_MAPPLS_KEY) await expect(page.getByText(/MapmyIndia key is not configured/)).toBeVisible();
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
  await page.getByTestId("preview-plan").click();
  await expect(page.getByText(/Proposed execution plan/)).toBeVisible();
  await expect(page.getByRole("cell", { name: "Water Quality Monitor" })).toBeVisible();
  await page.getByTestId("run-task").click();
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

test("integrations show honest connection states", async ({ page }) => {
  await login(page, "admin@swarmos.local", "/c/bengaluru/integrations");
  await expect(page.getByRole("row", { name: /CPCB real-time air quality/ })).toContainText("Awaiting Credentials");
  await expect(page.getByRole("row", { name: /India Meteorological Department/ })).toContainText("Awaiting Access");
  await expect(page.getByRole("row", { name: /demonstration fixtures/ })).toContainText("Demo");
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
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
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

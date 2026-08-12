import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const responsiveWidths = [
  360, 375, 390, 639, 640, 767, 768, 979, 980, 1280,
] as const;

type StatusMode = "healthy" | "degraded" | "unknown" | "loading" | "error";

function currentHealth(mode: Exclude<StatusMode, "loading" | "error">) {
  if (mode === "unknown") {
    return {
      state: "unavailable",
      tone: "neutral",
      label: "Live status unavailable",
      detail: "The gateway could not observe service state.",
      observation_available: false,
      services: [
        {
          service_id: "gateway",
          name: "Gateway API",
          configured: true,
          enabled: true,
          role: "required",
          routable: null,
          observation: "unavailable",
          tone: "neutral",
          last_error: null,
        },
      ],
    };
  }

  const degraded = mode === "degraded";
  return {
    state: degraded ? "warn" : "ok",
    tone: degraded ? "warn" : "ok",
    label: degraded ? "One service needs attention" : "All services available",
    detail: degraded
      ? "Gateway API is degraded."
      : "Live observations are current.",
    observation_available: true,
    services: [
      {
        service_id: "gateway",
        name: "Gateway API",
        configured: true,
        enabled: true,
        role: "required",
        routable: true,
        observation: degraded ? "degraded" : "up",
        tone: degraded ? "warn" : "ok",
        last_error: degraded ? "Probe timed out" : null,
      },
    ],
  };
}

async function stubAdminApi(
  page: import("@playwright/test").Page,
  mode: StatusMode,
) {
  await page.unroute("**/admin/api/**");
  await page.route("**/admin/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });

    if (path === "/admin/api/status") {
      if (mode === "loading") return new Promise(() => undefined);
      if (mode === "error")
        return json({ error: { code: "offline", message: "offline" } }, 503);
      return json({
        status: {
          current_health: currentHealth(mode),
        },
      });
    }
    if (path === "/admin/api/session") {
      return json({
        actor: {
          subject: "admin",
          email: "operator@example.test",
          roles: ["admin"],
          csrf_token: "",
        },
      });
    }
    if (path === "/admin/api/apps") return json({ apps: [] });
    if (path === "/admin/api/users") return json({ users: [] });
    if (path === "/admin/api/credentials") return json({ credentials: [] });
    if (path === "/admin/api/profiles") return json({ profiles: [] });
    if (path === "/admin/api/overview") {
      return json({
        actor: { subject: "admin", email: "operator@example.test", roles: [] },
        counts: {},
        attention: [],
        current_health: currentHealth("healthy"),
        runtime_profile_mappings: {},
      });
    }
    if (path === "/admin/api/audit")
      return json({ records: [], next_cursor: null });
    if (path === "/admin/api/operator/service-registry") {
      return json({ service_registry: { schema_version: 1, services: {} } });
    }
    return json({});
  });
}

async function openStatus(
  page: import("@playwright/test").Page,
  mode: StatusMode,
) {
  await stubAdminApi(page, mode);
  const terminalError =
    mode === "error" ? waitForTerminalStatusError(page) : undefined;
  await page.goto(`/ui/admin/status/?scenario=${mode}-${Date.now()}`, {
    waitUntil: "commit",
  });
  await terminalError;
}

/**
 * The shared query client retries a 5xx status request twice. During those
 * retries React Query remains pending, so wait for the final intercepted
 * failure before asserting the terminal error projection.
 */
function waitForTerminalStatusError(page: import("@playwright/test").Page) {
  let failures = 0;
  return new Promise<void>((resolve) => {
    const onResponse = (response: import("@playwright/test").Response) => {
      if (
        new URL(response.url()).pathname !== "/admin/api/status" ||
        response.status() !== 503
      ) {
        return;
      }

      failures += 1;
      if (failures === 3) {
        page.off("response", onResponse);
        resolve();
      }
    };
    page.on("response", onResponse);
  });
}

test("shell preserves all destinations, focus origins, target size, and containment at every breakpoint", async ({
  page,
}) => {
  await stubAdminApi(page, "healthy");

  for (const width of responsiveWidths) {
    await page.setViewportSize({ width, height: 812 });
    await page.goto("/ui/admin/status/");
    await expect(
      page.getByRole("heading", { name: "System Status" }),
    ).toBeVisible();

    const overflow = await page.evaluate(() => ({
      document:
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
      body: document.body.scrollWidth <= document.body.clientWidth,
      shell: (() => {
        const element =
          document.querySelector<HTMLElement>(".vf-console-shell");
        return element !== null && element.scrollWidth <= element.clientWidth;
      })(),
    }));
    expect(overflow).toEqual({ document: true, body: true, shell: true });

    if (width < 980) {
      const nav = page.getByRole("navigation", { name: "Primary navigation" });
      await expect(nav).toContainText("OverviewStatusServicesAppsMore");
      await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);
      for (const target of [
        page.getByRole("button", { name: "Open navigation" }),
        page.getByRole("button", { name: "More navigation" }),
        page.getByRole("link", { name: "Status", exact: true }),
      ]) {
        const box = await target.boundingBox();
        expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
        expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
      }
    } else {
      await expect(
        page.getByRole("navigation", { name: "Primary navigation" }),
      ).toBeHidden();
      await expect(
        page.getByRole("link", { name: "Status", exact: true }),
      ).toHaveAttribute("aria-current", "page");
      for (const target of [
        page.getByRole("combobox", { name: "Search" }),
        page
          .getByRole("banner")
          .getByRole("link", { name: "Portal", exact: true }),
        page
          .getByRole("banner")
          .getByRole("link", { name: "Sign out", exact: true }),
        page
          .locator("main")
          .getByRole("button", { name: "Refresh", exact: true }),
      ]) {
        const box = await target.boundingBox();
        expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
        expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
      }
    }
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/admin/status/");
  for (const trigger of ["Open navigation", "More navigation"] as const) {
    const button = page.getByRole("button", { name: trigger });
    await button.focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("dialog", { name: "Navigation" }),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(button).toBeFocused();
  }
});

test("Status keeps healthy, degraded, unavailable, loading, and error projections distinct", async ({
  page,
}, testInfo) => {
  for (const [mode, label] of [
    ["healthy", "All services available"],
    ["degraded", "One service needs attention"],
    ["unknown", "Live status unavailable"],
    ["error", "Status unavailable"],
  ] as const) {
    await openStatus(page, mode);
    await expect(
      page.locator("main").getByText(label, { exact: true }),
    ).toBeVisible();
  }

  await openStatus(page, "loading");
  await expect(
    page.getByRole("heading", { name: "System Status", level: 1 }),
  ).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

  await openStatus(page, "error");
  await expect(
    page.locator("main").getByText("Status unavailable", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "System Status", level: 1 }),
  ).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

  await page.setViewportSize({ width: 1280, height: 800 });
  await openStatus(page, "degraded");
  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations).toEqual([]);
  const screenshot = testInfo.outputPath("shell-status-degraded-desktop.png");
  await page.screenshot({ path: screenshot, fullPage: true });
  await testInfo.attach("shell-status-degraded-desktop", {
    path: screenshot,
    contentType: "image/png",
  });
});

test("Status uses one desktop right rail and one compact Sheet detail", async ({
  page,
}) => {
  await stubAdminApi(page, "healthy");

  await page.setViewportSize({ width: 979, height: 844 });
  await page.goto("/ui/admin/status/?service=gateway");
  const compactSheet = page.getByRole("dialog", { name: "Gateway API" });
  await expect(compactSheet).toBeVisible();
  await expect(compactSheet.locator(".vf-context-panel")).toHaveCount(1);
  await expect(page.locator("main .vf-context-panel:visible")).toHaveCount(0);
  await expect(page.locator(".vf-context-panel:visible")).toHaveCount(1);

  await page.setViewportSize({ width: 980, height: 844 });
  await page.goto("/ui/admin/status/?service=gateway");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator("main .vf-context-panel:visible")).toHaveCount(1);
  await expect(page.locator(".vf-context-panel:visible")).toHaveCount(1);
});

test("shared query health state stays honest across shell, Overview, Status, and Services", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 844 });
  await stubAdminApi(page, "healthy");
  await page.goto("/ui/admin/status/");
  await expect(
    page.locator("main").getByText("All services available"),
  ).toBeVisible();

  await stubAdminApi(page, "error");
  await page.locator("main").getByRole("button", { name: "Refresh" }).click();
  await expect(
    page
      .locator("main")
      .getByText("Showing last known status", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("banner")
      .getByRole("link", { name: "Showing last known status" }),
  ).toBeVisible();

  await page
    .getByRole("link", { name: "Overview", exact: true })
    .first()
    .click();
  await expect(page).toHaveURL(/\/ui\/admin\/?$/);
  await expect(
    page
      .locator("main")
      .getByText("Showing last known status", { exact: true }),
  ).toBeVisible();

  await page
    .getByRole("link", { name: "Services", exact: true })
    .first()
    .click();
  await expect(page).toHaveURL(/\/ui\/admin\/services\/?$/);
  await expect(
    page
      .locator("main")
      .getByRole("heading", { name: "Showing last known status", exact: true })
      .first(),
  ).toBeVisible();
  await expect(
    page.getByText("Checking live status", { exact: true }),
  ).toHaveCount(0);
});

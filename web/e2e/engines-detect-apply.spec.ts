import { request, type APIRequestContext, type Page } from "@playwright/test";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { createServer } from "node:http";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdir } from "node:fs/promises";
import path from "node:path";

// Axe must inspect settled toast colours, not their entrance/exit opacity.
test.use({ contextOptions: { reducedMotion: "reduce" } });

let engine: ChildProcess;
let enginePort: number;
let registryApi: APIRequestContext;
const originalSubstrates: Record<string, unknown> = {};
let registryHeaders: Record<string, string>;

test.beforeAll(async ({ baseURL }) => {
  // Each CI lane owns ten ports: gateway, fake engine, then strict probes.
  const fixturePort = Number(new URL(baseURL!).port);
  if (!Number.isInteger(fixturePort) || fixturePort < 18000 || fixturePort > 18998)
    throw new Error("Fixture port must be between 18000 and 18998");
  for (let candidate = fixturePort + 2; candidate <= Math.min(fixturePort + 9, 18998); candidate++) {
    const reservation = createServer();
    const available = await new Promise<boolean>((resolve) => {
      reservation.once("error", () => resolve(false));
      reservation.listen(candidate, "127.0.0.1", () => resolve(true));
    });
    if (available) {
      await new Promise<void>((resolve) => reservation.close(() => resolve()));
      enginePort = candidate;
      break;
    }
  }
  if (!enginePort) throw new Error("No isolated strict-engine port available");
  const fixtureHome = path.resolve(
    process.env.UI_E2E_STATE_ROOT ?? "../.ui-e2e-state",
    `strict-home-${fixturePort}`,
  );
  await mkdir(fixtureHome, { recursive: true });
  engine = spawn(
    process.env.UI_E2E_PYTHON ?? path.resolve("../venv/bin/python"),
    [
      "-m",
      "uvicorn",
      "engines_test_substrate:app",
      "--app-dir",
      "e2e",
      "--host",
      "127.0.0.1",
      "--port",
      String(enginePort),
    ],
    {
      env: {
        PATH: process.env.PATH,
        NODE_ENV: "test",
        HOME: fixtureHome,
        LITELLM_LOCAL_MODEL_COST_MAP: "True",
        LITELLM_TELEMETRY: "False",
        PYTHONDONTWRITEBYTECODE: "1",
      },
      stdio: "ignore",
    },
  );
  await expect
    .poll(
      async () => {
        if (engine.exitCode !== null)
          throw new Error("Strict engine fixture exited before readiness");
        try {
          return (await fetch(`http://127.0.0.1:${enginePort}/health`)).status;
        } catch {
          return 0;
        }
      },
      { timeout: 30_000 },
    )
    .toBe(200);
  registryApi = await request.newContext({
    baseURL,
    storageState: `test-results/auth-${new URL(baseURL!).port}.json`,
  });
  const session = await (await registryApi.get("/admin/api/session")).json();
  registryHeaders = {
    "x-azizam-csrf": session.actor.csrf_token,
    "x-azizam-admin-action": "1",
  };
  const response = await registryApi.get(
    "/admin/api/operator/service-registry",
  );
  const registry = (await response.json()).service_registry;
  for (const [id, record] of Object.entries(registry.services) as [
    string,
    { kind: string; config: Record<string, unknown> },
  ][]) {
    if (id !== "litellm") continue;
    originalSubstrates[id] = structuredClone(record);
    record.config = {
      ...record.config,
      base_url_env: "",
      api_base_env: "",
      base_url: `http://127.0.0.1:${enginePort}/substrate`,
      host: "127.0.0.1",
      port: enginePort,
      base_path: "/substrate",
      health_endpoint: "/health",
    };
  }
  expect(Object.keys(originalSubstrates)).not.toHaveLength(0);
  const seeded = await registryApi.put("/admin/api/operator/service-registry", {
    headers: { ...registryHeaders, "if-match": response.headers().etag },
    data: registry,
  });
  expect(seeded.ok()).toBe(true);
});

test.afterAll(async () => {
  try {
    if (registryApi && Object.keys(originalSubstrates).length) {
      const response = await registryApi.get(
        "/admin/api/operator/service-registry",
      );
      const registry = (await response.json()).service_registry;
      Object.assign(registry.services, originalSubstrates);
      const restored = await registryApi.put(
        "/admin/api/operator/service-registry",
        {
          headers: { ...registryHeaders, "if-match": response.headers().etag },
          data: registry,
        },
      );
      expect(restored.ok()).toBe(true);
    }
  } finally {
    if (registryApi) await registryApi.dispose();
    if (engine && engine.exitCode === null) {
      const exited = new Promise<void>((resolve) =>
        engine.once("exit", () => resolve()),
      );
      engine.kill("SIGTERM");
      const timeout = setTimeout(() => engine.kill("SIGKILL"), 8000);
      await exited;
      clearTimeout(timeout);
    }
  }
});

async function checkVisuals(page: Page, name: string) {
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((row) =>
      ["serious", "critical"].includes(row.impact ?? ""),
    ),
  ).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: `test-results/screens/${name}.png`,
    fullPage: true,
  });
}

for (const theme of ["light", "dark"]) {
  for (const width of [1440, 390]) {
    test(`detected model applies and routes with explicit access ${theme} ${width}`, { tag: GLOBAL_STATE_TAG }, async ({
      page,
    }) => {
      const suffix = `${theme}-${width}`;
      const serviceId = `strict-${suffix}`;
      const publicName = `mapped-${suffix}`;
      const session = await (
        await page.request.get("/admin/api/session")
      ).json();
      const headers = { "x-azizam-csrf": session.actor.csrf_token };
      const registryResponse = await page.request.get(
        "/admin/api/operator/service-registry",
      );
      const added = await page.request.post("/admin/api/operator/services", {
        headers: { ...headers, "if-match": registryResponse.headers().etag },
        data: {
          id: serviceId,
          name: `Strict engine ${suffix}`,
          base_url: `http://127.0.0.1:${enginePort}/v1`,
          protocol: "openai-compatible",
          capabilities: ["chat"],
          health_endpoint: "/health",
          role: "optional",
          enabled: true,
          custom_operator_field: "preserve",
        },
      });
      expect(added.ok()).toBe(true);
      const { profile } = await (
        await page.request.post("/admin/api/profiles", {
          headers,
          data: {
            name: `Restricted ${suffix}`,
            profile: {
              protocols: ["openai"],
              backends: [serviceId],
              models: [publicName],
            },
          },
        })
      ).json();
      expect(profile.id).toBeTruthy();
      const { users } = await (
        await page.request.get("/admin/api/users")
      ).json();
      const { app } = await (
        await page.request.post("/admin/api/apps", {
          headers,
          data: {
            name: `Strict app ${suffix}`,
            kind: "personal",
            owner_user_id: users[0].id,
            profile_id: profile.id,
          },
        })
      ).json();
      const { credential } = await (
        await page.request.post(`/admin/api/apps/${app.id}/keys`, {
          headers,
          data: { label: "Strict routing test" },
        })
      ).json();
      const callerHeaders = { authorization: `Bearer ${credential.api_key}` };
      const before = await page.request.get("/openai/v1/models", {
        headers: callerHeaders,
      });
      expect(
        (await before.json()).data.map((row: { id: string }) => row.id),
      ).not.toContain(publicName);
      await page.setViewportSize({ width, height: width === 390 ? 844 : 960 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.goto(`/engines/?service=${serviceId}`);
      const card = page.locator(`tr[data-engine-id="${serviceId}"]`);
      if (width === 390) {
        const actionBox = await card
          .getByRole("button", { name: /Actions|More/ })
          .boundingBox();
        expect(actionBox!.height).toBeGreaterThanOrEqual(44);
        expect(actionBox!.width).toBeGreaterThanOrEqual(44);
      }
      await card.getByRole("button", { name: /Actions|More/ }).click();
      await page
        .getByRole("menuitem", { name: "Detect models", exact: true })
        .click();
      const sheet = page.getByRole("dialog", {
        name: "Edit engine",
        exact: true,
      });
      if (width === 390) {
        const toggleBox = await sheet
          .getByRole("switch", { name: new RegExp("Toggle Strict engine") })
          .boundingBox();
        expect(toggleBox!.height).toBeGreaterThanOrEqual(44);
        expect(toggleBox!.width).toBeGreaterThanOrEqual(44);
      }
      const originalEndpoint = await sheet
        .getByLabel("Endpoint", { exact: true })
        .inputValue();
      await sheet
        .getByLabel("Endpoint", { exact: true })
        .fill("http://127.0.0.1:18990/v1");
      await expect(
        sheet.getByRole("button", { name: "Test", exact: true }),
      ).toBeDisabled();
      await expect(
        sheet.getByRole("button", { name: "Detect", exact: true }),
      ).toBeDisabled();
      await expect(
        sheet.getByText(
          "Save configuration changes before testing or detecting models.",
        ),
      ).toBeVisible();
      await sheet
        .getByLabel("Endpoint", { exact: true })
        .fill(originalEndpoint);
      await sheet.getByLabel("Note").fill("Draft retained after model apply");
      await sheet.getByRole("button", { name: "Detect", exact: true }).click();
      await expect(
        sheet.getByRole("heading", { name: "Preview detected models" }),
      ).toBeVisible();
      await sheet.getByRole("group", { name: /^Model \d+$/ }).filter({ hasText: "Engine model name: strict-chat" }).getByLabel("Model name").fill(publicName);
      await expect(
        sheet.getByRole("button", { name: "Apply models" }),
      ).toBeDisabled();
      await expect(sheet.getByText(/^Detected models:/)).toHaveCount(0);
      await expect(sheet.getByText("Engine model name: strict-chat")).not.toBeVisible();
      await checkVisuals(page, `engines-preview-${suffix}`);
      if (width === 390) {
        const consent = sheet.getByLabel(
          "Use existing access rules for these models",
        );
        await consent.evaluate((element) =>
          element.scrollIntoView({ block: "center" }),
        );
        expect(
          await consent.evaluate((element) => {
            const rect = element.getBoundingClientRect();
            return (
              document.elementFromPoint(
                rect.x + rect.width / 2,
                rect.y + rect.height / 2,
              ) === element
            );
          }),
        ).toBe(true);
        await page.screenshot({
          path: `test-results/screens/engines-consent-viewport-${suffix}.png`,
        });
      }
      await sheet
        .getByLabel("Use existing access rules for these models")
        .check();
      await sheet.getByRole("button", { name: "Apply models" }).click();
      await expect(
        sheet.getByText(`${serviceId}`, { exact: true }),
      ).toHaveCount(0);
      await expect(sheet.getByText(/models applied/)).toBeVisible();
      await sheet.getByText("Advanced fields", { exact: true }).click();
      await expect(sheet.getByLabel("Advanced JSON")).toHaveValue(
        new RegExp(publicName),
      );
      await expect(sheet.getByLabel("Note")).toHaveValue(
        "Draft retained after model apply",
      );
      await sheet
        .getByRole("button", { name: "Save engine", exact: true })
        .click();
      await expect(sheet).not.toBeVisible();
      const registry = (
        await (
          await page.request.get("/admin/api/operator/service-registry")
        ).json()
      ).service_registry;
      expect(registry.services[serviceId].config.models).toEqual([
        {
          id: publicName,
          model_group: publicName,
          upstream_model: "strict-chat",
          transforms: [],
        },
      ]);
      expect(
        registry.services[serviceId].config.model_policy_schema_version,
      ).toBe(1);
      expect(registry.services[serviceId].config.custom_operator_field).toBe(
        "preserve",
      );
      expect(registry.services[serviceId].config.note).toBe(
        "Draft retained after model apply",
      );
      expect(registry.services[serviceId].enabled).toBe(true);
      const unchanged = (
        await (await page.request.get("/admin/api/profiles")).json()
      ).profiles.find((row: { id: string }) => row.id === profile.id);
      expect(unchanged.profile.models).toEqual([publicName]);
      const generated = await page.request.get(
        "/admin/api/operator/litellm-config",
      );
      expect(generated.ok()).toBe(true);
      const deployment = (
        await generated.json()
      ).litellm_config.model_list.filter(
        (row: { model_name: string }) => row.model_name === publicName,
      );
      expect(deployment).toHaveLength(1);
      expect(deployment[0].litellm_params.model).toBe("openai/strict-chat");
      const configured = await page.request.post(
        `http://127.0.0.1:${enginePort}/test/configure`,
        { data: { model_list: deployment } },
      );
      expect(configured.ok()).toBe(true);
      const models = await page.request.get("/openai/v1/models", {
        headers: callerHeaders,
      });
      expect(
        (await models.json()).data.map((row: { id: string }) => row.id),
      ).toContain(publicName);
      const unknown = await page.request.post(
        `http://127.0.0.1:${enginePort}/v1/chat/completions`,
        {
          data: {
            model: "unknown-model",
            messages: [{ role: "user", content: "Test" }],
          },
        },
      );
      expect(unknown.status()).toBe(404);
      const allowed = await page.request.post("/openai/v1/chat/completions", {
        headers: callerHeaders,
        data: {
          model: publicName,
          messages: [{ role: "user", content: "Test the public mapping" }],
        },
      });
      expect(allowed.status()).toBe(200);
      expect((await allowed.json()).choices[0].message.content).toBe(
        "Strict model mapping works.",
      );
      const observations = await (
        await page.request.get(
          `http://127.0.0.1:${enginePort}/test/observations`,
        )
      ).json();
      expect(observations.accepted_models.at(-1)).toBe("strict-chat");
      const denied = await page.request.post("/openai/v1/chat/completions", {
        headers: callerHeaders,
        data: {
          model: "demo-chat",
          messages: [{ role: "user", content: "Not granted" }],
        },
      });
      expect(denied.status()).toBe(403);
      await checkVisuals(page, `engines-applied-${suffix}`);
    });

    test(`failed engine check identifies target and recovers ${theme} ${width}`, { tag: GLOBAL_STATE_TAG }, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 960 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      let failing = true;
      await page.route("**/admin/api/operator/services/test", (route) =>
        route.fulfill({
          json: {
            probe: {
              ok: !failing,
              url: "http://127.0.0.1:18498/health",
              status: failing ? 503 : 200,
              error: failing ? "Engine unavailable" : "",
            },
          },
        }),
      );
      await page.goto("/engines/?service=capture-engine");
      const card = page.locator('tr[data-engine-id="capture-engine"]');
      await card.getByRole("button", { name: /Actions|More/ }).click();
      await page
        .getByRole("menuitem", { name: "Test engine", exact: true })
        .click();
      const sheet = page.getByRole("dialog", { name: "Edit engine" });
      await sheet.getByRole("button", { name: "Test", exact: true }).click();
      const alert = sheet.getByRole("alert");
      await expect(alert).toBeVisible();
      const friendlyName = (await sheet
        .locator("[data-slot='sheet-description']")
        .textContent())!;
      await expect(alert).toContainText(`${friendlyName}: connection failed`);
      await expect(sheet.getByRole("alert")).not.toContainText("HTTP");
      const technical = sheet.locator("details").filter({ hasText: "HTTP 503" });
      await expect(technical).not.toHaveAttribute("open");
      await technical.getByText("Technical details", { exact: true }).click();
      await expect(technical).toContainText("http://127.0.0.1:18498/health (HTTP 503)");
      await expect(sheet.getByRole("alert")).toContainText(
        "reachable from the gateway",
      );
      await checkVisuals(page, `engines-failed-${theme}-${width}`);
      await sheet.getByRole("button", { name: "Edit endpoint" }).click();
      await expect(sheet).toBeVisible();
      failing = false;
      await sheet.getByRole("button", { name: "Retry test" }).click();
      await expect(sheet.getByText(/connection passed/)).toBeVisible();
      await expect(sheet.getByRole("alert")).toHaveCount(0);
    });
  }
}

test("a registry conflict preserves the detected-model preview for retry", { tag: GLOBAL_STATE_TAG }, async ({
  page,
}) => {
  await page.route("**/admin/api/operator/services/capture-engine", (route) => {
    expect(route.request().method()).toBe("PATCH");
    expect(route.request().headers()["if-match"]).toBeTruthy();
    return route.fulfill({
      status: 412,
      json: {
        error: {
          code: "generation_mismatch",
          message: "The registry changed. Review your preview and try again.",
        },
      },
    });
  });
  await page.goto("/engines/?service=capture-engine");
  const card = page.locator('tr[data-engine-id="capture-engine"]');
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page
    .getByRole("menuitem", { name: "Detect models", exact: true })
    .click();
  const sheet = page.getByRole("dialog", { name: "Edit engine", exact: true });
  await sheet.getByRole("button", { name: "Detect", exact: true }).click();
  await sheet.getByRole("group", { name: /^Model \d+$/ }).filter({ hasText: "Engine model name: demo-chat" }).getByLabel("Model name").fill("draft-chat");
  await sheet.getByLabel("Use existing access rules for these models").check();
  await sheet.getByRole("button", { name: "Apply models" }).click();
  await expect(sheet.getByRole("alert")).toContainText("Engine settings changed elsewhere");
  await expect(sheet.getByRole("group", { name: /^Model \d+$/ }).filter({ hasText: "Engine model name: demo-chat" }).getByLabel("Model name")).toHaveValue(
    "draft-chat",
  );
  await expect(
    sheet.getByRole("button", { name: "Apply models" }),
  ).toBeEnabled();
  await expect(sheet.getByText(/models applied/)).toHaveCount(0);
});

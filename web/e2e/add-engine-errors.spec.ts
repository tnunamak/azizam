import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import type { Locator, Page } from "@playwright/test";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

// Add engine's error journeys and its key boundary (round 4, e2e review).
// Two small engines (e2e/engines_key_gate.py) run on this lane's strict ports
// (gateway port + 2 … + 9, the only extra ports the fixture gateway may call).
// Each logs the Authorization header of every request it gets, so the specs
// prove at the gateway's outbound boundary which key reached which address.
// A third strict port is left closed for the unreachable journey.

test.use({ contextOptions: { reducedMotion: "reduce" } });

const GOOD_KEY = "sk-journey-gate-key";
type Gate = { port: number; process: ChildProcess };
let gates: Gate[] = [];
let closedPort = 0;

async function freePorts(base: number, count: number): Promise<number[]> {
  const found: number[] = [];
  for (let candidate = base + 2; candidate <= base + 9 && found.length < count; candidate++) {
    const probe = createServer();
    const free = await new Promise<boolean>((resolve) => {
      probe.once("error", () => resolve(false));
      probe.listen(candidate, "127.0.0.1", () => resolve(true));
    });
    if (free) {
      await new Promise<void>((resolve) => probe.close(() => resolve()));
      found.push(candidate);
    }
  }
  if (found.length < count) throw new Error("Not enough free strict-engine ports on this lane");
  return found;
}

test.beforeAll(async ({ baseURL }) => {
  const gatewayPort = Number(new URL(baseURL!).port);
  const [first, second, third] = await freePorts(gatewayPort, 3);
  closedPort = third;
  const home = path.resolve(process.env.UI_E2E_STATE_ROOT ?? "../.ui-e2e-state", `key-gate-${gatewayPort}`);
  await mkdir(home, { recursive: true });
  gates = [first, second].map((port) => ({
    port,
    process: spawn(
      process.env.UI_E2E_PYTHON ?? path.resolve("../venv/bin/python"),
      ["-m", "uvicorn", "engines_key_gate:app", "--app-dir", "e2e", "--host", "127.0.0.1", "--port", String(port)],
      { env: { PATH: process.env.PATH, HOME: home, PYTHONDONTWRITEBYTECODE: "1" }, stdio: "ignore" },
    ),
  }));
  for (const gate of gates) {
    await expect
      .poll(
        async () => {
          if (gate.process.exitCode !== null) throw new Error("A key-gate engine exited before readiness");
          try {
            return (await fetch(`http://127.0.0.1:${gate.port}/health`)).status;
          } catch {
            return 0;
          }
        },
        { timeout: 30_000 },
      )
      .toBe(200);
  }
});

test.afterAll(async () => {
  for (const gate of gates) gate.process.kill("SIGTERM");
});

const keyed = (gate: Gate) => `http://127.0.0.1:${gate.port}/keyed/v1`;
const empty = (gate: Gate) => `http://127.0.0.1:${gate.port}/empty/v1`;

async function seen(gate: Gate): Promise<{ path: string; authorization: string }[]> {
  return (await (await fetch(`http://127.0.0.1:${gate.port}/fixture/seen`)).json()).requests;
}

async function forget(gate: Gate): Promise<void> {
  await fetch(`http://127.0.0.1:${gate.port}/fixture/forget`, { method: "POST" });
}

async function serviceIds(page: Page): Promise<string[]> {
  const response = await page.request.get("/admin/api/operator/service-registry");
  return Object.keys((await response.json()).service_registry.services).sort();
}

async function writeHeaders(page: Page): Promise<Record<string, string>> {
  const session = await (await page.request.get("/admin/api/session")).json();
  const registry = await page.request.get("/admin/api/operator/service-registry");
  return { "x-azizam-csrf": session.actor.csrf_token, "if-match": registry.headers().etag };
}

async function removeEngine(page: Page, serviceId: string): Promise<void> {
  const removed = await page.request.delete(`/admin/api/operator/services/${serviceId}`, { headers: await writeHeaders(page) });
  expect(removed.ok()).toBe(true);
}

async function openAddressStep(page: Page): Promise<Locator> {
  await page.goto("/engines/");
  await page.getByRole("button", { name: "Add engine", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Add engine" });
  await dialog.getByRole("button", { name: /Any OpenAI-compatible server/ }).click();
  await expect(dialog.getByText(/Step 2 of 5/)).toBeVisible();
  return dialog;
}

async function fitsTheScreen(page: Page, dialog: Locator): Promise<void> {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect.poll(() => dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  const box = await dialog.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
}

for (const width of [1280, 390]) {
  test.describe(`at ${width} px`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    });

    test(`an unreachable server is reported, creates nothing, and the corrected address goes on`, { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
      const before = await serviceIds(page);
      const dialog = await openAddressStep(page);
      await dialog.getByLabel(/^Address/).fill(`http://127.0.0.1:${closedPort}/v1`);
      await dialog.getByRole("button", { name: "Continue" }).click();
      await expect(dialog.getByText("Server not reachable").first()).toBeVisible();
      await expect(dialog.getByRole("heading", { name: "What this server can do" })).toHaveCount(0);
      await expect(dialog.getByText(/Key accepted/)).toHaveCount(0);
      await fitsTheScreen(page, dialog);
      expect(await serviceIds(page)).toEqual(before);

      await dialog.getByLabel(/^Address/).fill(keyed(gates[0]));
      await dialog.getByLabel(/^API key/).fill(GOOD_KEY);
      await dialog.getByRole("button", { name: /^(Continue|Check again)$/ }).click();
      await expect(dialog.getByRole("heading", { name: "What this server can do" })).toBeVisible();
      await expect(dialog.getByText(/Reached the server\. Key accepted\./)).toBeVisible();
      await expect(dialog.getByText("Server not reachable")).toHaveCount(0);
      await page.keyboard.press("Escape");
      expect(await serviceIds(page)).toEqual(before);
    });

    test(`a refused key is reported, creates nothing, and the right key is accepted`, { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
      const before = await serviceIds(page);
      const dialog = await openAddressStep(page);
      await dialog.getByLabel(/^Address/).fill(keyed(gates[0]));
      await dialog.getByLabel(/^API key/).fill("sk-journey-wrong-key");
      await dialog.getByRole("button", { name: "Continue" }).click();
      await expect(dialog.getByText("Key refused").first()).toBeVisible();
      await expect(dialog.getByRole("heading", { name: "What this server can do" })).toHaveCount(0);
      await fitsTheScreen(page, dialog);
      expect(await serviceIds(page)).toEqual(before);

      await dialog.getByLabel(/^API key/).fill(GOOD_KEY);
      await dialog.getByRole("button", { name: /^(Continue|Check again)$/ }).click();
      await expect(dialog.getByText(/Reached the server\. Key accepted\./)).toBeVisible();
      await expect(dialog.getByText("Key refused")).toHaveCount(0);
      await page.keyboard.press("Escape");
      expect(await serviceIds(page)).toEqual(before);
    });

    test(`nothing recognised is its own state, with no models left from an earlier check`, { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
      const dialog = await openAddressStep(page);
      // A first check that succeeds and finds a model.
      await dialog.getByLabel(/^Address/).fill(keyed(gates[0]));
      await dialog.getByLabel(/^API key/).fill(GOOD_KEY);
      await dialog.getByRole("button", { name: "Continue" }).click();
      await expect(dialog.getByRole("heading", { name: "What this server can do" })).toBeVisible();
      await dialog.getByRole("button", { name: "Continue" }).click();
      await expect(dialog.getByText("gate-chat").first()).toBeVisible();
      // Back to the address, then a server that offers nothing.
      await dialog.getByRole("button", { name: "Back" }).click();
      await dialog.getByRole("button", { name: "Back" }).click();
      await expect(dialog.getByText(/Step 2 of 5/)).toBeVisible();
      await dialog.getByLabel(/^Address/).fill(empty(gates[0]));
      await dialog.getByLabel(/^API key/).fill("");
      await dialog.getByRole("button", { name: "Continue" }).click();
      await expect(dialog.getByText("Nothing recognised").first()).toBeVisible();
      await expect(dialog.getByText("Server not reachable")).toHaveCount(0);
      await expect(dialog.getByText(/Key accepted/)).toHaveCount(0);
      await expect(dialog.getByText("gate-chat")).toHaveCount(0);
      await fitsTheScreen(page, dialog);
      await page.keyboard.press("Escape");
    });

    test(`a duplicate address sends nothing, creates nothing, and opens the existing engine`, { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
      // An engine kept off at the gate's address: kept off, so the gateway never calls it.
      const created = await page.request.post("/admin/api/operator/services", {
        headers: await writeHeaders(page),
        data: { definition: "openai-compatible", name: `Gate engine ${width}`, values: { address: empty(gates[1]) }, disabled: true },
      });
      expect(created.status()).toBe(201);
      const serviceId: string = (await created.json()).service_id;
      try {
        await forget(gates[1]);
        const before = await serviceIds(page);
        const dialog = await openAddressStep(page);
        const creates: string[] = [];
        page.on("request", (request) => {
          if (request.method() === "POST" && request.url().endsWith("/admin/api/operator/services")) creates.push(request.url());
        });
        await dialog.getByLabel(/^Address/).fill(empty(gates[1]));
        await dialog.getByRole("button", { name: "Continue" }).click();
        await expect(dialog.getByText("Already added").first()).toBeVisible();
        await fitsTheScreen(page, dialog);
        expect(await seen(gates[1])).toEqual([]);
        expect(creates).toEqual([]);
        expect(await serviceIds(page)).toEqual(before);
        await dialog.getByRole("button", { name: `Open Gate engine ${width}` }).click();
        await expect(page).toHaveURL(new RegExp(`engine=${serviceId}`));
        await expect(page.getByRole("heading", { level: 1, name: `Gate engine ${width}` })).toBeVisible();
        await expect(dialog).toHaveCount(0);
        expect(await serviceIds(page)).toEqual(before);
      } finally {
        await removeEngine(page, serviceId);
      }
    });
  });
}

async function engineWithKey(page: Page, name: string): Promise<string> {
  const created = await page.request.post("/admin/api/operator/services", {
    headers: await writeHeaders(page),
    data: {
      definition: "openai-compatible",
      name,
      values: { address: keyed(gates[0]), api_key: GOOD_KEY },
      capabilities: ["chat"],
      acknowledge_warnings: true,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  return (await created.json()).service_id;
}

test("the stored key never reaches an edited address, checked or saved", { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
  await forget(gates[0]);
  await forget(gates[1]);
  const serviceId = await engineWithKey(page, "Key boundary engine");
  try {
    // Positive control: the key reached the address it was entered for.
    expect((await seen(gates[0])).some((r) => r.authorization === `Bearer ${GOOD_KEY}`)).toBe(true);

    await page.goto(`/engines/?engine=${serviceId}`);
    await expect(page.getByRole("heading", { level: 1, name: "Key boundary engine" })).toBeVisible();
    await page.getByLabel(/^Address/).fill(keyed(gates[1]));
    await expect(page.getByText(/Your saved key won’t be sent to a new address/)).toBeVisible();
    const checked = page.waitForResponse((r) => r.url().endsWith("/admin/api/engines/check"));
    await page.getByRole("button", { name: "Check again" }).first().click();
    expect((await checked).status()).toBe(200);
    const atNewAddress = await seen(gates[1]);
    expect(atNewAddress.length).toBeGreaterThan(0);
    expect(atNewAddress.filter((r) => r.authorization !== "")).toEqual([]);
    await expect(page.getByText(/Key accepted/)).toHaveCount(0);

    const saved = page.waitForResponse((r) => r.request().method() === "PATCH" && r.url().endsWith(`/services/${serviceId}`));
    await page.getByRole("button", { name: "Save and keep off" }).click();
    expect((await saved).status()).toBe(200);
    expect((await seen(gates[1])).filter((r) => r.authorization !== "")).toEqual([]);
    const config = (await (await page.request.get("/admin/api/operator/service-registry")).json()).service_registry.services[serviceId].config;
    expect(config.address).toBe(keyed(gates[1]));
    expect(config.api_key_secret_id).toBeUndefined();
    const view = (await (await page.request.get(`/admin/api/engines/${serviceId}`)).json()).engine;
    expect(view.key.state).toBe("none");
  } finally {
    await removeEngine(page, serviceId);
  }
});

test("a check that answers after the address changed cannot enable Save", { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
  const serviceId = await engineWithKey(page, "Stale check engine");
  try {
    await page.goto(`/engines/?engine=${serviceId}`);
    await expect(page.getByRole("heading", { level: 1, name: "Stale check engine" })).toBeVisible();
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/admin/api/engines/check", async (route) => {
      await held;
      await route.continue();
    });
    await page.getByLabel(/^Address/).fill(empty(gates[1]));
    const answered = page.waitForResponse((r) => r.url().endsWith("/admin/api/engines/check"));
    await page.getByRole("button", { name: "Check again" }).first().click();
    // While that check is out, the owner moves to another address.
    await page.getByLabel(/^Address/).fill(`http://127.0.0.1:${gates[1].port}/other/v1`);
    release();
    expect((await answered).status()).toBe(200);
    await expect(page.getByText("Out of date. Check again.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await page.unroute("**/admin/api/engines/check");
  } finally {
    await removeEngine(page, serviceId);
  }
});

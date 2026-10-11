import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const root = "/admin/api/categories";
const widths = [1440, 390] as const;
type Width = (typeof widths)[number];
type Revision = { revision: number; activated_at?: string; snapshot: ReturnType<typeof snapshot> };
type Candidate = Revision & {
  preview_base_revision: number | null;
  restored_from_revision: number;
  preview: {
    gains: { person: string; app: string; capability: string; service: string }[];
    losses: { person: string; app: string; capability: string; service: string }[];
  };
};

// The closure is immutable across revisions. Only Casey's grant changes;
// Notebook grants both capabilities, so restoring produces real gains AND losses.
function snapshot(id: string, capability: "chat" | "embed") {
  const grant = (cap: string) => ({ capability: cap, service: "local-service@r1" });
  return {
    id,
    categories: {
      location: {
        id: "location", label: "Data location", kind: "set",
        values: ["local", "remote"], applies_to: ["release"],
      },
    },
    people: {
      Casey: [{
        id: "person-access", grants: [grant(capability)],
        require: [{ id: "known-location", category: "location", op: "exists", values: [] }],
        deny: [], missing: {}, mode: "v3", legacy_tags: [],
      }],
    },
    apps: {
      Notebook: [{
        id: "app-access", grants: [grant("chat"), grant("embed")],
        require: [], deny: [], missing: {}, mode: "v3", legacy_tags: [],
      }],
    },
    services: {
      "local-service@r1": {
        id: "local-service@r1", checkable: true, dependencies: [],
        members: [{
          id: "local-model@r1", kind: "release",
          assertions: [{
            category: "location", value: "local", source: "Browser fixture",
            accepted: true, observed_at: "2026-01-01T00:00:00Z",
            accepted_at: "2026-01-01T00:00:00Z", accepting_owner: "Casey",
            mapping_version: "fixture-1",
          }],
        }],
      },
    },
    retained: {},
  };
}

async function surface(page: Page, width: Width) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "light" });
  await page.addInitScript(() => localStorage.setItem("theme", "light"));
}

async function accessible(page: Page, width: Width) {
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter(({ impact }) =>
    impact === "serious" || impact === "critical",
  )).toEqual([]);
  if (width === 390) {
    // Include disclosures as well as buttons, inputs and Radix select triggers.
    const targets = page.locator(
      "main button:visible, main input:visible, main [role=combobox]:visible, main summary:visible",
    );
    for (const target of await targets.all()) {
      const box = await target.boundingBox();
      expect(box, await target.evaluate((node) => node.outerHTML)).not.toBeNull();
      expect(box!.height, await target.evaluate((node) => node.outerHTML))
        .toBeGreaterThanOrEqual(44);
    }
  }
}

async function active(request: APIRequestContext): Promise<Revision | null> {
  const response = await request.get(root + "/active");
  const body = await response.json();
  if (response.status() === 409) {
    expect(body.error.code).toBe("no_active_snapshot");
    return null;
  }
  expect(response.status(), JSON.stringify(body)).toBe(200);
  return body;
}

async function writer(request: APIRequestContext) {
  const response = await request.get("/admin/api/session");
  expect(response.status()).toBe(200);
  const session = await response.json();
  const headers = {
    "x-azizam-admin-action": "1",
    "x-azizam-csrf": session.actor.csrf_token,
  };
  return async (path: string, data: unknown) => {
    const result = await request.post(root + path, { headers, data });
    const body = await result.json();
    expect(result.ok(), JSON.stringify(body)).toBe(true);
    return body;
  };
}

async function seed(request: APIRequestContext, id: string, capability: "chat" | "embed") {
  const post = await writer(request);
  const base = await active(request);
  const candidate: Candidate = await post("/candidates", {
    base_revision: base?.revision ?? null,
    reason: "Isolated Categories browser journey",
    snapshot: snapshot(id, capability),
  });
  await post(`/candidates/${candidate.revision}/activate`, {
    expected_active_revision: base?.revision ?? null,
  });
  const selected = await active(request);
  expect(selected?.revision).toBe(candidate.revision);
  return selected!;
}

async function choose(page: Page, label: string, option: string) {
  await page.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}

async function restore(page: Page, revision: number): Promise<Candidate> {
  const response = page.waitForResponse((result) =>
    new URL(result.url()).pathname === `${root}/revisions/${revision}/restore`
    && result.request().method() === "POST",
  );
  await page.getByRole("button", { name: `Restore revision ${revision}`, exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(200);
  const candidate: Candidate = await result.json();
  await expect(page.getByRole("region", { name: "Change preview", exact: true })).toBeVisible();
  return candidate;
}

// The fixture has no category reset endpoint. Run empty checks at both widths
// before mutation, then use CAS against the actual active revision, never IDs
// assumed from ordering. This suite requires the fresh configured fixture.
test.describe("Categories real fixture journeys", () => {
  test.describe.configure({ mode: "serial" });

  test("empty category store at desktop and phone widths", async ({ page, request }) => {
    expect(await active(request)).toBeNull();
    for (const width of widths) {
      await surface(page, width);
      await page.goto("/ui/categories/");
      await expect(page.getByText("No category rules are active", { exact: true })).toBeVisible();
      await expect(page.getByText(/Nothing is enforced by this page yet/)).toBeVisible();
      await expect(page.getByRole("region", { name: "Current rules", exact: true })).toHaveCount(0);
      await expect(page.getByText("No category history yet", { exact: true })).toBeVisible();
      await accessible(page, width);
    }
  });

  for (const width of widths) {
    test(`seed, preview, restore and stale base at ${width}px`, async ({ page, request }) => {
      test.setTimeout(180_000);
      await surface(page, width);
      await page.goto("/ui/categories/");
      const first = await seed(request, `browser-chat-${width}`, "chat");
      await page.reload();
      const current = page.getByRole("region", { name: "Current rules", exact: true });
      const preview = page.getByRole("region", { name: "Preview as", exact: true });
      const changes = page.getByRole("region", { name: "Change preview", exact: true });

      await test.step("API-seeded current rules are readable and dated", async () => {
        await expect(current.getByText(`Revision ${first.revision}`, { exact: true })).toBeVisible();
        await expect(current.getByText("Data location", { exact: true })).toBeVisible();
        await expect(current.getByText("Local, Remote", { exact: true })).toBeVisible();
        await expect(current.getByText("Person: Casey", { exact: true })).toBeVisible();
        await expect(current.getByText("App: Notebook", { exact: true })).toBeVisible();
        await expect(current.getByText("Access plan: Person access", { exact: true })).toBeVisible();
        await expect(current.getByText(/Needs Data location has an accepted value/)).toBeVisible();
        await expect(current.getByText("Data location: Local", { exact: true })).toBeVisible();
        expect(first.activated_at).toBeTruthy();
        const stamp = await page.evaluate((value) => new Date(value).toLocaleString(), first.activated_at!);
        await expect(current.getByText(`Turned on: ${stamp}`, { exact: true })).toBeVisible();
        await accessible(page, width);
      });

      await test.step("Preview as reports real allow and deny decisions", async () => {
        await choose(page, "Person", "Casey");
        await choose(page, "App", "Notebook");
        await choose(page, "Capability", "Chat");
        await choose(page, "Service", "local-service@r1");
        await preview.getByRole("button", { name: "Check access", exact: true }).click();
        await expect(preview.getByRole("status")).toContainText(`Allow · Revision ${first.revision}`);
        await expect(preview.getByText("Every attached access plan allows this request.", { exact: true })).toBeVisible();
        await accessible(page, width);

        await choose(page, "Capability", "Embeddings");
        await preview.getByRole("button", { name: "Check access", exact: true }).click();
        await expect(preview.getByRole("status")).toContainText(`Deny · Revision ${first.revision}`);
        await expect(preview.getByText("An access plan does not grant this capability with this service.", { exact: true })).toBeVisible();
        await accessible(page, width);
      });

      await test.step("Restore previews gains and losses without activation", async () => {
        const second = await seed(request, `browser-embed-${width}`, "embed");
        await page.reload();
        await expect(current.getByText(`Revision ${second.revision}`, { exact: true })).toBeVisible();
        await accessible(page, width);
        await page.getByLabel("Reason for restore (optional)", { exact: true }).fill("Restore chat access");
        const candidate = await restore(page, first.revision);
        expect(candidate.preview_base_revision).toBe(second.revision);
        expect(candidate.preview.gains).toContainEqual(expect.objectContaining({ person: "Casey", app: "Notebook", capability: "chat", service: "local-service@r1" }));
        expect(candidate.preview.losses).toContainEqual(expect.objectContaining({ person: "Casey", app: "Notebook", capability: "embed", service: "local-service@r1" }));
        const gains = changes.getByText("Who gains access", { exact: true }).locator("..");
        const losses = changes.getByText("Who loses access", { exact: true }).locator("..");
        await expect(gains).toContainText("Casey · Notebook · Chat · local-service@r1");
        await expect(losses).toContainText("Casey · Notebook · Embeddings · local-service");
        await expect(changes.getByText(/Nothing has been turned on/)).toBeVisible();
        expect((await active(request))?.revision).toBe(second.revision);
        await expect(current.getByText(`Revision ${second.revision}`, { exact: true })).toBeVisible();
        await accessible(page, width);

        await changes.getByRole("button", { name: "Close preview", exact: true }).click();
        await expect(changes).toHaveCount(0);
        expect((await active(request))?.revision).toBe(second.revision);
        const reopened = await restore(page, first.revision);
        await changes.getByRole("button", { name: "Turn on", exact: true }).click();
        await expect(current.getByText(`Revision ${reopened.revision}`, { exact: true })).toBeVisible();
        await expect(changes).toHaveCount(0);
        const selected = await active(request);
        expect(selected?.snapshot).toEqual(first.snapshot);
        expect(selected?.revision).toBe(reopened.revision);
        await accessible(page, width);

        // Keep a pending UI candidate while a separate owner API write wins CAS.
        await restore(page, second.revision);
        const concurrent = await seed(request, `browser-concurrent-${width}`, "chat");
        const staleResponse = page.waitForResponse((result) =>
          new URL(result.url()).pathname.startsWith(root + "/candidates/")
          && new URL(result.url()).pathname.endsWith("/activate")
          && result.request().method() === "POST",
        );
        await changes.getByRole("button", { name: "Turn on", exact: true }).click();
        const stale = await staleResponse;
        expect(stale.status()).toBe(409);
        expect((await stale.json()).error.code).toBe("stale_base");
        await expect(page.getByText("The active rules changed. Restore again to review a new preview.", { exact: true })).toBeVisible();
        await expect(current.getByText(`Revision ${concurrent.revision}`, { exact: true })).toBeVisible();
        await expect(changes).toHaveCount(0);
        expect((await active(request))?.revision).toBe(concurrent.revision);
        await accessible(page, width);
      });
    });

    test(`member sees no category rules at ${width}px`, async ({ page }) => {
      await surface(page, width);
      const categoryRequests: string[] = [];
      page.on("request", (request) => {
        if (new URL(request.url()).pathname.startsWith(root)) categoryRequests.push(request.url());
      });
      // Same member-session simulation used by workflows.spec.ts; category
      // responses remain real and are never intercepted.
      await page.route("**/me/api/session", (route) => route.fulfill({
        json: {
          authenticated: true, csrf_token: "fixture", apps: [],
          user: { id: "member-categories", name: "Member", role: "member", email: "member@example.test" },
        },
      }));
      await page.goto("/ui/categories/");
      // The shell's owner-page guard answers before the page renders.
      await expect(page.getByRole("heading", { name: "Owner access required", exact: true })).toBeVisible();
      await expect(page.getByRole("region", { name: "Current rules", exact: true })).toHaveCount(0);
      await expect(page.getByRole("region", { name: "Preview as", exact: true })).toHaveCount(0);
      await expect(page.getByRole("region", { name: "History", exact: true })).toHaveCount(0);
      await expect(page.getByText("Data location", { exact: true })).toHaveCount(0);
      await accessible(page, width);
      expect(categoryRequests).toEqual([]);
    });
  }
});

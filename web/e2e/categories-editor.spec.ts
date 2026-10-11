import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Snapshot } from "../src/app/categories/editor";

const root = "/admin/api/categories";
const widths = [1440, 390] as const;
type Width = (typeof widths)[number];
type Revision = { revision: number; activated_at?: string; snapshot: Snapshot };
type Candidate = Revision & {
  preview_base_revision: number | null;
  preview: {
    gains: { person: string; app: string; capability: string; service: string }[];
    losses: { person: string; app: string; capability: string; service: string }[];
  };
};

// Keep the proven categories.spec.ts API seed/CSRF/CAS helpers local: importing
// that spec would register its tests. Pinned IDs, scalar assertion values and
// mapping_version deliberately match the validated fixture.
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
    const targets = page.locator(
      "main button:visible, main input:visible, main [role=combobox]:visible, main summary:visible",
    );
    for (const target of await targets.all()) {
      // The full wrapping label is the checkbox's clickable target, not its
      // deliberately small visual glyph. Check both target dimensions.
      const box = await target.evaluate((node) => {
        const hit = node.matches('input[type="checkbox"]')
          ? node.closest("label") ?? node : node;
        const { width, height } = hit.getBoundingClientRect();
        return { width, height, html: hit.outerHTML };
      });
      expect(box.height, box.html).toBeGreaterThanOrEqual(44);
      expect(box.width, box.html).toBeGreaterThanOrEqual(44);
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
  // Never recycle service/member revision IDs across reruns or other specs.
  const unique = id + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  const body = snapshot(unique, capability);
  const serviceId = unique + "-service@r1";
  const service = body.services["local-service@r1"];
  service.id = serviceId;
  service.members[0].id = unique + "-model@r1";
  const seeded: Snapshot = { ...body, services: { [serviceId]: service } };
  for (const plan of [...seeded.people.Casey, ...seeded.apps.Notebook]) {
    for (const grant of plan.grants) grant.service = serviceId;
  }
  const candidate: Candidate = await post("/candidates", {
    base_revision: base?.revision ?? null,
    reason: "Isolated Categories browser journey",
    snapshot: seeded,
  });
  await post(`/candidates/${candidate.revision}/activate`, {
    expected_active_revision: base?.revision ?? null,
  });
  const selected = await active(request);
  expect(selected?.revision).toBe(candidate.revision);
  return selected!;
}

function editor(page: Page) {
  return page.getByRole("region", { name: "Edit category rules", exact: true });
}

function writes(page: Page) {
  const sent: { path: string; body: Record<string, unknown> }[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith(root) && request.method() === "POST") {
      sent.push({ path, body: request.postDataJSON() });
    }
  });
  return sent;
}

async function openEditor(page: Page) {
  await page.getByRole("button", { name: "Edit rules", exact: true }).click();
  await expect(editor(page).getByLabel("New category label", { exact: true })).toBeEnabled();
}

async function addCategory(page: Page, label: string, values: string[]) {
  const form = editor(page);
  await form.getByLabel("New category label", { exact: true }).fill(label);
  await form.getByRole("button", { name: "Add category", exact: true }).click();
  for (const value of values) {
    await form.getByLabel("New value for " + label, { exact: true }).fill(value);
    await form.getByRole("button", { name: "Add value to " + label, exact: true }).click();
  }
}

async function pick(page: Page, scope: Locator, label: string, option: string) {
  await scope.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}

async function preview(page: Page, base: number | null): Promise<Candidate> {
  const response = page.waitForResponse((result) =>
    new URL(result.url()).pathname === root + "/candidates"
    && result.request().method() === "POST",
  );
  await editor(page).getByRole("button", { name: "Preview changes", exact: true }).click();
  const result = await response;
  const body = await result.json();
  expect(result.status(), JSON.stringify(body)).toBe(200);
  expect(result.request().postDataJSON().base_revision).toBe(base);
  expect(body.preview_base_revision).toBe(base);
  const changes = editor(page).getByRole("region", { name: "Change preview", exact: true });
  await expect(changes).toContainText(
    "Compared with " + (base === null ? "no active revision" : "revision " + base) + ". Nothing has been turned on.",
  );
  return body;
}

async function turnOn(page: Page, request: APIRequestContext, candidate: Candidate) {
  const response = page.waitForResponse((result) =>
    new URL(result.url()).pathname === root + "/candidates/" + candidate.revision + "/activate"
    && result.request().method() === "POST",
  );
  await editor(page).getByRole("button", { name: "Turn on", exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(200);
  expect(result.request().postDataJSON()).toEqual({
    expected_active_revision: candidate.preview_base_revision,
  });
  await expect(editor(page).getByText(
    "Revision " + candidate.revision + " is now selected for category previews.", { exact: true },
  )).toBeVisible();
  expect((await active(request))?.revision).toBe(candidate.revision);
}

async function closeAndRead(page: Page, revision: number) {
  await editor(page).getByRole("button", { name: "Close editor", exact: true }).click();
  const current = page.getByRole("region", { name: "Current rules", exact: true });
  await expect(current.getByText("Revision " + revision, { exact: true })).toBeVisible();
  return current;
}

// The fixture is shared by files and may already contain active rules. Only
// the empty rendering/client-validation test stubs GET active; every write
// journey uses the real API, current CAS base and fresh pinned revision IDs.
test.describe("Categories editor real fixture journeys", () => {
  test.describe.configure({ mode: "default" });

  test("empty rendering and local validation at both widths send no writes", async ({ page }) => {
    test.setTimeout(180_000);
    await page.route("**/admin/api/categories/active", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 409,
        json: { error: { code: "no_active_snapshot", message: "No policy snapshot has been activated." } },
      });
    });
    const sent = writes(page);
    for (const width of widths) {
      await surface(page, width);
      await page.goto("/ui/categories/");
      await expect(page.getByText("No category rules are active", { exact: true })).toBeVisible();
      await accessible(page, width);
      await openEditor(page);
      const form = editor(page);
      await expect(form.getByText("No services are saved in these rules yet.", { exact: true })).toBeVisible();
      await accessible(page, width);
      await form.getByRole("button", { name: "Add category", exact: true }).click();
      await expect(form.getByRole("alert")).toHaveText(
        "Enter a category label with at least one letter or number.",
      );
      await accessible(page, width);
      expect(sent).toEqual([]);

      await addCategory(page, "Data location", ["local", "remote"]);
      await form.getByLabel("Reason for changes", { exact: true }).fill("Validate category label");
      await form.getByLabel("Label for data-location", { exact: true }).fill("");
      await form.getByRole("button", { name: "Preview changes", exact: true }).click();
      await expect(form.getByRole("alert")).toContainText("Every category needs a label.");
      await accessible(page, width);
      expect(sent).toEqual([]);
      await form.getByRole("button", { name: "Close editor", exact: true }).click();
      await expect(form).toHaveCount(0);
      await accessible(page, width);
    }
    expect(sent).toEqual([]);
  });

  for (const width of widths) {
    test(`service values, new plans, blocked removal, rewrite and gains at ${width}px`, async ({ page, request }) => {
      test.setTimeout(180_000);
      await surface(page, width);
      const base = await seed(request, "editor-plans-" + width, "embed");
      const serviceId = Object.keys(base.snapshot.services)[0];
      const memberId = base.snapshot.services[serviceId].members[0].id;
      await page.goto("/ui/categories/");
      await openEditor(page);
      const form = editor(page);
      const sent = writes(page);
      await accessible(page, width);
      await addCategory(page, "Data handling", ["private", "shared"]);
      const accepted = form.getByRole("group", {
        name: "Accepted Data handling for " + memberId, exact: true,
      });
      await accepted.getByRole("checkbox", { name: "Private", exact: true }).check();
      await accessible(page, width);

      for (const kind of ["Person", "App"] as const) {
        const section = form.getByRole("region", { name: kind + " access plans", exact: true });
        await pick(page, section, kind + " for new access plan", kind === "Person" ? "Casey" : "Notebook");
        await section.getByRole("button", { name: "Add " + kind.toLowerCase() + " access plan", exact: true }).click();
        // New plan is the last plan in the sole seeded identity's card.
        const plan = section.getByLabel("Access plan name", { exact: true }).last().locator("../..");
        await plan.getByLabel("Access plan name", { exact: true }).fill(kind.toLowerCase() + "-reviewed");
        await plan.getByRole("button", { name: "Add grant", exact: true }).click();
        await pick(page, plan, "Capability", "Embeddings");
        await pick(page, plan, "Service", serviceId);
        await plan.getByRole("button", { name: "Add requirement", exact: true }).click();
        await pick(page, plan, "Category", "Data handling");
        const allowed = plan.getByRole("group", { name: "Allowed values", exact: true });
        await allowed.getByRole("checkbox", { name: "Private", exact: true }).check();
        await allowed.getByRole("checkbox", { name: "Shared", exact: true }).check();
        await accessible(page, width);
      }

      // Shared is used only in plan rows, never a service assertion.
      await form.getByRole("button", { name: "Remove Shared from Data handling", exact: true }).click();
      await expect(form.getByRole("alert")).toHaveText(
        "Shared is used by an access plan or a service. Remove that use before removing this value.",
      );
      await expect(form.getByRole("button", { name: "Remove Shared from Data handling", exact: true })).toBeVisible();
      await accessible(page, width);
      expect(sent).toEqual([]);
      expect((await active(request))?.revision).toBe(base.revision);

      const rewrites = form.getByRole("region", { name: "Service grant changes", exact: true });
      await expect(rewrites).toBeVisible();
      const rewriteText = await rewrites.innerText();
      const next = /→ (\S+)/.exec(rewriteText)?.[1];
      expect(next, rewriteText).toBeTruthy();
      const nextService = next!;
      expect(nextService).toMatch(/@r2-[a-z0-9]+$/);
      expect(nextService).not.toBe(serviceId);
      await expect(rewrites).toContainText(serviceId + " → " + nextService);
      await expect(rewrites).toContainText("Casey · Person reviewed");
      await expect(rewrites).toContainText("Notebook · App reviewed");
      await expect(form.getByRole("region", { name: "Change preview", exact: true })).toHaveCount(0);
      await form.getByLabel("Reason for changes", { exact: true }).fill("Review new data-handling service access");
      const candidate = await preview(page, base.revision);
      expect((await active(request))?.revision).toBe(base.revision);
      expect(sent.map(({ path }) => path)).toEqual([root + "/candidates"]);
      expect(candidate.snapshot.services[nextService]).toBeDefined();
      for (const plans of [candidate.snapshot.people.Casey, candidate.snapshot.apps.Notebook]) {
        for (const plan of plans) expect(plan.grants.every(({ service }) => service === nextService)).toBe(true);
      }
      expect(candidate.preview.gains).toContainEqual(expect.objectContaining({
        person: "Casey", app: "Notebook", capability: "embed", service: nextService,
      }));
      const changes = form.getByRole("region", { name: "Change preview", exact: true });
      await expect(changes.getByText("Who gains access", { exact: true }).locator(".."))
        .toContainText("Casey · Notebook · Embeddings · " + nextService);
      await accessible(page, width);
      await turnOn(page, request, candidate);
      const selected = await active(request);
      expect(selected?.snapshot).toEqual(candidate.snapshot);
      await accessible(page, width);
      const current = await closeAndRead(page, candidate.revision);
      await expect(current.getByText("Data handling: Private", { exact: true })).toBeVisible();
      await expect(current.getByText("Allows Embeddings with " + nextService, { exact: true })).toHaveCount(4);
      await expect(current.getByText("Access plan: Person reviewed", { exact: true })).toBeVisible();
      await expect(current.getByText("Access plan: App reviewed", { exact: true })).toBeVisible();
      await accessible(page, width);
    });

    for (const stage of ["preview", "activation"] as const) {
      test(`stale ${stage} rebuild preserves both owners' changes at ${width}px`, async ({ page, request }) => {
        test.setTimeout(180_000);
        await surface(page, width);
        const base = await seed(request, "editor-base-" + stage + "-" + width, "embed");
        await page.goto("/ui/categories/");
        await openEditor(page);
        const form = editor(page);
        await form.getByLabel("Label for location", { exact: true }).fill("Reviewed data location");
        await form.getByLabel("Reason for changes", { exact: true }).fill("Preserve my label after concurrent activation");
        await accessible(page, width);
        const pending = stage === "activation" ? await preview(page, base.revision) : null;
        if (pending) await accessible(page, width);
        const concurrent = await seed(request, "editor-concurrent-" + stage + "-" + width, "chat");
        const response = page.waitForResponse((result) =>
          new URL(result.url()).pathname === (pending
            ? root + "/candidates/" + pending.revision + "/activate" : root + "/candidates")
          && result.request().method() === "POST",
        );
        await form.getByRole("button", {
          name: pending ? "Turn on" : "Preview changes", exact: true,
        }).click();
        const stale = await response;
        expect(stale.status()).toBe(409);
        expect((await stale.json()).error.code).toBe("stale_base");
        await expect(form.getByText("The active rules changed. Create a new preview.", { exact: true })).toBeVisible();
        await expect(form.getByRole("button", { name: "Turn on", exact: true })).toHaveCount(0);
        await expect(form.getByRole("button", { name: "Preview changes", exact: true })).toBeDisabled();
        expect((await active(request))?.revision).toBe(concurrent.revision);
        await accessible(page, width);
        await form.getByRole("button", { name: "Rebuild on current rules", exact: true }).click();
        await expect(form.getByText(
          "Your edit is rebuilt on the current rules. Preview changes again before turning them on.", { exact: true },
        )).toBeVisible();
        await expect(form.getByLabel("Label for location", { exact: true })).toHaveValue("Reviewed data location");
        await accessible(page, width);
        const rebuilt = await preview(page, concurrent.revision);
        if (pending) expect(rebuilt.revision).not.toBe(pending.revision);
        expect(rebuilt.snapshot.id).toBe(concurrent.snapshot.id);
        expect(rebuilt.snapshot.people).toEqual(concurrent.snapshot.people);
        expect(rebuilt.snapshot.categories.location.label).toBe("Reviewed data location");
        expect((await active(request))?.revision).toBe(concurrent.revision);
        await accessible(page, width);
        await turnOn(page, request, rebuilt);
        await accessible(page, width);
        const current = await closeAndRead(page, rebuilt.revision);
        await expect(current.getByText("Reviewed data location", { exact: true })).toBeVisible();
        await accessible(page, width);
      });
    }
  }
});

test("catalog service can be added, granted and previewed with category APIs stubbed", async ({ page }) => {
  await surface(page, 390);
  const base = snapshot("catalog-browser", "chat");
  base.people.Casey[0].grants = [];
  base.people.Casey[0].require = [];
  base.apps.Notebook[0].grants = [];
  const selected: Snapshot = { ...base, services: {} };
  const sent = writes(page);
  await page.route("**/admin/api/categories/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === root + "/active") return route.fulfill({ json: { revision: 2, snapshot: selected, reason: "Fixture", author: "owner", created_at: "2026-10-10T00:00:00Z" } });
    if (path === root + "/history") return route.fulfill({ json: { history: [] } });
    if (path === root + "/services") return route.fulfill({ json: { services: [{ stem: "workflow:paint", label: "Paint workflow", kind: "workflow", capabilities: ["image.generate", "image.edit"] }] } });
    if (path === root + "/candidates" && route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      return route.fulfill({ json: { revision: 3, snapshot: body.snapshot, preview_base_revision: 2, preview: { gains: [], losses: [], reason_changes: [], scope: [] } } });
    }
    throw new Error("Unexpected category API: " + path);
  });
  await page.goto("/ui/categories/");
  await openEditor(page);
  const form = editor(page);
  await pick(page, form, "Service to add", "Paint workflow");
  await form.getByRole("button", { name: "Add service", exact: true }).click();
  await expect(form.getByRole("group", { name: "Workflow: workflow:paint@r1", exact: true })).toBeVisible();
  for (const name of ["Person access plans", "App access plans"]) {
    const plans = form.getByRole("region", { name, exact: true });
    await plans.getByRole("button", { name: "Add grant", exact: true }).click();
    await pick(page, plans, "Service", "workflow:paint@r1");
    await pick(page, plans, "Capability", "Image editing");
  }
  await form.getByLabel("Reason for changes", { exact: true }).fill("Grant image edits");
  const candidate = await preview(page, 2);
  expect(sent.map(({ path }) => path)).toEqual([root + "/candidates"]);
  expect(candidate.snapshot.services["workflow:paint@r1"]).toEqual({ id: "workflow:paint@r1", members: [{ id: "workflow:paint@r1", kind: "workflow", assertions: [] }], dependencies: [], checkable: true });
  for (const plans of [candidate.snapshot.people.Casey, candidate.snapshot.apps.Notebook]) {
    expect(plans[0].grants).toEqual([{ capability: "image.edit", service: "workflow:paint@r1" }]);
  }
  await accessible(page, 390);
});

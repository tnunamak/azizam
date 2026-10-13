import { type Locator, type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

const aliases = [
  ["fast", "chat.completions", "Chat", "chat", "Fast"],
  ["image", "image.generate", "Images", "image", "Images"],
  ["speech", "speech.synthesis", "Speech", "speech", "Speech"],
].map(([id, intent, label, capability, title]) => ({
  id,
  object: "model_catalog.alias",
  display_name: title,
  title,
  capability_title: label,
  icon_key: capability,
  use_names: [id],
  secondary_profile: false,
  alias: true,
  operations: [],
  resolves_to: {},
  description: `${label} through a configured model.`,
  capabilities: [{ intent, label, capability, icon_key: capability }],
  provider: "Local engine",
  engine_label: "Local engine",
  locality: "local",
  cost: { kind: "free", price: null },
  timing: { typical_seconds: 0.8, source: "receipts" },
  availability: {
    available: true,
    measured: true,
    status: "ready",
    reason: "Ready for your next request.",
  },
  io: { input: ["text"], output: ["text"] },
  examples: [],
  recent_receipts: [],
  use: { model: id },
}));

const plans = ["Family", "Creative", "Builders"].map((name, index) => ({
  id: `plan-${index}`,
  name,
  created_at: 1720000000,
  updated_at: 1720000000,
  plan: {
    id: `plan-${index}`,
    name,
    can_use: { chat: { items: "all" }, image: { items: "all" } },
    direct_engine_access: { engines: [], operations: [] },
    paid_providers: [],
    provider_fallback: false,
    caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
    can_load_models: true,
    receipt_detail: "basic",
    media_metadata: "none",
  },
  summary: "Chat and images. Local models only. No paid providers.",
  used_by: { people: [], apps: [], invites: 0 },
}));
const choices = plans.map(({ id, name, summary }) => ({ id, name, summary }));

// Copy, grouping, price/provider text and plan summaries are asserted in
// web/src/app/models/models-client.spec.tsx and web/tests/people-plans-list.spec.tsx.
// The browser keeps what needs real layout: full-width rows, no page overflow,
// the phone row height and the below-640 px heading, plus axe in both themes.

async function prepare(page: Page, width: number, theme: "light" | "dark") {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      ),
    )
    .toBeLessThanOrEqual(0);
}

async function verifyModelLayout(page: Page, cards: Locator, count: number) {
  await expect(cards).toHaveCount(count);
  await expect
    .poll(() =>
      cards.evaluateAll((elements) =>
        Math.min(...elements.map((element) => element.getBoundingClientRect().width)),
      ),
    )
    .toBeGreaterThan(280);
  await expectNoHorizontalOverflow(page);
  const a11y = await new AxeBuilder({ page }).analyze();
  expect(
    a11y.violations
      .filter((violation) =>
        ["serious", "critical"].includes(violation.impact ?? ""),
      )
      .map((violation) => violation.id),
  ).toEqual([]);
}

async function stubCatalog(page: Page, rows: unknown[]) {
  await page.route("**/me/api/models/catalog", (route) =>
    fulfilJson(route, {
        schema_version: 1,
        object: "models.catalog",
        aliases: rows,
        models: [],
      }),
  );
}

const modelCards = (page: Page) =>
  page.locator('table[aria-label="Available model names"] tbody tr');

test("polished models: three aliases light 1440 fill the width and pass axe", async ({
  page,
}) => {
  await prepare(page, 1440, "light");
  await stubCatalog(page, aliases);
  await page.goto("/models/");
  await expect(
    page.getByRole("heading", { name: "Fast", exact: true }),
  ).toBeVisible();
  await verifyModelLayout(page, modelCards(page), 3);
});

test("polished model grouping keeps its width and passes axe after a search", async ({
  page,
}) => {
  await prepare(page, 1440, "light");
  const chatAliases = ["fast", "default", "balanced"].map((id) => ({
    ...aliases[0],
    id,
    display_name: id[0].toUpperCase() + id.slice(1),
    title: id[0].toUpperCase() + id.slice(1),
    use_names: [id],
    use: { model: id },
  }));
  await stubCatalog(page, chatAliases);
  await page.goto("/models/");
  await expect(
    page.getByRole("heading", { name: "Fast", exact: true }),
  ).toBeVisible();
  await verifyModelLayout(page, modelCards(page), 1);
  await page.getByRole("textbox", { name: "Search models" }).fill("fast");
  await expect(page.getByText(/\d+ more/)).toHaveCount(0);
  await verifyModelLayout(page, modelCards(page), 1);
});

test("polished models: two aliases dark 390 name the task, fill the width and pass axe", async ({
  page,
}) => {
  await prepare(page, 390, "dark");
  await stubCatalog(page, aliases.slice(0, 2));
  await page.goto("/models/");
  // Below 640 px a row is two lines: the task name, then the alias. The
  // model's friendly title is a desktop column.
  await expect(
    page.getByRole("heading", { name: "Chat", exact: true }),
  ).toBeVisible();
  await verifyModelLayout(page, modelCards(page), 2);
});

test("paid model rows with a long provider name keep the 390 px page inside the viewport", async ({
  page,
}) => {
  await prepare(page, 390, "light");
  const provider =
    "Example remote provider with a long configured display name";
  const price =
    "$0.80 per million input tokens / $2.40 per million output tokens";
  const paid = ["image", "portrait", "illustration"].map((id) => ({
    ...aliases[1],
    id,
    display_name: id[0].toUpperCase() + id.slice(1),
    title: id[0].toUpperCase() + id.slice(1),
    use_names: [id],
    provider,
    engine_label: provider,
    locality: "remote",
    cost: { kind: "paid", price },
    timing: { typical_seconds: null, source: null },
    use: { model: id },
  }));
  await stubCatalog(page, paid);
  await page.goto("/models/");
  await verifyModelLayout(page, modelCards(page), 1);
});

async function checkPlanRows(page: Page, width: number) {
  await page.route("**/admin/api/people", (route) =>
    fulfilJson(route, { people: [], access_plans: choices }),
  );
  await page.route("**/admin/api/invites", (route) =>
    fulfilJson(route, { invites: [], access_plans: choices, invite_access_plan: null }),
  );
  await page.route("**/admin/api/access-plans", (route) =>
    fulfilJson(route, { access_plans: plans }),
  );
  await page.goto("/people/");
  await page.getByRole("tab", { name: "Access plans" }).click();
  await expect(
    page.getByRole("heading", { name: "Family", exact: true }),
  ).toBeVisible();
  // Craft PR 3: plans are one ListCard of full-width rows, 60 tall (64 on
  // a phone), each with its summary and counts on the second line.
  const rows = page.getByRole("tabpanel").getByRole("listitem");
  await expect(rows).toHaveCount(3);
  const geometry = async () => {
    const boxes = await rows.evaluateAll((elements) =>
      elements.map((element) => element.getBoundingClientRect().toJSON()),
    );
    const panelWidth = await page
      .getByRole("tabpanel")
      .evaluate((element) => element.getBoundingClientRect().width);
    return {
      alignedLeft: boxes.every(
        (box) => Math.round(box.x) === Math.round(boxes[0].x),
      ),
      fullWidth: boxes.every((box) => box.width > panelWidth - 4),
      heights: boxes.map((box) => Math.round(box.height)),
      stacked: boxes.every((box, index) => !index || box.y > boxes[index - 1].y),
    };
  };
  await expect.poll(geometry).toEqual({
    alignedLeft: true,
    fullWidth: true,
    heights: Array(3).fill(width < 640 ? 64 : 60),
    stacked: true,
  });
  await expectNoHorizontalOverflow(page);
}

test("polished access plans light 1440 are full-width 60 px rows", async ({
  page,
}) => {
  await prepare(page, 1440, "light");
  await checkPlanRows(page, 1440);
});

test("polished access plans dark 390 are full-width 64 px rows", async ({
  page,
}) => {
  await prepare(page, 390, "dark");
  await checkPlanRows(page, 390);
});

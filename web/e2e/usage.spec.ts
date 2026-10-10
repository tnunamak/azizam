import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const seededSummary = {
  requests: 4,
  media_count: 1,
  remote_spend: 0.023,
  remote_spend_available: true,
  paid_requests: 3,
  spend_measured_requests: 3,
  spend_total_requests: 4,
  retention_days: 14,
  coverage_start: "2026-10-01T00:00:00Z",
  coverage_note: "Provider spend is available for matched receipts.",
  by_day: [
    { date: "2026-10-01", chat: 1, image: 1 },
    { date: "2026-10-02", chat: 1, embeddings: 1 },
  ],
  by_model: [
    { model: "demo-chat", capability: "chat", requests: 2, remote_spend: 0.01 },
    { model: "image", capability: "image", requests: 1, remote_spend: 0.013 },
  ],
  receipts: [
    {
      request_id: "req_demo_chat",
      accepted_at_ms: 1790870400000,
      requested_model: "demo-chat",
      resolved_model: "demo-chat",
      intent: "chat",
      capability: "chat",
      status: "success",
      latency_ms: 104,
      remote_spend: null,
    },
  ],
};

async function expectNoSeriousAxeViolations(
  page: import("@playwright/test").Page,
) {
  const a11y = await new AxeBuilder({ page }).analyze();
  const violations = a11y.violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations, "Serious accessibility violations").toEqual([]);
}

async function expectNoHorizontalOverflow(
  page: import("@playwright/test").Page,
) {
  const widths = await page.evaluate(() => ({
    scroll: (document.scrollingElement ?? document.documentElement).scrollWidth,
    inner: window.innerWidth,
    wide: [...document.querySelectorAll<HTMLElement>("body *")]
      .filter((node) => node.getBoundingClientRect().right > window.innerWidth + 1)
      .slice(0, 6)
      .map((node) => `${node.tagName.toLowerCase()}.${String(node.className).slice(0, 70)}`),
  }));
  expect(
    widths.scroll,
    `Page must fit the viewport; wide: ${widths.wide.join(" | ")}`,
  ).toBeLessThanOrEqual(widths.inner);
}

test("usage lists a receipt and opens technical details", async ({ page }) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: seededSummary }),
  );
  await page.goto("/ui/usage/");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await page.getByText("Receipt coverage", { exact: true }).click();
  await expect(page.getByText("Receipts are kept for 14 days.")).toBeVisible();
  await expect(page.getByText("Coverage starts Oct 1, 2026.")).toBeVisible();
  await expect(
    page.getByText("Provider spend is available for matched receipts."),
  ).toBeVisible();
  await expect(page.getByText("Demo Chat").first()).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "Typical time" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "View recent requests" }).click();
  const receipt = page.getByRole("button", {
    name: /Receipt details for Chat request/,
  });
  await expect(receipt).toContainText("104 ms");
  await expect(receipt.locator("p.font-medium")).toHaveText("Chat request");
  // The row and its tooltip show the model's display name (5524a85f).
  await expect(receipt.getByText("Demo Chat")).toHaveAttribute(
    "title",
    "Demo Chat",
  );
  await receipt.first().click();
  const sheet = page.getByRole("dialog", { name: "Request receipt" });
  await expect(sheet).toBeVisible();
  await expect(sheet).toContainText("Demo Chat");
  await expect(sheet).toContainText("104 ms");
  await sheet.getByText("Technical details", { exact: true }).click();
  // The raw model id an app sends stays in Technical details for debugging.
  const technical = sheet.locator("details").filter({
    hasText: "Technical details",
  });
  await expect(technical).toContainText("request_id");
  await expect(technical).toContainText("requested_model");
  await expect(technical).toContainText('"demo-chat"', { useInnerText: true });
});

test("usage omits the model line when only the capability is known", async ({
  page,
}) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      json: {
        ...seededSummary,
        by_model: [
          {
            model: null,
            capability: "transcribe",
            requests: 6,
            remote_spend: null,
          },
        ],
      },
    }),
  );

  await page.goto("/ui/usage/");
  const transcriptionRow = page
    .getByRole("table", { name: "Models" })
    .getByRole("row")
    .filter({ hasText: "Transcribe a recording" });
  await expect(transcriptionRow).toBeVisible();
  await expect(transcriptionRow.locator("code")).toHaveCount(0);
  await expect(transcriptionRow).not.toContainText("Unknown model");
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`usage empty ${theme} at ${width}px`, async ({ page }) => {
      await prepareUsageViewport(page, theme, width);
      await page.route("**/me/api/usage/summary?**", (route) =>
        route.fulfill({
          json: {
            requests: 0,
            media_count: 0,
            remote_spend: 0,
            remote_spend_available: false,
            retention_days: 14,
            by_day: [],
            by_model: [],
            receipts: [],
          },
        }),
      );
      await page.goto("/ui/usage/");
      await expect(
        page.getByText("No requests in this period", { exact: true }),
      ).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectNoSeriousAxeViolations(page);
      await page.screenshot({
        path: `test-results/screens/usage-empty-${theme}-${width}.png`,
        fullPage: true,
      });
    });

    test(`usage error ${theme} at ${width}px`, async ({ page }) => {
      await prepareUsageViewport(page, theme, width);
      let failed = true;
      await page.route("**/me/api/usage/summary?**", (route) => {
        if (failed) {
          failed = false;
          return route.fulfill({
            status: 503,
            json: { error: { message: "Unavailable for this test" } },
          });
        }
        return route.fulfill({ json: seededSummary });
      });
      await page.goto("/ui/usage/");
      await expect(
        page.getByText("Usage could not be loaded", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText("Unknown · Usage unavailable", { exact: true }),
      ).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectNoSeriousAxeViolations(page);
      await page.screenshot({
        path: `test-results/screens/usage-error-${theme}-${width}.png`,
        fullPage: true,
      });
      await page
        .getByRole("button", { name: "Retry usage", exact: true })
        .click();
      await expect(page.getByText("Demo Chat").first()).toBeVisible();
    });

    test(`usage ${theme} at ${width}px`, async ({ page }) => {
      await prepareUsageViewport(page, theme, width);
      await page.route("**/me/api/usage/summary?**", (route) =>
        route.fulfill({ json: seededSummary }),
      );
      await page.goto("/ui/usage/");
      await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
      await expect(
        page.locator('[aria-label="Loading your account"]'),
      ).toHaveCount(0);
      await expect(page.getByText("Remote spend")).toBeVisible();
      await page.getByRole("button", { name: "View recent requests" }).click();
      await expect(page.getByText("Receipts", { exact: true })).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await expectNoSeriousAxeViolations(page);
      await page.screenshot({
        path: `test-results/screens/usage-lane-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

async function prepareUsageViewport(
  page: import("@playwright/test").Page,
  theme: "light" | "dark",
  width: 1440 | 390,
) {
  await page.setViewportSize({
    width,
    height: width === 390 ? 844 : 1000,
  });
  await page.emulateMedia({
    colorScheme: theme,
    reducedMotion: "reduce",
  });
  await page.addInitScript((value) => {
    localStorage.setItem("theme", value);
  }, theme);
}

test("usage labels unavailable remote spend honestly", async ({ page }) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      json: {
        ...seededSummary,
        remote_spend: 0,
        remote_spend_available: false,
        paid_requests: 0,
        spend_measured_requests: 0,
        spend_total_requests: 4,
      },
    }),
  );
  await page.goto("/ui/usage/");
  const remoteSpendCard = page
    .getByText("Remote spend", { exact: true })
    .locator("..");
  await expect(remoteSpendCard).toContainText("Cost not recorded");
  await expect(remoteSpendCard).not.toContainText("$0");
});

test("usage reads the real account summary and seeded receipt", async ({
  page,
  request,
}) => {
  const session = await request.get("/me/api/session");
  expect(session.status()).toBe(200);
  const account = await session.json();
  expect(
    account.authenticated,
    "Seeded owner must have an account session",
  ).toBe(true);
  const response = await request.get("/me/api/usage/summary?period=month");
  expect(response.status(), "Summary must accept the same account cookie").toBe(
    200,
  );
  const summary = await response.json();
  expect(summary.requests).toBeGreaterThan(0);
  expect(
    summary.receipts.some(
      (receipt: { requested_model: string }) =>
        receipt.requested_model === "demo-chat",
    ),
  ).toBe(true);
  await page.goto("/ui/usage/");
  await page.getByRole("button", { name: "View recent requests" }).click();
  await expect(
    page
      .getByRole("button", { name: /Receipt details for Chat request/ })
      .first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /Receipt details for Chat request/ })
    .first()
    .click();
  await expect(
    page.getByRole("dialog", { name: "Request receipt" }),
  ).toContainText("succeeded");
});

test("Usage person filter does not promote an email address to a person name", async ({
  page,
}) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      json: {
        ...seededSummary,
        people: [
          { id: "person-email", name: "person@example.test" },
          { id: "person-second", label: "second@example.test" },
        ],
      },
    }),
  );
  await page.goto("/ui/usage/");

  await page.getByRole("combobox", { name: "Person" }).click();
  const people = page.getByRole("option", { name: /^Person · / });
  await expect(people).toHaveCount(2);
  const labels = await people.allTextContents();
  expect(new Set(labels).size).toBe(2);
  expect(labels.join(" ")).not.toContain("@example.test");
  await expect(
    page.getByRole("option", { name: "person@example.test", exact: true }),
  ).toHaveCount(0);
  const summaryRequest = page.waitForRequest((request) =>
    request.url().includes("person=person-second"),
  );
  await people.nth(1).click();
  await summaryRequest;
});

const GGUF =
  "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf";

const phoneSummary = {
  ...seededSummary,
  requests: 8264,
  by_model: [
    { model: GGUF, capability: "chat", requests: 3402, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: null, capability: "transcribe", requests: 2322, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "Qwen3 (Q4)", capability: "chat", requests: 505, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "default", capability: "chat", requests: 408, remote_spend: 0, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "decide-quality", capability: "decide", requests: 377, remote_spend: null, paid_requests: 0, unknown_paid_requests: 0 },
    { model: "paid-remote-model", capability: "image", requests: 300, remote_spend: null, paid_requests: 0, unknown_paid_requests: 4 },
  ],
  by_day: [
    { date: "2026-10-01", chat: 5, image: 1, audio: 2, transcribe: 3, decide: 4, embeddings: 5 },
    { date: "2026-10-02", chat: 1 },
  ],
  receipts: [
    { ...seededSummary.receipts[0], requested_model: "default", resolved_model: GGUF },
  ],
};

for (const theme of ["dark", "light"] as const) {
  test(`usage fits a 390 px phone with long model names (${theme})`, async ({ page }) => {
    await prepareUsageViewport(page, theme, 390);
    await page.route("**/me/api/usage/summary?**", (route) =>
      route.fulfill({ json: phoneSummary }),
    );
    await page.goto("/ui/usage/");
    await expect(page.getByText("Top models by requests")).toBeVisible();
    await expectNoHorizontalOverflow(page);

    // The tab bar is not cut off: its right edge is inside the viewport.
    const tabs = page.getByRole("navigation", { name: "Phone navigation" });
    const box = await tabs.boundingBox();
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);

    // Every row sits inside the viewport, with no clipped left edge.
    const rows = page
      .getByRole("table")
      .filter({ hasText: "Task and model" })
      .getByRole("row");
    for (let index = 0; index < (await rows.count()); index += 1) {
      const rowBox = await rows.nth(index).boundingBox();
      expect(rowBox?.x ?? 0).toBeGreaterThanOrEqual(0);
      expect((rowBox?.x ?? 0) + (rowBox?.width ?? 0)).toBeLessThanOrEqual(390);
    }
  });
}

test("usage model table shows names, never a path, and a consistent Cost", async ({ page }) => {
  await prepareUsageViewport(page, "dark", 390);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: phoneSummary }),
  );
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        object: "models.catalog",
        aliases: [{ id: "decide-quality", title: "Careful choices" }],
        models: [],
        counts: { aliases: 1, models: 0, capabilities: 0 },
      },
    }),
  );
  await page.goto("/ui/usage/");
  const table = page.getByRole("table").filter({ hasText: "Task and model" });
  await expect(table).toBeVisible();
  const text = await table.innerText();
  expect(text).not.toMatch(/\/media\/|AI Models|\/GGUF\/|\.gguf/);
  // A weight file reads as its model's name: no path, extension or packing
  // tag, not even in the tooltip (5524a85f).
  expect(text).not.toMatch(/Q4 K M|Q4_K_M/);
  await expect(table).toContainText("Qwen3.8 27B Uncensored");
  await expect(table).toContainText("Careful choices");
  await expect(table).toContainText("Transcribe a recording");
  await expect(table.getByTitle("Qwen3.8 27B Uncensored", { exact: true })).toBeVisible();
  await expect(table.locator('[title*="gguf" i], [title*="Q4_K_M"]')).toHaveCount(0);

  // Cost is never blank: Local, Unavailable or an amount.
  const rows = table.getByRole("row");
  for (let index = 1; index < (await rows.count()); index += 1) {
    const cost = (await rows.nth(index).getByRole("cell").last().innerText()).trim();
    expect(cost, `row ${index}`).toMatch(/^(Local|Unavailable|\$[\d.]+)/);
  }
  await expect(table).not.toContainText("$0.00");
  await expect(page.getByText("Local: no provider charge.")).toBeVisible();

  // The legend uses the names Home and Try use.
  await expect(page.getByText("Make a picture").first()).toBeVisible();
  await expect(page.getByText("Choose an option").first()).toBeVisible();
  await expect(page.getByText("Decisions", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Embeddings", { exact: true })).toHaveCount(0);
});

const manySources = [
  { id: "local-network", label: "Local network · no key", kind: "local-network", requests: 60, disabled: false, created_at: null },
  { id: "app:1", label: "tim.nunamaker@gmail.com personal access with a rather long name", kind: "app", requests: 90, disabled: false, created_at: 1 },
  ...Array.from({ length: 20 }, (_, index) => ({
    id: `app:tmp${index}`,
    label: index < 3 ? "Temporary empathy Jev triage" : `copy-triage-17911237${index}`,
    kind: "app",
    requests: index < 3 ? 6 : 1,
    disabled: true,
    created_at: 1000 + index,
  })),
  ...Array.from({ length: 6 }, (_, index) => ({
    id: `app:live${index}`,
    label: `Household app ${index}`,
    kind: "app",
    requests: 20 - index,
    disabled: false,
    created_at: 100 + index,
  })),
];

test("usage Source list stays on screen, scrolls inside itself and ranks apps", async ({ page }) => {
  await prepareUsageViewport(page, "dark", 390);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: { ...phoneSummary, sources: manySources } }),
  );
  await page.goto("/ui/usage/");
  await expect(page.getByText("Top models by requests")).toBeVisible();
  await page.getByRole("combobox", { name: "Source" }).click();
  const list = page.getByRole("listbox");
  await expect(list).toBeVisible();
  const box = await list.boundingBox();
  expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);
  expect(box?.y ?? -1).toBeGreaterThanOrEqual(0);
  expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(844);
  // It has a maximum height and scrolls inside itself.
  const scroll = await list.evaluate((node) => {
    const holder = node.querySelector("[data-radix-select-viewport]") as HTMLElement;
    return { client: holder.clientHeight, scroll: holder.scrollHeight };
  });
  expect(scroll.scroll).toBeGreaterThan(scroll.client);
  await expectNoHorizontalOverflow(page);

  const labels = await page.getByRole("option").allTextContents();
  expect(labels.slice(0, 4)).toEqual([
    "All sources",
    "tim.nunamaker@gmail.com personal access with a rather long name",
    "Local network · no key",
    "Household app 0",
  ]);
  // Same-named apps are numbered, and turned-off apps come after live ones.
  expect(labels.filter((label) => label.startsWith("Temporary empathy"))).toEqual([
    "Temporary empathy Jev triage (1)",
    "Temporary empathy Jev triage (2)",
    "Temporary empathy Jev triage (3)",
  ]);
  expect(labels.indexOf("Household app 5")).toBeLessThan(
    labels.indexOf("Temporary empathy Jev triage (1)"),
  );
  await expect(page.getByText("Turned off or no requests")).toBeVisible();
});

test("usage Source list does not push the page wider on a phone", async ({ page }) => {
  await prepareUsageViewport(page, "dark", 390);
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({ json: { ...phoneSummary, sources: manySources } }),
  );
  await page.goto("/ui/usage/");
  await page.getByRole("combobox", { name: "Source" }).click();
  await page.keyboard.press("End");
  await expectNoHorizontalOverflow(page);
  await page.keyboard.press("Escape");
  await expectNoHorizontalOverflow(page);
});

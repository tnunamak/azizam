import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const serviceRegistry = {
  schema_version: 1,
  services: {
    chat: {
      id: "chat",
      name: "Local chat engine",
      kind: "llama",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["chat"],
      role: "primary",
      config: {
        host: "127.0.0.1",
        port: 18081,
        base_path: "/v1",
        protocol: "openai-compatible",
        health_endpoint: "/health",
        models: { "legacy-chat": { upstream_model: "legacy-upstream" } },
      },
    },
    image: {
      id: "image",
      name: "Local image engine",
      kind: "comfy",
      source: "ui",
      enabled: false,
      editable: true,
      capabilities: ["image", "video"],
      role: "optional",
      config: { base_url: "http://127.0.0.1:18082" },
    },
    managed: {
      id: "managed",
      name: "Gateway substrate",
      kind: "litellm",
      source: "builtin",
      enabled: true,
      editable: false,
      capabilities: [],
      role: "required",
      config: {
        base_url: "http://litellm:4000",
        protocol: "openai-compatible",
        note: "Managed by the gateway.",
      },
    },
  },
};

const status = {
  current_health: {
    state: "ok",
    tone: "ok",
    label: "Ready",
    detail: "Configured engines are reachable.",
    observation_available: true,
    services: [
      {
        service_id: "chat",
        name: "Local chat engine",
        configured: true,
        enabled: true,
        role: "primary",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
      {
        service_id: "managed",
        name: "Gateway substrate",
        configured: true,
        enabled: true,
        role: "required",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
      {
        service_id: "image",
        name: "Local image engine",
        configured: true,
        enabled: false,
        role: "optional",
        routable: null,
        observation: "disabled",
        tone: "neutral",
        last_error: null,
      },
    ],
  },
  backends: [{ service_id: "chat", live_status: "up", latency_ms: 42 }],
};

async function mockEngineApis(page: Page) {
  await page.route("**/admin/api/status", (route) =>
    route.fulfill({ json: { status } }),
  );
  await page.route("**/admin/api/operator/service-presets", (route) =>
    route.fulfill({ json: { presets: [] } }),
  );
  await page.route("**/admin/api/operator/service-registry", (route) =>
    route.fulfill({
      json: { service_registry: serviceRegistry },
      headers: { etag: "test-etag" },
    }),
  );
  await page.route("**/admin/api/operator/services/test", (route: Route) =>
    route.fulfill({
      json: {
        probe: {
          ok: true,
          status: 200,
          error: "",
          url: "http://127.0.0.1:18081/v1/health",
        },
      },
    }),
  );
  await page.route("**/admin/api/operator/services/detect-models", (route) =>
    route.fulfill({
      json: { detected: { ok: true, models: ["demo-chat"], error: "" } },
    }),
  );
}

async function setTheme(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 390,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}

async function expectNoSeriousAxeOrOverflow(page: Page) {
  const violations = (
    await new AxeBuilder({ page }).analyze()
  ).violations.filter((violation) =>
    ["serious", "critical"].includes(violation.impact ?? ""),
  );
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}

async function expectMinBox(target: Locator, width: number, height: number) {
  const box = await target.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(width);
  expect(box?.height).toBeGreaterThanOrEqual(height);
}

async function expectCompactBox(
  target: Locator,
  width: number,
  height: number,
) {
  const box = await target.boundingBox();
  expect(Math.round(box?.width ?? 0)).toBe(width);
  expect(Math.round(box?.height ?? 0)).toBe(height);
}

async function expectCompactHeight(target: Locator, height: number) {
  const box = await target.boundingBox();
  expect(Math.round(box?.height ?? 0)).toBe(height);
}

async function expectNoOverlap(targets: Locator[]) {
  const boxes = [];
  for (const target of targets) {
    const box = await target.boundingBox();
    if (!box) throw new Error("Expected target to render");
    boxes.push(box);
  }
  for (let index = 0; index < boxes.length; index += 1) {
    for (let next = index + 1; next < boxes.length; next += 1) {
      const first = boxes[index];
      const second = boxes[next];
      const overlaps =
        first.x < second.x + second.width &&
        first.x + first.width > second.x &&
        first.y < second.y + second.height &&
        first.y + first.height > second.y;
      expect(overlaps).toBe(false);
    }
  }
}

function engineCard(page: Page, name: string) {
  return page.locator("article").filter({ hasText: name });
}

function engineSummaryButton(card: Locator, serviceName: string) {
  return card.getByRole("button", { name: new RegExp(serviceName) }).first();
}

test.describe("phone target primitives", () => {
  for (const theme of ["light", "dark"] as const) {
    for (const width of [1440, 390] as const) {
      test(`engines controls meet geometry in ${theme} at ${width}`, async ({
        page,
      }) => {
        await mockEngineApis(page);
        await setTheme(page, theme, width);
        await page.goto("/ui/engines/");
        await expect(
          page.getByRole("heading", { name: "Engines", level: 1 }),
        ).toBeVisible();

        const showNotInUse = page.getByRole("switch", {
          name: "Show not in use",
        });
        const chatCard = engineCard(page, "Local chat engine");
        const overflow = chatCard.getByRole("button", {
          name: "Actions for Local chat engine",
        });
        const addEngine = page
          .getByRole("button", { name: "Add engine" })
          .first();
        const summaryButton = engineSummaryButton(
          chatCard,
          "Local chat engine",
        );

        if (width === 390) {
          await expectMinBox(showNotInUse, 44, 44);
          await expectMinBox(addEngine, 44, 44);
          await expectMinBox(summaryButton, 44, 44);
          await expectMinBox(overflow, 44, 44);
          await expectNoOverlap([summaryButton, overflow]);

          const showBox = await showNotInUse.boundingBox();
          if (!showBox)
            throw new Error("Show not in use switch did not render");
          await page.mouse.click(showBox.x + 4, showBox.y + showBox.height / 2);
          await expect(page.getByText("Local image engine")).toBeVisible();
          await showNotInUse.focus();
          await page.keyboard.press("Space");
          await expect(page.getByText("Local image engine")).toHaveCount(0);
        } else {
          await expectMinBox(summaryButton, 44, 44);
        }

        await summaryButton.focus();
        await page.keyboard.press("Enter");
        const dialog = page.getByRole("dialog", { name: "Edit engine" });
        await expect(dialog).toBeVisible();
        const engineToggle = dialog.getByRole("switch", {
          name: "Toggle Local chat engine",
        });
        await expectCompactBox(
          engineToggle.locator('[data-slot="switch-track"]'),
          32,
          18,
        );
        const testButton = dialog.getByRole("button", { name: "Test" });
        const detectButton = dialog.getByRole("button", { name: "Detect" });
        if (width === 390) {
          await expectMinBox(engineToggle, 44, 44);
          await expectMinBox(testButton, 44, 44);
          await expectMinBox(detectButton, 44, 44);
        } else {
          await expectCompactHeight(testButton, 32);
          await expectCompactHeight(detectButton, 32);
        }
        await expectNoSeriousAxeOrOverflow(page);
        if (width === 390) {
          await expectMinBox(
            dialog.getByRole("button", { name: "Cancel" }),
            44,
            44,
          );
          await expectMinBox(
            dialog.getByRole("button", { name: "Save engine" }),
            44,
            44,
          );
          await expectMinBox(
            dialog.getByRole("button", { name: "Close" }),
            44,
            44,
          );
        }
        await page.keyboard.press("Escape");
        await expect(dialog).toHaveCount(0);
        await expect(summaryButton).toBeFocused();

        await expectNoSeriousAxeOrOverflow(page);
        await page.screenshot({
          path: `test-results/screens/phone-targets-engines-${theme}-${width}.png`,
          fullPage: true,
        });
      });
    }
  }

  for (const theme of ["light", "dark"] as const) {
    test(`design primitives keep phone tabs and modern switches tappable in ${theme}`, async ({
      page,
    }) => {
      await setTheme(page, theme, 390);
      await page.goto("/ui/design/");
      await expect(
        page.getByRole("heading", { name: "Design system", level: 1 }),
      ).toBeVisible();
      await expectMinBox(
        page.getByRole("button", { name: "Copy alias" }),
        44,
        44,
      );
      await expectMinBox(page.getByRole("button", { name: "Go to" }), 44, 44);
      await expectMinBox(
        page.getByRole("switch", { name: "Enable access" }),
        44,
        44,
      );
      const disabledSwitch = page.getByRole("switch", {
        name: "Disabled",
        exact: true,
      });
      await expectMinBox(disabledSwitch, 44, 44);
      await expect(disabledSwitch).toBeDisabled();
      const tabs = page.getByRole("tablist", { name: "Example detail" });
      await expectMinBox(tabs.getByRole("tab", { name: "Details" }), 44, 44);
      await expectMinBox(tabs.getByRole("tab", { name: "Empty" }), 44, 44);
      await tabs.getByRole("tab", { name: "Details" }).focus();
      await page.keyboard.press("ArrowRight");
      await expect(tabs.getByRole("tab", { name: "Empty" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await page.keyboard.press("ArrowRight");
      await expect(tabs.getByRole("tab", { name: "Error" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      const openDialog = page.getByRole("button", { name: "Open dialog" });
      await openDialog.focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("dialog", { name: "Create an app" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(openDialog).toBeFocused();
      await expectNoSeriousAxeOrOverflow(page);
      await page.screenshot({
        path: `test-results/screens/phone-targets-design-${theme}-390.png`,
        fullPage: true,
      });
    });
  }

  for (const theme of ["light", "dark"] as const) {
    test(`design desktop keeps compact modern switch geometry in ${theme}`, async ({
      page,
    }) => {
      await setTheme(page, theme, 1440);
      await page.goto("/ui/design/");
      await expect(
        page.getByRole("heading", { name: "Design system", level: 1 }),
      ).toBeVisible();
      await expectCompactBox(
        page.getByRole("switch", { name: "Enable access" }),
        32,
        18,
      );
      await expectCompactHeight(
        page.getByRole("button", { name: "Copy alias" }),
        32,
      );
      await expectNoSeriousAxeOrOverflow(page);
      await page.screenshot({
        path: `test-results/screens/phone-targets-design-${theme}-1440.png`,
        fullPage: true,
      });
    });
  }
});

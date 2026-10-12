import { test, expect } from "./fixtures/isolation";
import type { Page } from "@playwright/test";

// The shell health pill must say what its dot says (DESIGN.md, Status model
// rule 3). Phone width, as the owner saw it.

const member = {
  id: "member-pill",
  name: "Member",
  role: "member",
  email: "member@example.test",
};

function capability(
  key: string,
  title: string,
  state: "ready" | "unknown" | "down",
) {
  return {
    capability: key,
    capability_title: title,
    icon_key: key,
    intent: `${key}.run`,
    model: "default",
    available: state === "ready",
    status:
      state === "down"
        ? "unavailable"
        : state === "unknown"
          ? "unknown"
          : "ready",
    reason_code: state === "down" ? "unavailable" : state,
    ...(state === "ready"
      ? {
          availability: {
            evidence: {
              source: "health_check",
              observed_at_s: Math.floor(Date.now() / 1000),
            },
          },
        }
      : {}),
  };
}

async function installMember(
  page: Page,
  rows: ReturnType<typeof capability>[],
) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "fixture",
        apps: [],
        user: member,
      },
    }),
  );
  await page.route("**/me/api/home", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: member,
        aliases: [],
        receipts: [],
        capabilities: rows,
      },
    }),
  );
}

test("one Unknown capability reads 'Not checked yet' with the grey ring dot", async ({
  page,
}) => {
  await installMember(page, [
    capability("chat", "Chat", "ready"),
    capability("image", "Images", "unknown"),
  ]);
  await page.goto("/models/");
  const pill = page.getByRole("button", {
    name: "Gateway status: Not checked yet",
  });
  await expect(pill).toBeVisible();
  await expect(pill).toContainText("Not checked yet");
  await expect(page.getByRole("button", { name: /\bissues?$/ })).toHaveCount(0);
  const dot = pill.locator("span[aria-hidden]").first();
  await expect(dot).toHaveClass(/border-neutral/);
  await expect(dot).not.toHaveClass(
    /bg-danger-fill|bg-warning-fill|bg-success-fill/,
  );
});

test("one Down capability reads '1 issue' with the red dot", async ({
  page,
}) => {
  await installMember(page, [
    capability("chat", "Chat", "ready"),
    capability("image", "Images", "down"),
  ]);
  await page.goto("/models/");
  const pill = page.getByRole("button", { name: "Gateway status: 1 issue" });
  await expect(pill).toBeVisible();
  await expect(pill.locator("span[aria-hidden]").first()).toHaveClass(
    /bg-danger-fill/,
  );
});

test("tapping the pill lists the capability behind the count", async ({
  page,
}) => {
  await installMember(page, [
    capability("chat", "Chat", "ready"),
    capability("image", "Images", "unknown"),
  ]);
  await page.goto("/models/");
  await page
    .getByRole("button", { name: "Gateway status: Not checked yet" })
    .click();
  const popover = page.getByLabel("Gateway health");
  await expect(popover.getByRole("listitem")).toHaveText(["Images · Unknown"]);
  const action = popover.getByRole("link", { name: "See why" });
  await expect(action).toHaveAttribute("href", "/status/");
  // The popover fits the 390 px viewport.
  const box = await popover.boundingBox();
  expect(box && box.x >= 0 && box.x + box.width <= 390).toBe(true);
  await action.click();
  await expect(page).toHaveURL(/\/status\/?$/);
});

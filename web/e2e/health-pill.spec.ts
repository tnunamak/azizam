import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

// The shell health pill must say what its dot says (DESIGN.md, Status model
// rule 3). Phone width, as the owner saw it. Pill label, dot colour, popover
// rows and member health are asserted in app-shell.health.spec.tsx; the
// browser keeps what jsdom cannot see: the popover inside the 390 px viewport
// and the real navigation to Status.

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
  await expect
    .poll(async () => {
      const box = await popover.boundingBox();
      return !!box && box.x >= 0 && box.x + box.width <= 390;
    })
    .toBe(true);
  await action.click();
  await expect(page).toHaveURL(/\/status\/?$/);
});

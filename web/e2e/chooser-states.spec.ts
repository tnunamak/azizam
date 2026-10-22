// Every state of the sign-in page's chooser and person screens, at a phone and a desktop width (account
// switcher W4; DESIGN.md 4.5-4.9 and 10, mockup S4 to S10). The remembered list and the sign-in answers
// are answered here, so each state is the same every run. Each state is captured to test-results/w4-shots/
// and asserted on its key line, so a missing state fails the run rather than leaving an empty picture.
import type { Page } from "@playwright/test";
import { expect, test } from "@playwright/test";

test.use({ storageState: { cookies: [], origins: [] } });

const WIDTHS = [
  { name: "390", width: 390, height: 844 },
  { name: "1440", width: 1440, height: 900 },
];

const people = [
  { tile: "t_robin", name: "Robin Okafor", avatar: { variant: "v1", initial: "R" }, method: "password", provider: null, hint: null },
  { tile: "t_lea", name: "Léa Okafor", avatar: { variant: "v4", initial: "L" }, method: "password", provider: null, hint: null },
  { tile: "t_sam", name: "Sam Okafor", avatar: { variant: "v3", initial: "S" }, method: "sso", provider: "Family login", hint: null },
];

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function remember(page: Page, list = people) {
  await page.route("**/auth/api/remembered", (route) => route.fulfill(json({ remember: true, people: list })));
}

/** The browser's record of who signed out last, as a switch or a sign-out writes it. */
async function lastLeft(page: Page, tile: string) {
  await page.addInitScript((value) => sessionStorage.setItem("azizam.handoff.last", value), JSON.stringify({ tile }));
}

type State = {
  id: string;
  setup: (page: Page) => Promise<void>;
  /** Where the state is on screen once it is ready. */
  ready: (page: Page) => Promise<void>;
};

const states: State[] = [
  {
    id: "list",
    setup: (page) => remember(page).then(() => page.goto("/sign-in/")),
    ready: async (page) => {
      await expect(page.getByRole("heading", { level: 1, name: "Who’s signing in?" })).toBeVisible();
    },
  },
  {
    id: "list-after-switch",
    setup: async (page) => {
      await remember(page);
      await lastLeft(page, "t_robin");
      await page.goto("/sign-in/?after=switch");
    },
    ready: async (page) => {
      await expect(page.getByRole("status")).toHaveText("Robin Okafor is signed out. To come back, sign in again.");
    },
  },
  {
    id: "list-after-sign-out",
    setup: async (page) => {
      await remember(page);
      await lastLeft(page, "t_lea");
      await page.goto("/sign-in/?after=sign-out");
    },
    ready: async (page) => {
      await expect(page.getByRole("button", { name: "Forget Léa Okafor on this browser" })).toBeVisible();
    },
  },
  {
    id: "list-after-provider-sign-out",
    setup: async (page) => {
      await remember(page);
      await page.goto("/sign-in/?after=provider-sign-out");
    },
    ready: async (page) => {
      await expect(page.getByRole("status")).toContainText("You’re signed out of Family login.");
    },
  },
  {
    id: "forget",
    setup: async (page) => {
      await remember(page);
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Forget someone on this browser" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("heading", { level: 1, name: "Forget someone on this browser" })).toBeVisible();
    },
  },
  {
    id: "forget-after-one",
    setup: async (page) => {
      await remember(page);
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Forget someone on this browser" }).click();
      await page.getByRole("button", { name: "Forget Robin Okafor on this browser" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("status")).toContainText("Robin Okafor is forgotten on this browser.");
    },
  },
  {
    id: "password",
    setup: async (page) => {
      await remember(page);
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Léa Okafor", exact: true }).click();
    },
    ready: async (page) => {
      await expect(page.getByLabel("Password for Léa Okafor")).toBeFocused();
    },
  },
  {
    id: "password-wrong",
    setup: async (page) => {
      await remember(page);
      await page.route("**/auth/api/login", (route) =>
        route.fulfill(json({ error: { code: "invalid_login", message: "Wrong" } }, 401)),
      );
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Léa Okafor", exact: true }).click();
      await page.getByLabel("Password for Léa Okafor").fill("not the password");
      await page.getByRole("button", { name: "Sign in" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("alert")).toHaveText("That password isn’t right. Try again.");
    },
  },
  {
    id: "password-too-many",
    setup: async (page) => {
      await remember(page);
      await page.route("**/auth/api/login", (route) =>
        route.fulfill(json({ error: { code: "login_rate_limited", message: "Too many attempts. Try again in 15 minutes." } }, 429)),
      );
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Léa Okafor", exact: true }).click();
      await page.getByLabel("Password for Léa Okafor").fill("not the password");
      await page.getByRole("button", { name: "Sign in" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("alert")).toHaveText("Too many tries. Wait 15 minutes, then try again.");
    },
  },
  {
    id: "password-no-answer",
    setup: async (page) => {
      await remember(page);
      await page.route("**/auth/api/login", (route) => route.abort());
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Léa Okafor", exact: true }).click();
      await page.getByLabel("Password for Léa Okafor").fill("not the password");
      await page.getByRole("button", { name: "Sign in" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("alert")).toHaveText("Couldn’t reach the gateway. Try again in a moment.");
    },
  },
  {
    id: "single-sign-on-opening",
    setup: async (page) => {
      await remember(page);
      // The provider's page never answers here, so the opening state stays on screen.
      await page.route(/\/login\?tile=/, () => {});
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Sam Okafor" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("status")).toHaveText("Opening Family login");
    },
  },
  {
    id: "password-gone",
    setup: async (page) => {
      await remember(page);
      await page.route("**/auth/api/login", (route) =>
        route.fulfill(json({ error: { code: "tile_unknown", message: "That name is no longer on this browser." } }, 404)),
      );
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Léa Okafor", exact: true }).click();
      await page.getByLabel("Password for Léa Okafor").fill("not the password");
      await page.getByRole("button", { name: "Sign in" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("heading", { level: 1, name: "Léa Okafor can’t sign in from this list" })).toBeVisible();
    },
  },
  {
    id: "form",
    setup: async (page) => {
      await remember(page, []);
      await page.goto("/sign-in/");
    },
    ready: async (page) => {
      await expect(page.getByRole("checkbox", { name: "Show my name on this browser next time" })).toBeChecked();
    },
  },
  {
    id: "form-from-list",
    setup: async (page) => {
      await remember(page);
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Someone else" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("button", { name: "Back to the list" })).toBeVisible();
    },
  },
  {
    id: "form-remember-nobody",
    setup: async (page) => {
      await page.route("**/auth/api/remembered", (route) => route.fulfill(json({ remember: false, people: [] })));
      await page.goto("/sign-in/");
    },
    ready: async (page) => {
      await expect(page.getByLabel("Email")).toBeVisible();
      await expect(page.getByRole("checkbox")).toHaveCount(0);
    },
  },
  {
    id: "form-everyone-forgotten",
    setup: async (page) => {
      await page.route("**/auth/api/remembered", (route) => route.fulfill(json({ remember: true, people: people.slice(0, 2) })));
      await page.route("**/auth/api/remembered/*", (route) => route.fulfill(json({ ok: true })));
      await page.goto("/sign-in/");
      await page.getByRole("button", { name: "Forget someone on this browser" }).click();
      await page.getByRole("button", { name: "Forget Robin Okafor on this browser" }).click();
      await page.getByRole("button", { name: "Forget Léa Okafor on this browser" }).click();
    },
    ready: async (page) => {
      await expect(page.getByRole("status")).toHaveText("Everyone is forgotten on this browser.");
    },
  },
  {
    id: "list-unavailable",
    setup: async (page) => {
      await page.route("**/auth/api/remembered", (route) => route.abort());
      await page.goto("/sign-in/");
    },
    ready: async (page) => {
      await expect(page.getByRole("alert")).toHaveText("Couldn’t reach the gateway.");
    },
  },
  {
    id: "reset-password",
    setup: async (page) => {
      await page.goto("/sign-in/#reset=capture-token");
    },
    ready: async (page) => {
      await expect(page.getByRole("heading", { level: 1, name: "Choose a new password" })).toBeVisible();
    },
  },
];

for (const viewport of WIDTHS) {
  test(`every chooser and sign-in state is captured and shows its key line at ${viewport.name} px`, async ({ context }) => {
    for (const state of states) {
      // A fresh page per state: the init script that sets the last-left record must not carry over.
      const page = await context.newPage();
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await state.setup(page);
      await state.ready(page);
      await page.screenshot({
        path: test.info().outputPath(`w4-shots/${viewport.name}-${state.id}.png`),
        fullPage: true,
      });
      await page.close();
    }
  });
}

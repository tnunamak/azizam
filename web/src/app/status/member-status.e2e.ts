import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const screenshotRoot = "/home/tnunamak/.tmp/gw-product/gate/v5/fixtures";

type MemberStatusState =
  | "ready"
  | "idle"
  | "starting"
  | "busy"
  | "unknown"
  | "unavailable";

const allStates: MemberStatusState[] = [
  "ready",
  "idle",
  "starting",
  "busy",
  "unknown",
  "unavailable",
];

const capabilityNames = {
  ready: "Chat",
  idle: "Images",
  starting: "Video",
  busy: "Speech",
  unknown: "Transcription",
  unavailable: "Embeddings",
} as const;

const capabilityKinds = {
  ready: "chat",
  idle: "image",
  starting: "video",
  busy: "audio",
  unknown: "transcribe",
  unavailable: "embed",
} as const;

async function installMemberSession(page: Page) {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        apps: [],
        user: {
          id: "member-status",
          name: "Member",
          role: "member",
          email: "member-status@example.test",
        },
      },
    }),
  );
}

async function installStatusSnapshot(
  page: Page,
  state: "seeded" | "empty" | "error" | "401" | "loading",
) {
  await installMemberSession(page);
  let failed = state === "error";
  await page.route("**/me/api/status", (route) => {
    if (state === "loading") return;
    if (state === "401") {
      return route.fulfill({
        status: 401,
        json: { error: { code: "unauthorized", message: "Session expired" } },
      });
    }
    if (failed) {
      return route.fulfill({
        status: 503,
        json: {
          error: { code: "status_unavailable", message: "Unavailable" },
        },
      });
    }
    const capabilities =
      state === "empty"
        ? []
        : allStates.map((availability, index) => ({
            capability: capabilityKinds[availability],
            capability_title: capabilityNames[availability],
            icon_key:
              availability === "busy"
                ? "speech"
                : capabilityKinds[availability],
            title: `${capabilityNames[availability]} test model`,
            recommended_model: `${capabilityNames[availability]} test model`,
            use_names: [`${availability}-alias`, `${availability}-model`],
            summary: `Use ${capabilityNames[availability].toLowerCase()} with your account.`,
            intent: `${capabilityKinds[availability]}.test`,
            model: `${availability}-model`,
            workflow: null,
            status: availability,
            available: availability === "ready" || availability === "idle",
            measured: availability !== "unknown",
            observed_at_s:
              availability === "unknown" ? null : 1790870400 + index,
            reason: `${availability} evidence is visible to the member.`,
          }));
    return route.fulfill({
      json: {
        scope: "member",
        status: {
          state:
            state === "empty"
              ? "unavailable"
              : ("unavailable" satisfies MemberStatusState),
          reason:
            state === "empty"
              ? "No service has been measured as ready yet."
              : "Embeddings are unavailable: the engine is offline.",
        },
        capabilities,
        receipts:
          state === "empty"
            ? []
            : [
                {
                  request_id: "failed-request",
                  status: "failed",
                  intent: "chat.test",
                  created_at: 1790870400,
                  href: "/usage/?receipt=failed-request",
                },
              ],
        help: "Ask the owner if an allowed capability is unavailable.",
      },
    });
  });
  return {
    recover() {
      failed = false;
    },
  };
}

async function assertPageQuality(
  page: Page,
  label: string,
  ownerCalls: string[],
) {
  await mkdir(screenshotRoot, { recursive: true });
  await expect(page.locator('[aria-busy="true"]:visible')).toHaveCount(0);
  await page.evaluate(() => document.fonts.ready);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const serious = (await new AxeBuilder({ page }).analyze()).violations.filter(
    (violation) => ["serious", "critical"].includes(violation.impact ?? ""),
  );
  expect(serious).toEqual([]);
  expect(ownerCalls).toEqual([]);
  await page.screenshot({
    path: path.join(screenshotRoot, `${label}.png`),
    fullPage: true,
  });
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`member Status ${state} ${theme} ${width}`, async ({ page }) => {
        const ownerCalls: string[] = [];
        page.on("request", (request) => {
          if (new URL(request.url()).pathname.startsWith("/admin/api/")) {
            ownerCalls.push(request.url());
          }
        });
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        const status = await installStatusSnapshot(page, state);

        await page.goto("/ui/status/");
        await expect(page).toHaveURL(/\/ui\/status\/$/);
        await expect(
          page.getByRole("heading", { name: "Status", level: 1 }),
        ).toBeVisible();

        if (state === "seeded") {
          for (const availability of allStates) {
            const capability = page.locator("li").filter({
              has: page.getByRole("heading", {
                name: capabilityNames[availability],
                exact: true,
              }),
            });
            await expect(capability).toBeVisible();
            const labels = {
              ready: "Ready",
              idle: "Ready to start",
              starting: "Starting",
              busy: "Busy",
              unknown: "Unknown",
              unavailable: "Unavailable",
            };
            await expect(
              capability.getByText(labels[availability], { exact: true }),
            ).toBeVisible();
            if (availability === "unknown") {
              await expect(capability.locator("time")).toHaveCount(0);
            } else {
              await expect(capability.locator("time")).toHaveAttribute(
                "datetime",
                new Date(
                  (1790870400 + allStates.indexOf(availability)) * 1000,
                ).toISOString(),
              );
            }
            await expect(
              capability.getByText(
                `${availability} evidence is visible to the member.`,
                { exact: true },
              ),
            ).toBeVisible();
          }
          await expect(
            page.getByRole("link", { name: "View receipt failed-request" }),
          ).toHaveAttribute("href", "/ui/usage/?receipt=failed-request");
          await expect(page.getByText("Not measured yet")).toBeHidden();
          await expect(
            page.getByText("Ask the owner", { exact: true }),
          ).toHaveCount(0);
          await expect(
            page.getByText(
              "Embeddings are unavailable: the engine is offline.",
              { exact: true },
            ),
          ).toBeVisible();
          const unknown = page.locator("li").filter({
            has: page.getByRole("heading", {
              name: "Transcription",
              exact: true,
            }),
          });
          await unknown.locator("details").locator("summary").click();
          await unknown.getByText("Names you can use", { exact: true }).click();
          await expect(
            unknown.getByText("unknown-alias, unknown-model", { exact: true }),
          ).toBeVisible();
          await expect(unknown.getByText("Not measured yet")).toBeVisible();
          await unknown.getByText("Names you can use", { exact: true }).click();
        } else if (state === "empty") {
          await expect(
            page.getByText("No models are available to your account yet", {
              exact: true,
            }),
          ).toBeVisible();
          await expect(
            page.getByText(/No receipts for your account yet/),
          ).toBeVisible();
        } else {
          await expect(
            page.getByRole("button", { name: "Try again" }),
          ).toBeVisible();
          await assertPageQuality(
            page,
            `member-status-${state}-${theme}-${width}`,
            ownerCalls,
          );
          status.recover();
          await page.getByRole("button", { name: "Try again" }).click();
          await expect(
            page.getByRole("heading", { name: "Chat", exact: true }),
          ).toBeVisible();
          expect(ownerCalls).toEqual([]);
          return;
        }
        await expect(
          page.getByText(/GPU|disk|VRAM|Engines|Providers/, { exact: true }),
        ).toHaveCount(0);
        await assertPageQuality(
          page,
          `member-status-${state}-${theme}-${width}`,
          ownerCalls,
        );
      });
    }
  }
}

test("member Status offers sign-in after an expired session without retrying", async ({
  page,
}) => {
  const ownerCalls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/admin/api/")) {
      ownerCalls.push(request.url());
    }
  });
  let calls = 0;
  await installStatusSnapshot(page, "401");
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/me/api/status") calls += 1;
  });

  await page.goto("/ui/status/");
  await expect(
    page.getByText("Sign in to see your status", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in" })).toHaveAttribute(
    "href",
    "/ui/sign-in/?return_to=%2Fui%2Fstatus%2F",
  );
  expect(calls).toBe(1);
  expect(ownerCalls).toEqual([]);
});

test("member Status exposes a loading skeleton while the scoped request is pending", async ({
  page,
}) => {
  await installStatusSnapshot(page, "loading");
  await page.goto("/ui/status/");
  await expect(
    page.getByRole("status", { name: "Loading your model status" }),
  ).toHaveAttribute("aria-busy", "true");
});

test("accepted member can fetch real scoped Status and keep the Status URL", async ({
  browser,
  baseURL,
  request,
}) => {
  const memberContext = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
  });
  const memberPage = await memberContext.newPage();
  const ownerCalls: string[] = [];
  memberPage.on("request", (pageRequest) => {
    if (new URL(pageRequest.url()).pathname.startsWith("/admin/api/")) {
      ownerCalls.push(pageRequest.url());
    }
  });

  try {
    const session = await (await request.get("/admin/api/session")).json();
    const headers = {
      "x-ai-gateway-csrf": session.actor.csrf_token,
      "x-ai-gateway-admin-action": "1",
    };
    const profiles = await (await request.get("/admin/api/profiles")).json();
    const local = profiles.profiles.find(
      (profile: { name: string }) => profile.name === "Local chat",
    );
    expect(local).toBeTruthy();
    const invite = await request.post("/admin/api/invites", {
      headers,
      data: {
        profile_id: local.id,
        email: "status-member@example.test",
        expires_in: 86400,
      },
    });
    expect(invite.ok()).toBe(true);
    const created = await invite.json();

    await memberPage.goto(created.url);
    await memberPage.getByLabel("Your name").fill("Status member");
    await memberPage
      .getByLabel("Password", { exact: true })
      .fill("Status member password 2026");
    const acceptedResponse = memberPage.waitForResponse((response) =>
      response.url().endsWith("/auth/api/invite/accept"),
    );
    await memberPage
      .getByRole("button", { name: "Create account", exact: true })
      .click();
    const accepted = await (await acceptedResponse).json();
    expect(accepted.credential.api_key).toBeTruthy();
    await memberPage.getByRole("button", { name: "I’ve saved it" }).click();
    await expect(memberPage).toHaveURL(/\/ui\/$/);

    const status = await memberContext.request.get("/me/api/status");
    expect(status.ok()).toBe(true);
    const statusBody = await status.json();
    expect(statusBody.scope).toBe("member");
    const chat = statusBody.capabilities.find(
      (capability: { capability: string }) => capability.capability === "chat",
    );
    expect(chat).toMatchObject({
      capability_title: "Chat",
      use_names: expect.arrayContaining(["demo-chat"]),
      recommended_model: expect.any(String),
    });
    expect(chat.recommended_model.trim()).not.toBe("");

    ownerCalls.length = 0;
    await memberPage.goto("/ui/status/");
    await expect(memberPage).toHaveURL(/\/ui\/status\/$/);
    await expect(
      memberPage.getByRole("heading", { name: "Chat", exact: true }).first(),
    ).toBeVisible();
    const chatRow = memberPage.locator("li").filter({
      has: memberPage.getByRole("heading", { name: "Chat", exact: true }),
    });
    await expect(
      chatRow.getByText(chat.recommended_model, { exact: true }),
    ).toBeVisible();
    await chatRow.locator("details").locator("summary").click();
    await expect(chatRow.locator("details")).toContainText("demo-chat");
    await expect(
      memberPage.getByRole("link", { name: "Try a request" }),
    ).toBeVisible();
    expect(ownerCalls).toEqual([]);
  } finally {
    await memberContext.close();
  }
});

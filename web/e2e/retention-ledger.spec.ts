import { expect, test } from "./fixtures/isolation";

const ledger = {
  ledger: {
    kinds: [
      { id: "library_prompt", purpose: "Find it again", readers: "Only you" },
    ],
    destinations: [
      {
        id: "local-server",
        name: "Local test engine",
        kind: "engine",
        address: "127.0.0.1:8188",
        reach: "on this computer",
        facts: [
          {
            id: "history",
            holds: "submitted graphs",
            where: "engine history",
            how_long: "until restart",
            default: "on",
            reach: "anyone who can reach the port",
            control: "restart it",
            evidence_url: "https://example.com/history",
            checked: "2026-01-01",
            confidence: "documented",
            stale: true,
            source: "set by the owner",
            revision: "abc",
          },
        ],
      },
    ],
    never_save_text:
      "Azizam will not keep the request. The engine may keep its own history.",
  },
};

for (const width of [1280, 390]) {
  test(`ledger can be opened from settings and an engine at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const ledgerCalls: string[] = [];
    await page.route("**/admin/api/**/retention/ledger", async (route) => {
      ledgerCalls.push(route.request().url());
      await route.fulfill({ json: ledger });
    });
    await page.route(
      "**/admin/api/settings/retention/ledger",
      async (route) => {
        ledgerCalls.push(route.request().url());
        await route.fulfill({ json: ledger });
      },
    );
    await page.goto("/settings/");
    const settingsLedger = page.getByRole("region", {
      name: "Who could see a request",
    });
    await expect(
      settingsLedger.getByRole("button", { name: "View ledger" }),
    ).toBeVisible();
    expect(ledgerCalls).toHaveLength(0);
    await settingsLedger.getByRole("button", { name: "View ledger" }).click();
    await expect(
      settingsLedger.getByText(
        "This was last checked on 2026-01-01. Set by the owner.",
      ),
    ).toBeVisible();
    await expect(
      settingsLedger.getByText(ledger.ledger.never_save_text),
    ).toBeVisible();
    await page.goto("/engines/?engine=capture-engine");
    const engineLedger = page.getByRole("region", {
      name: "Who could see a request",
    });
    await expect(
      engineLedger.getByRole("button", { name: "View ledger" }),
    ).toBeVisible();
    await engineLedger.getByRole("button", { name: "View ledger" }).click();
    await expect(engineLedger.getByText("submitted graphs")).toBeVisible();
    expect(ledgerCalls).toHaveLength(2);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

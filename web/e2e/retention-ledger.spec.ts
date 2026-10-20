import { expect, test } from "./fixtures/isolation";

for (const width of [1280, 390]) {
  test(`ledger can be opened from settings and an engine at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const ledgerCalls: string[] = [];
    const ledgerStatuses: number[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/retention/ledger")) ledgerCalls.push(request.url());
    });
    page.on("response", (response) => {
      if (response.url().includes("/retention/ledger")) ledgerStatuses.push(response.status());
    });
    await page.goto("/settings/");
    const settingsLedger = page.getByRole("region", {
      name: "Who could see a request",
    });
    await expect(
      settingsLedger.getByRole("button", { name: "View ledger" }),
    ).toBeVisible();
    expect(ledgerCalls).toHaveLength(0);
    await settingsLedger.getByRole("button", { name: "View ledger" }).click();
    await expect.poll(() => ledgerStatuses).toEqual([200]);
    await expect(settingsLedger.getByText("Gateway", { exact: true })).toBeVisible();
    await expect(settingsLedger.getByText("Never save stops Azizam", { exact: false })).toBeVisible();
    await page.goto("/engines/?engine=capture-engine");
    const engineLedger = page.getByRole("region", {
      name: "Who could see a request",
    });
    await expect(
      engineLedger.getByRole("button", { name: "View ledger" }),
    ).toBeVisible();
    await engineLedger.getByRole("button", { name: "View ledger" }).click();
    await expect(engineLedger.getByText("Local test engine", { exact: true })).toBeVisible();
    expect(ledgerCalls).toHaveLength(2);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

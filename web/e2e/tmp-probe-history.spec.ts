import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

// TEMPORARY probe: what the History detail's receipt lookup returns for a Try chat request.
test("probe receipt for a chat request @global-state", { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
  await page.goto("/try/?capability=chat");
  await page.getByRole("textbox", { name: "Prompt" }).fill("probe " + Date.now());
  await page.getByRole("button", { name: "Run" }).click();
  await expect(page.getByRole("heading", { name: "Chat result" })).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(3000);
  const list = await page.evaluate(async () => (await fetch("/gateway/v1/requests?limit=3", { credentials: "same-origin" })).json());
  const first = list.requests?.[0];
  console.log("PROBE_LIST_FIRST", JSON.stringify(first));
  if (first?.request_id) {
    const receipt = await page.evaluate(async (id) => {
      const r = await fetch(`/me/api/receipts/${encodeURIComponent(id)}`, { credentials: "same-origin" });
      return { status: r.status, body: await r.text() };
    }, first.request_id);
    console.log("PROBE_RECEIPT", receipt.status, receipt.body.slice(0, 1500));
  }
});

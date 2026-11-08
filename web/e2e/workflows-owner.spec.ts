import { expect, test } from "./fixtures/isolation";

// The owner view on the Workflows page, against the fixture gateway itself (no
// stubbed routes): list, open one workflow, toggle publication with the ETag.
// What the view says in each state is tested in src/app/workflows/owner-view.spec.tsx.

test("the owner lists workflows, opens one and toggles publication with the ETag", async ({
  page,
}) => {
  await page.goto("/workflows/");
  const list = page.getByRole("list", { name: "Owner workflows" });
  await expect(list).toBeVisible();
  await list.getByRole("button", { name: /^Manage / }).first().click();

  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("textbox", { name: "Workflow JSON" })).toBeVisible();
  const toggle = sheet.getByRole("button", { name: /^(Publish|Unpublish)$/ });
  const before = (await toggle.textContent()) ?? "";

  const write = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/publish"),
  );
  await toggle.click();
  const request = await write;
  expect(request.headers()["if-match"]).toMatch(/^"os-\d+"$/);
  const after = before === "Publish" ? "Unpublish" : "Publish";
  await expect(sheet.getByRole("button", { name: after, exact: true })).toBeVisible();

  // Put it back, with the ETag of the detail that was read after the first write.
  await sheet.getByRole("button", { name: before, exact: true }).click();
  await expect(sheet.getByRole("button", { name: before, exact: true })).toBeVisible();
});

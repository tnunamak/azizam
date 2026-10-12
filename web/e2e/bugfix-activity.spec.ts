import { test, expect } from "@playwright/test";

for (const status of ["error", "failed"]) {
  test(`decision ${status} shows failure and HTTP status in Activity`, async ({
    page,
  }) => {
    await page.route("**/admin/api/activity/requests?*", (route) =>
      route.fulfill({
        json: {
          records: [
            {
              receipt: {
                request_id: "req_decision_failure",
                intent: "decision.systemone",
                status,
                requested_model: "decide",
                metadata: { status_code: 529 },
              },
              person_label: "Owner",
              app_label: "Try",
              created_at: 1700000000,
            },
          ],
          next_cursor: null,
          people: [],
        },
      }),
    );
    await page.goto("/activity/");
    await page.getByRole("tab", { name: "Requests", exact: true }).click();
    const row = page.getByRole("button", { name: /Request details:/ });
    await expect(row).toContainText("Failed");
    await expect(
      page.getByText("Outcome not recorded", { exact: true }),
    ).toHaveCount(0);
    await row.click();
    await expect(page.getByRole("dialog")).toContainText("Failed");
    await expect(page.getByRole("dialog")).toContainText("Owner");
    await page.getByText("Technical details", { exact: true }).click();
    await expect(page.getByLabel("Technical request details")).toContainText(
      '"status_code": 529',
    );
  });
}

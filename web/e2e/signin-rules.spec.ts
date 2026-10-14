import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, fixtureStateDir, GLOBAL_STATE_TAG, ownerApi, test } from "./fixtures/isolation";

// One real journey through Settings > Sign-in on the fixture gateway: the owner adds a
// rule, sees what it changes, saves it, and an administrator sees the same page read-only.
// Every display state belongs to the component specs (web/src/app/settings/sign-in).
for (const viewport of [
  { width: 1280, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`the owner adds a sign-in rule and an administrator sees it read-only at ${viewport.width} px`, {
    tag: GLOBAL_STATE_TAG,
  }, async ({ page, request, baseURL, isolated }) => {
    await page.setViewportSize(viewport);
    const owner = await ownerApi(request);
    // A provider that is on: the page is the live one, not the empty first run.
    const connection = await owner.get("/admin/api/oidc-config");
    await owner.send("PUT", "/admin/api/oidc-config", {
      enabled: true,
      issuer: "https://id.example.test/realms/e2e",
      client_id: "azizam-e2e",
      client_secret: "e2e-secret",
      redirect_uri: `${new URL(baseURL!).origin}/admin/oidc/callback`,
      scopes: ["openid", "email", "profile"],
    }, connection.revision as string);
    const mode = await owner.get("/admin/api/operator/sign-in");
    await owner.send("PUT", "/admin/api/operator/sign-in", { mode: "both" }, mode.revision as string);

    // An administrator by the owner's hand (a hand-made grant is what the page must not touch).
    const helper = await isolated.member({ name: "Helper" });
    const db = path.join(fixtureStateDir(Number(new URL(baseURL!).port)), "admin.db");
    execFileSync("sqlite3", [
      db,
      "INSERT INTO admin_role_assignments(role, subject_kind, subject_id, granted_at, granted_by, provenance) " +
        `VALUES ('admin', 'user', '${helper.userId}', ${Math.floor(Date.now() / 1000)}, 'e2e', 'manual')`,
    ]);

    // The owner adds a rule that lets one group in, and reads what it changes.
    await page.goto("/settings/");
    const section = page.locator("#sign-in-section");
    await expect(section.getByRole("heading", { name: "Sign-in" })).toBeVisible();
    await expect(section).toContainText("id.example.test");           // named by its address: no Name was typed
    await expect(section).not.toContainText(/\b(a|own) your provider/);
    await section.getByRole("button", { name: "Add a rule" }).click();
    await page.getByPlaceholder(/^Add a group/).fill("/Family");
    await page.getByRole("button", { name: "Add", exact: true }).click();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    const rule = section.getByRole("group", { name: "Rule 1" });
    await rule.getByRole("checkbox", { name: "Let in" }).check();
    const changes = section.getByRole("region", { name: "What changes" });
    await expect(changes).toBeVisible();
    await expect(changes).toContainText("No one who has signed in before is affected.");
    await expect(changes).toContainText("You stay the owner.");
    if (viewport.width < 640) await expect(section).toContainText("1 change");

    await section.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText(/^Saved\./).first()).toBeVisible();
    await page.reload();
    await expect(section.getByRole("group", { name: "Rule 1" })).toContainText("/Family");
    await expect(section.getByRole("button", { name: "Save changes" })).toBeDisabled();

    // The administrator sees the same rules, with nothing to change.
    await helper.page.setViewportSize(viewport);
    await helper.page.goto("/settings/");
    const seen = helper.page.locator("#sign-in-section");
    await expect(seen.getByText("Only the owner can change sign-in.")).toBeVisible();
    await expect(seen.getByRole("group", { name: "Rule 1" })).toContainText("/Family");
    await expect(seen.getByRole("button", { name: "Add a rule" })).toBeDisabled();
    await expect(seen.getByRole("button", { name: "Save changes" })).toHaveCount(0);
    await expect(seen.getByRole("switch").first()).toBeDisabled();
    expect(await helper.page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );

    // The hand-made grant leaves with the test; the fixture restores the sign-in settings.
    execFileSync("sqlite3", [
      db,
      `UPDATE admin_role_assignments SET revoked_at = ${Math.floor(Date.now() / 1000)} WHERE subject_id = '${helper.userId}'`,
    ]);
  });
}

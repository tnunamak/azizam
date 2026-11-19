import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

// The owner journey for an engine Azizam starts, against the fixture gateway's real routes: list the folders
// engines may run from (Settings), open Add engine, pick the workflow engine, see the exact command before saving,
// and save with the folder and port, not an address. It writes owner-only state (the folder list and the service
// registry), so it runs in the serial global-state lane and puts both back.

test.use({ contextOptions: { reducedMotion: "reduce" } });

test("add the workflow engine Azizam starts, from an allowed folder", { tag: GLOBAL_STATE_TAG }, async ({ page }) => {
  const root = mkdtempSync(path.join(process.env.UI_E2E_STATE_ROOT ?? os.tmpdir(), "starts-itself-"));
  // The folder an engine is installed in: main.py, and a Python in .venv that can run.
  const install = path.join(root, "studio");
  mkdirSync(path.join(install, ".venv", "bin"), { recursive: true });
  writeFileSync(path.join(install, "main.py"), "# stands in for an engine\n");
  writeFileSync(path.join(install, ".venv", "bin", "python"), "#!/bin/sh\n", { mode: 0o755 });

  const session = await (await page.request.get("/admin/api/session")).json();
  const ownerHeaders = { "x-azizam-csrf": session.actor.csrf_token, "x-azizam-admin-action": "1" };
  const before = await (await page.request.get("/admin/api/operator/engine-folders")).json();
  let serviceId: string | null = null;
  try {
    // 1. Engine folders: the whole computer is refused in plain words; the real folder is accepted.
    await page.goto("/settings/");
    const section = page.getByRole("region", { name: "Engine folders" });
    await section.getByRole("button", { name: "Edit Engine folders" }).click();
    await section.getByLabel("Add a folder").fill("/");
    await section.getByRole("button", { name: "Add folder" }).click();
    await expect(section.getByRole("alert")).toContainText("whole computer");
    await section.getByLabel("Add a folder").fill(root);
    await section.getByRole("button", { name: "Add folder" }).click();
    await expect(section.getByRole("list", { name: "Engine folders" })).toContainText(path.basename(root));

    // 2. Add engine: pick the kind that Azizam starts.
    await page.goto("/engines/");
    await page.getByRole("button", { name: "Add engine", exact: true }).first().click();
    const dialog = page.getByRole("dialog", { name: "Add engine" });
    await dialog.getByRole("button", { name: /Workflow image engine/ }).click();
    await expect(dialog.getByText("Step 2 of 3 · Start")).toBeVisible();
    await expect(dialog.getByLabel(/^Address/)).toHaveCount(0);
    // A free port is already filled in.
    const port = dialog.getByLabel(/^Port/);
    await expect(port).toHaveValue(/^\d{4,5}$/);
    const portValue = await port.inputValue();

    // A folder with no main.py is refused beside the Folder field, with no command shown.
    const folder = dialog.getByLabel(/^Folder/);
    await folder.fill(root);
    await expect(folder).toHaveAccessibleDescription(/main\.py/);
    await expect(dialog.getByTestId("process-preview")).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Continue" })).toBeDisabled();

    // The right folder shows the exact command, working folder and port.
    await folder.fill(install);
    const preview = dialog.getByTestId("process-preview");
    await expect(preview).toContainText(`${install}/.venv/bin/python`);
    await expect(preview).toContainText(`${install}/main.py`);
    await expect(preview).toContainText(`Working folder${install}`);
    await expect(preview).toContainText(`Port${portValue}`);
    await dialog.getByRole("button", { name: "Continue" }).click();

    // 3. Name, then save with the slots. The request has no address.
    await expect(dialog.getByText("Step 3 of 3 · Name")).toBeVisible();
    await expect(dialog.getByRole("textbox", { name: "Name", exact: true })).toHaveValue("studio");
    await expect(dialog.getByTestId("recap")).toContainText(`${install}/main.py`);
    const created = page.waitForResponse(
      (response) => response.request().method() === "POST" && response.url().endsWith("/admin/api/operator/services"),
    );
    await dialog.getByRole("button", { name: "Add engine", exact: true }).click();
    const answer = await created;
    expect(answer.status()).toBe(201);
    const sent = answer.request().postDataJSON();
    expect(sent.slots).toEqual({ folder: install, port: Number(portValue) });
    expect(sent.values).toEqual({});
    serviceId = (await answer.json()).service_id;
    await expect(page.getByRole("heading", { level: 1, name: "studio" })).toBeVisible();
  } finally {
    if (serviceId) {
      const registry = await page.request.get("/admin/api/operator/service-registry");
      const removed = await page.request.delete(`/admin/api/operator/services/${serviceId}`, {
        headers: { "x-azizam-csrf": ownerHeaders["x-azizam-csrf"], "if-match": registry.headers().etag },
      });
      expect(removed.ok()).toBe(true);
    }
    const now = await (await page.request.get("/admin/api/operator/engine-folders")).json();
    const restored = await page.request.put("/admin/api/operator/engine-folders", {
      headers: { ...ownerHeaders, "if-match": now.revision },
      data: { folders: before.folders },
    });
    expect(restored.ok()).toBe(true);
    rmSync(root, { recursive: true, force: true });
  }
});

import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

type Registry = {
  services: Record<string, {
    id: string;
    name: string;
    config: {
      headers: { id: string; name: string }[];
      [key: string]: unknown;
    };
    process: {
      env: Record<string, { id: string }>;
      start: (string | { id: string })[];
      [key: string]: unknown;
    };
    [key: string]: unknown;
  }>;
};

const entryId = (value: string | { id: string }) => typeof value === "string" ? "" : value.id;

for (const width of [1280, 390]) {
  test(`write-only header, environment and command secrets survive save and reload at ${width}px`, { tag: GLOBAL_STATE_TAG }, async ({ page, baseURL }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const name = `Secret entries ${width}`;
    const canaries = {
      header: `header-canary-${width}-do-not-read`,
      env: `env-canary-${width}-do-not-read`,
      argvKeep: `argv-retained-${width}-do-not-read`,
      argvReplace: `argv-canary-${width}-do-not-read`,
    };
    let serviceId: string | undefined;

    const sessionResponse = await page.request.get("/admin/api/session");
    expect(sessionResponse.ok()).toBe(true);
    const session = await sessionResponse.json();
    const writeHeaders = (etag?: string) => ({
      "x-azizam-csrf": session.actor.csrf_token,
      "x-azizam-admin-action": "1",
      "if-match": etag ?? "",
    });
    const readRegistry = async () => {
      const response = await page.request.get("/admin/api/operator/service-registry");
      expect(response.ok()).toBe(true);
      return {
        registry: (await response.json()).service_registry as Registry,
        etag: response.headers().etag,
      };
    };
    const readEngine = async () => {
      const response = await page.request.get(`/admin/api/engines/${serviceId}`);
      expect(response.ok()).toBe(true);
      const text = await response.text();
      for (const canary of Object.values(canaries)) expect(text).not.toContain(canary);
      return text;
    };

    try {
      const created = await page.request.post("/admin/api/operator/services", {
        headers: writeHeaders((await readRegistry()).etag),
        data: {
          definition: "openai-compatible",
          name,
          values: { address: `http://127.0.0.1:${Number(new URL(baseURL!).port) + 1}/secret-entry-${width}/v1` },
          disabled: true,
        },
      });
      expect(created.status()).toBe(201);
      const createdBody = await created.json() as { service_id?: unknown };
      if (typeof createdBody.service_id !== "string") throw new Error("service creation returned no service id");
      serviceId = createdBody.service_id;
      const id = createdBody.service_id;

      // Seed only public structure through the registry route. Secret bytes enter
      // the real secret store through the same explicit command route as the UI.
      const seed = await readRegistry();
      const record = seed.registry.services[id];
      record.config.headers = [];
      record.process = {
        driver: "command",
        start: ["runner"],
        env: {},
      };
      record.lifecycle = {
        driver: "command",
        driver_config: { start: ["runner"], stop: [] },
      };
      const seeded = await page.request.put("/admin/api/operator/service-registry", {
        headers: writeHeaders(seed.etag),
        data: seed.registry,
      });
      expect(seeded.ok()).toBe(true);

      const secretSeed = await page.request.post(`/admin/api/operator/services/${id}/secret-entries`, {
        headers: writeHeaders((await readRegistry()).etag),
        data: {
          commands: [
            { carrier: "config.headers", operation: "add", name: "X-Tenant", value: canaries.header },
            { carrier: "process.env", operation: "add", name: "TOKEN", value: canaries.env },
            {
              carrier: "process.start",
              operation: "set",
              items: [
                { literal: "runner" },
                { secret: canaries.argvKeep },
                { literal: "--token" },
                { secret: canaries.argvReplace },
              ],
              clear: [],
            },
          ],
        },
      });
      const seedText = await secretSeed.text();
      expect(secretSeed.status(), seedText).toBe(200);
      expect(seedText).not.toContain(canaries.header);
      expect(seedText).not.toContain(canaries.env);
      expect(seedText).not.toContain(canaries.argvKeep);
      expect(seedText).not.toContain(canaries.argvReplace);

      const before = await readRegistry();
      const beforeRecord = before.registry.services[id];
      const beforeHeader = beforeRecord.config.headers.find((entry) => entry.name === "X-Tenant");
      if (!beforeHeader) throw new Error("seeded header entry is missing");
      const beforeEnv = beforeRecord.process.env.TOKEN;
      const beforeArgv = beforeRecord.process.start;
      expect(beforeHeader.id).toMatch(/^ent_/);
      expect(beforeEnv.id).toMatch(/^ent_/);
      expect(entryId(beforeArgv[1])).toMatch(/^ent_/);
      expect(entryId(beforeArgv[3])).toMatch(/^ent_/);
      const retainedArgId = entryId(beforeArgv[1]);
      const replacedArgId = entryId(beforeArgv[3]);
      const beforeText = JSON.stringify(before.registry);
      for (const canary of Object.values(canaries)) expect(beforeText).not.toContain(canary);
      await readEngine();

      await page.goto(`/engines/?engine=${id}`);
      await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();

      const headerValue = page.getByRole("textbox", { name: "Value for X-Tenant" });
      const environmentValue = page.getByRole("textbox", { name: "Value for TOKEN" });
      const retainedArg = page.getByRole("textbox", { name: "process start command Argument 1" });
      const replaceArg = page.getByRole("textbox", { name: "process start command Argument 3" });
      await expect(headerValue).toHaveValue("");
      await expect(environmentValue).toHaveValue("");
      await expect(retainedArg).toHaveValue("");
      await expect(replaceArg).toHaveValue("");
      await expect(page.getByRole("textbox", { name: "process start command Program" })).toHaveValue("runner");
      await expect(page.getByRole("textbox", { name: "process start command Argument 2" })).toHaveValue("--token");

      await headerValue.fill(`header-replacement-${width}`);
      await environmentValue.fill(`env-replacement-${width}`);
      await replaceArg.fill(`argv-replacement-${width}`);
      await expect(page.getByRole("textbox", { name: "Name", exact: true })).toBeDisabled();
      await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeEnabled();

      const saved = page.waitForResponse((response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/admin/api/operator/services/${id}/secret-entries`),
      );
      await page.getByRole("button", { name: "Save changes", exact: true }).click();
      const saveResponse = await saved;
      expect(saveResponse.status()).toBe(200);
      const commands = saveResponse.request().postDataJSON().commands as Record<string, unknown>[];
      expect(commands).toEqual(expect.arrayContaining([
        expect.objectContaining({ carrier: "config.headers", operation: "replace", entry_id: beforeHeader.id }),
        expect.objectContaining({ carrier: "process.env", operation: "replace", entry_id: beforeEnv.id }),
        expect.objectContaining({ carrier: "process.start", operation: "set" }),
      ]));
      const argvCommand = commands.find((command) => command.carrier === "process.start");
      expect(argvCommand?.clear).toEqual([]);
      const items = argvCommand?.items as Record<string, unknown>[] | undefined;
      const replacement = items?.[3];
      if (!replacement) throw new Error("argv replacement item is missing");
      expect(Object.keys(replacement).sort()).toEqual(["replace", "secret"]);
      expect(replacement.replace).toBe(replacedArgId);
      expect(typeof replacement.secret).toBe("string");

      const after = await readRegistry();
      const afterRecord = after.registry.services[id];
      const afterHeader = afterRecord.config.headers.find((entry) => entry.name === "X-Tenant");
      if (!afterHeader) throw new Error("saved header entry is missing");
      const afterEnv = afterRecord.process.env.TOKEN;
      const afterArgv = afterRecord.process.start;
      expect(afterHeader.id).toBe(beforeHeader.id);
      expect(afterEnv.id).toBe(beforeEnv.id);
      expect(afterArgv[0]).toBe("runner");
      expect(entryId(afterArgv[1])).toBe(retainedArgId);
      expect(afterArgv[2]).toBe("--token");
      expect(entryId(afterArgv[3])).toBe(replacedArgId);
      const afterText = JSON.stringify(after.registry);
      for (const canary of Object.values(canaries)) expect(afterText).not.toContain(canary);
      expect(afterText).not.toContain(`header-replacement-${width}`);
      expect(afterText).not.toContain(`env-replacement-${width}`);
      expect(afterText).not.toContain(`argv-replacement-${width}`);
      await readEngine();

      await page.reload();
      await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
      await expect(page.getByRole("textbox", { name: "Value for X-Tenant" })).toHaveValue("");
      await expect(page.getByRole("textbox", { name: "Value for TOKEN" })).toHaveValue("");
      await expect(page.getByRole("textbox", { name: "process start command Program" })).toHaveValue("runner");
      await expect(page.getByRole("textbox", { name: "process start command Argument 1" })).toHaveValue("");
      await expect(page.getByRole("textbox", { name: "process start command Argument 2" })).toHaveValue("--token");
      await expect(page.getByRole("textbox", { name: "process start command Argument 3" })).toHaveValue("");
      if (width === 390)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath(`secret-entries-${width}-blank-after-reload.png`),
        fullPage: true,
      });
    } finally {
      if (serviceId) {
        const current = await readRegistry();
        const removed = await page.request.delete(`/admin/api/operator/services/${serviceId}`, {
          headers: writeHeaders(current.etag),
        });
        expect(removed.ok()).toBe(true);
      }
    }
  });
}

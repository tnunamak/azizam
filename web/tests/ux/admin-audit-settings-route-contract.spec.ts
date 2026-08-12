import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDirectory, "../..");

async function source(relativePath: string) {
  return readFile(path.join(webRoot, relativePath), "utf8");
}

describe("admin audit and settings route contracts", () => {
  it("uses server-issued actor labels without ID-prefix inference", async () => {
    const audit = await source("src/app/admin/audit/page.tsx");
    expect(audit).toContain("return entry.actor_label");
    expect(audit).not.toContain('startsWith("svc_")');
  });

  it("keeps OIDC responses closed and mutations write-only", async () => {
    const [api, settings] = await Promise.all([
      source("src/lib/api.ts"),
      source("src/app/admin/settings/page.tsx"),
    ]);
    expect(api).toContain('client_secret: "<redacted>" | ""');
    expect(api).toContain("export interface OidcConfigUpdate");
    expect(settings).toMatch(/saveOidcConfig\(\s*input: OidcConfigUpdate/);
    expect(settings).toContain("const payload: OidcConfigUpdate");
    expect(settings).toContain('if (secret && secret !== "<redacted>")');
  });

  it("keeps the mounted routes prepared for complete, redacted OIDC state", async () => {
    const settings = await source("src/app/admin/settings/page.tsx");
    expect(settings).toContain("config.provenance.fields");
    expect(settings).toContain("config.apply_mode");
    expect(settings).toContain("admin_emails");
    expect(settings).toContain("Couldn't load sign-in settings.");
  });
});

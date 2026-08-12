import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDirectory, "../..");

async function routeSource(route: "audit" | "settings") {
  return readFile(
    path.join(webRoot, "src", "app", "admin", route, "page.tsx"),
    "utf8",
  );
}

describe("admin audit and settings route contracts", () => {
  it("keeps audit filters and cursors server-backed, and only copies safe details", async () => {
    const source = await routeSource("audit");

    expect(source).toContain("api.audit.list(firstPageParams)");
    expect(source).toContain("before: nextCursor");
    expect(source).toContain(
      "setAllEntries((prev) => [...prev, ...result.records])",
    );
    expect(source).toContain('"No more matching events"');
    expect(source).toContain("function safeAuditDetails");
    expect(source).toContain("Safe event details");
    expect(source).not.toContain("JSON.stringify(entry, null, 2)");
  });

  it("keeps OIDC secrets write-only and settings mutations scoped to their cards", async () => {
    const source = await routeSource("settings");

    expect(source).toContain('if (secret && secret !== "<redacted>")');
    expect(source).toContain("restart required to apply sign-in changes");
    expect(source).toContain(
      "local admin account or your existing recovery method",
    );
    expect(source).toContain(
      "Queue depth and retry delay must be whole numbers zero or greater.",
    );
    expect(source).toContain("setShowRestartBanner(true)");
    expect(source).not.toMatch(/(?<![\w/])window\.confirm\s*\(/);
  });
});

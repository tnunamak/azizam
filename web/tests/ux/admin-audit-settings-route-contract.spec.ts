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
  it("keeps audit filters, URL state, and cursors server-backed", async () => {
    const source = await routeSource("audit");

    expect(source).toContain("api.audit.list(firstPageParams)");
    expect(source).toContain("before: cursor");
    expect(source).toContain("generationRef.current");
    expect(source).toContain('params.set(key, value)');
    expect(source).toContain('searchParams.get("category")');
    expect(source).toContain('family: "credential"');
    expect(source).toContain('family: "admin_key"');
    expect(source).toContain('"No more matching events"');
    expect(source).toContain("function auditDetails");
    expect(source).toContain("Safe event details");
    expect(source).not.toContain("startsWith(\"svc_\")");
    expect(source).not.toContain("/(secret|token");
  });

  it("keeps OIDC secrets write-only and settings mutations scoped to their cards", async () => {
    const source = await routeSource("settings");

    expect(source).toContain('if (secret && secret !== "<redacted>")');
    expect(source).toContain("typed API contract is available");
    expect(source).toContain("disabled={isPending || isError}");
    expect(source).toContain("error={fieldErrors.redirectUri}");
    expect(source).toContain(
      "Queue depth and retry delay must be whole numbers zero or greater.",
    );
    expect(source).not.toContain("restart required to apply sign-in changes");
    expect(source).not.toMatch(/(?<![\w/])window\.confirm\s*\(/);
  });
});

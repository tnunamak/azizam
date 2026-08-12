import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const overviewSource = await readFile(
  path.join(testDirectory, "../src/app/admin/page.tsx"),
  "utf8",
);

describe("Admin Overview route contract", () => {
  it("puts the shared health band before attention and uses the status diagnosis route", () => {
    const healthBand = overviewSource.indexOf("<SystemHealthBand");
    const attention = overviewSource.indexOf("<AttentionSection");

    expect(healthBand).toBeGreaterThan(-1);
    expect(attention).toBeGreaterThan(healthBand);
    expect(overviewSource).toContain('href="/admin/status"');
  });

  it("keeps attention navigation canonical and avoids a misleading test-request destination", () => {
    expect(overviewSource).toContain(
      "adminTargetHref(item.target_kind, item.target_id)",
    );
    expect(overviewSource).not.toContain('id: "send-request"');
    expect(overviewSource).not.toContain('href="/admin/services">Docs');
  });

  it("keeps unavailable health neutral and exposes local retries", () => {
    expect(overviewSource).toContain('label: "Live status unavailable"');
    expect(overviewSource).toContain('tone: "neutral" as HealthTone');
    expect(overviewSource).toContain("refetchStatus");
    expect(overviewSource).toContain("refetchOverview");
    expect(overviewSource).toContain("refetchApps");
    expect(overviewSource).toContain("refetchAudit");
  });
});

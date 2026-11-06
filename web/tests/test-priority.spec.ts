import { readdirSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import { LOW_NICE, lowerTestPriority } from "../test-priority";

const web = path.resolve(__dirname, "..");

function controls(nice: number) {
  return { getPriority: vi.fn(() => nice), setPriority: vi.fn(), setIdleIo: vi.fn() };
}

describe("lowerTestPriority", () => {
  it("lowers a normal-priority runner to nice 19 and idle I/O and marks it", () => {
    const env: Record<string, string | undefined> = {};
    const c = controls(0);
    expect(lowerTestPriority(env, c)).toBe("low");
    expect(c.setPriority).toHaveBeenCalledExactlyOnceWith(LOW_NICE);
    expect(c.setIdleIo).toHaveBeenCalledOnce();
    expect(env.AZIZAM_PRIORITY_SET).toBe("1");
  });

  it("leaves an already-lowered runner's nice value alone but still sets idle I/O", () => {
    const c = controls(LOW_NICE);
    expect(lowerTestPriority({}, c)).toBe("low");
    expect(c.setPriority).not.toHaveBeenCalled();
    expect(c.setIdleIo).toHaveBeenCalledOnce();
  });

  it("keeps the current priority when AZIZAM_TEST_PRIORITY=normal", () => {
    const env: Record<string, string | undefined> = { AZIZAM_TEST_PRIORITY: "normal" };
    const c = controls(0);
    expect(lowerTestPriority(env, c)).toBe("normal");
    expect(c.setPriority).not.toHaveBeenCalled();
    expect(c.setIdleIo).not.toHaveBeenCalled();
    expect(env.AZIZAM_PRIORITY_SET).toBeUndefined();
  });
});

describe("every test runner config lowers its own priority first", () => {
  const configs = readdirSync(web).filter(
    (name) => /^playwright(\..+)?\.config\.ts$/.test(name) || name === "vitest.config.ts",
  );

  it("finds the Playwright and vitest configs", () => {
    expect(configs).toContain("vitest.config.ts");
    expect(configs).toContain("playwright.config.ts");
  });

  it.each(configs)("%s imports ./test-priority before any other module", (name) => {
    const imports = readFileSync(path.join(web, name), "utf8")
      .split("\n")
      .filter((line) => line.startsWith("import "));
    expect(imports[0]).toMatch(/^import "\.\/test-priority";/);
  });

  it("this vitest run itself is at nice 19 unless the owner opted out", () => {
    if (process.env.AZIZAM_TEST_PRIORITY === "normal") return;
    expect(os.getPriority()).toBe(LOW_NICE);
  });
});

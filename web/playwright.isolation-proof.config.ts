import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Proof run for e2e/fixtures/isolation.ts (scripts/e2e/prove-isolation):
// several workers share one fixture gateway, as the shared-server lanes do.
export default defineConfig({
  ...base,
  testMatch: ["isolation.proof.ts", "invite-signin.spec.ts"],
  testIgnore: [],
  // These projects set project-level testMatch values, which override the
  // proof's root testMatch and would otherwise add unrelated suites.
  projects: base.projects?.map((project) =>
    [
      "chromium-global-state-2",
      "chromium-first-run",
      "chromium-people",
      "chromium-try",
    ].includes(project.name ?? "")
      ? { ...project, testMatch: "no-test-runs-here.never" }
      : project,
  ),
  // A first failure stays a failure (TEST_STRATEGY.md 3.1).
  retries: 0,
  fullyParallel: true,
});

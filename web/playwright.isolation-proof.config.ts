import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Proof run for e2e/fixtures/isolation.ts (scripts/e2e/prove-isolation):
// several workers share one fixture gateway, as the shared-server lanes do.
export default defineConfig({
  ...base,
  testMatch: ["isolation.proof.ts", "invite-signin.spec.ts"],
  testIgnore: [],
  // The second global-state project and the fresh first-run project match
  // their own files at project level, which would override the proof's
  // testMatch: they run no proof case.
  projects: base.projects?.map((project) =>
    project.name === "chromium-global-state-2" || project.name === "chromium-first-run"
      ? { ...project, testMatch: "no-test-runs-here.never" }
      : project,
  ),
  // A first failure stays a failure (TEST_STRATEGY.md 3.1).
  retries: 0,
  fullyParallel: true,
});

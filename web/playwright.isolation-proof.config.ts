import "./test-priority"; // first: nice 19 + idle I/O for the runner and everything it starts
import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Proof run for e2e/fixtures/isolation.ts (scripts/e2e/prove-isolation):
// several workers share one fixture gateway, as the shared-server lanes do.
export default defineConfig({
  ...base,
  testMatch: ["isolation.proof.ts", "invite-signin.spec.ts"],
  testIgnore: [],
  // A first failure stays a failure (TEST_STRATEGY.md 3.1).
  retries: 0,
  fullyParallel: true,
});

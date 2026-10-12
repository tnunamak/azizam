// Lower a test run to nice 19 and the idle I/O class, however it was started.
// scripts/low-priority covers the package.json scripts and our shell entry
// points, but a bare `npx playwright test` or `npx vitest` bypassed it and its
// browsers starved the live gateway on this machine. Every Playwright and
// vitest config imports this module first, so the runner lowers itself before
// it starts web servers, browsers or workers; those inherit the priority.
// AZIZAM_TEST_PRIORITY=normal keeps the current priority (quiet machine).
import { execFileSync } from "node:child_process";
import os from "node:os";

export const LOW_NICE = 19;

export type PriorityControls = {
  getPriority: () => number;
  setPriority: (nice: number) => void;
  setIdleIo: () => void;
};

const processControls: PriorityControls = {
  getPriority: () => os.getPriority(),
  setPriority: (nice) => os.setPriority(nice),
  setIdleIo: () => {
    try {
      execFileSync("ionice", ["-c3", "-p", String(process.pid)], { stdio: "ignore" });
    } catch {
      // No ionice on this machine: the nice value still applies.
    }
  },
};

export function lowerTestPriority(
  env: Record<string, string | undefined> = process.env,
  controls: PriorityControls = processControls,
): "low" | "normal" {
  if (env.AZIZAM_TEST_PRIORITY === "normal") return "normal";
  if (controls.getPriority() < LOW_NICE) controls.setPriority(LOW_NICE);
  controls.setIdleIo();
  env.AZIZAM_PRIORITY_SET = "1";
  return "low";
}

lowerTestPriority();

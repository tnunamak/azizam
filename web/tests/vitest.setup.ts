import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach } from "vitest";
import { parse } from "yaml";
import {
  setRetentionContract,
  type RetentionContract,
} from "@/lib/prompt-memory";

// The screens keep no copy of the retention choices or defaults; every
// prompt-memory response carries them (gateway/retention.py `public_contract`).
// Specs answer those requests with their own small bodies, so each file starts
// from the gateway's own data file, read the way the gateway reads it.
const retentionFile = parse(
  readFileSync(path.resolve(__dirname, "../../registry/retention.yml"), "utf8"),
) as {
  retention_choices: Record<string, number | null>;
  household: { never_keep: boolean };
  groups: {
    prompts: {
      default_keep: boolean;
      default_retention: string;
      default_cap: string;
      choices: string[];
    };
  };
};
const prompts = retentionFile.groups.prompts;
const bundledContract: RetentionContract = {
  choices: prompts.choices.map((id) => ({
    id,
    seconds: retentionFile.retention_choices[id],
  })),
  defaults: {
    default_remember: prompts.default_keep,
    never_keep: retentionFile.household.never_keep,
    retention_cap: prompts.default_cap,
    remember: prompts.default_keep,
    retention: prompts.default_retention,
  },
};
setRetentionContract(bundledContract);

// Specs replace these browser properties with Object.defineProperty, which
// neither vi.unstubAllGlobals nor restoreMocks undoes. Snapshot each one per
// file and put it back (or delete it) after every test, so no test depends on
// which test ran before it.
const replaceable = (
  [
    [navigator, "clipboard"],
    [navigator, "mediaDevices"],
    [document, "execCommand"],
  ] as const
).map(
  ([target, key]) => [target, key, Object.getOwnPropertyDescriptor(target, key)] as const,
);

afterEach(() => {
  setRetentionContract(bundledContract);
  // The test files run without vitest globals, so Testing Library cannot
  // register its own cleanup; do it here for every spec.
  cleanup();
  sessionStorage.clear();
  localStorage.clear();
  for (const [target, key, original] of replaceable) {
    if (original) Object.defineProperty(target, key, original);
    else Reflect.deleteProperty(target, key);
  }
});

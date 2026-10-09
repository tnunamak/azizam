#!/usr/bin/env node
// `npm run sweep [-- --live | --copy] [-- --out report.md] [extra playwright args]`
// Runs the sweep, then writes sweep.json and report.md even when it finds defects.
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const args = process.argv.slice(2);
const target = args.includes("--live") ? "live" : args.includes("--copy") ? "copy" : process.env.SWEEP_TARGET ?? "fixtures";
const outIndex = args.indexOf("--out");
const out = outIndex >= 0 ? args[outIndex + 1] : null;
const passThrough = args.filter(
  (arg, index) => !["--live", "--copy"].includes(arg) && (outIndex < 0 || (index !== outIndex && index !== outIndex + 1)),
);
const env = { ...process.env, SWEEP_TARGET: target };

const run = spawnSync("npx", ["playwright", "test", "--config", "playwright.sweep.config.ts", ...passThrough], {
  cwd: web,
  env,
  stdio: "inherit",
});
const report = spawnSync("node", ["e2e/sweep/report.mjs", env.SWEEP_TARGET, ...(out ? ["--out", path.resolve(out)] : [])], {
  cwd: web,
  env,
  stdio: "inherit",
});
process.exit(run.status ?? (report.status || 1));

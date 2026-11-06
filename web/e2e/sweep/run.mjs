#!/usr/bin/env node
// `npm run sweep [-- --live | --copy] [--out report.md] [--baseline known.json]
//   [--write-baseline known.json] [extra playwright args]`
// Runs the sweep, then writes sweep.json and report.md even when it finds defects.
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const args = process.argv.slice(2);
const target = args.includes("--live") ? "live" : args.includes("--copy") ? "copy" : process.env.SWEEP_TARGET ?? "fixtures";
// Flags with a value that belong to report.mjs, not Playwright.
const reportFlags = ["--out", "--baseline", "--write-baseline"];
const reportArgs = [];
const passThrough = [];
for (let index = 0; index < args.length; index++) {
  if (["--live", "--copy"].includes(args[index])) continue;
  if (reportFlags.includes(args[index])) {
    reportArgs.push(args[index], path.resolve(args[index + 1]));
    index++;
  } else passThrough.push(args[index]);
}
const env = { ...process.env, SWEEP_TARGET: target };

const run = spawnSync("npx", ["playwright", "test", "--config", "playwright.sweep.config.ts", ...passThrough], {
  cwd: web,
  env,
  stdio: "inherit",
});
const report = spawnSync("node", ["e2e/sweep/report.mjs", env.SWEEP_TARGET, ...reportArgs], {
  cwd: web,
  env,
  stdio: "inherit",
});
// The report decides: with --baseline only new findings fail; without it, any finding fails.
process.exit(report.status ?? (run.status || 1));

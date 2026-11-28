import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import ts from "typescript";

// Run a review command against exactly the gateway used by Playwright, then
// stop its supervisor (which owns and cleans up both gateway and fake engine).
const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const port = Number(process.env.UI_E2E_PORT ?? "18180");
if (!Number.isInteger(port) || port < 18000 || port > 18998)
  throw new Error("UI_E2E_PORT must be an isolated port from 18000 to 18998");
const localPython = path.resolve(web, "../venv/bin/python");
const python =
  process.env.UI_E2E_PYTHON ??
  (existsSync(localPython) ? localPython : "python3");
const command = process.argv.slice(2);
if (!command.length)
  throw new Error(
    "Usage: node e2e/seed-review.mjs <review command> [arguments]",
  );
const baseURL = `http://127.0.0.1:${port}`;
try {
  const response = await fetch(`${baseURL}/health`, {
    signal: AbortSignal.timeout(2_000),
  });
  if (response.ok) throw new Error(`Port ${port} already serves a gateway`);
} catch (error) {
  if (error.message?.includes("already serves")) throw error;
}
const server = spawn(python, ["../scripts/ui/serve.py"], {
  cwd: web,
  env: { ...process.env, UI_E2E_PORT: String(port) },
  stdio: ["ignore", "inherit", "inherit"],
});
console.log(`[review] Waiting for isolated gateway on port ${port}`);
let serverFailure;
const serverStopped = new Promise((resolve) => {
  server.once("exit", resolve);
  server.once("error", (error) => {
    serverFailure = error;
    resolve();
  });
});
let review;
const stop = () => {
  review?.kill("SIGTERM");
  server.kill("SIGTERM");
};
process.once("SIGINT", stop);
process.once("SIGTERM", stop);
try {
  const deadline = Date.now() + 120_000;
  while (true) {
    if (serverFailure) throw serverFailure;
    if (server.exitCode !== null) throw new Error("Isolated supervisor exited");
    try {
      const response = await fetch(`${baseURL}/health`, {
        signal: AbortSignal.timeout(2_000),
      });
      if (response.ok) break;
    } catch {}
    if (Date.now() > deadline)
      throw new Error("Isolated server readiness timed out");
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  console.log("[review] Gateway ready; preparing the existing UI seed");
  const output = path.resolve(web, "test-results/review-global-setup.mjs");
  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(
    output,
    ts.transpileModule(
      await readFile(path.resolve(web, "e2e/global-setup.ts"), "utf8"),
      {
        compilerOptions: {
          module: ts.ModuleKind.ESNext,
          target: ts.ScriptTarget.ES2022,
        },
      },
    ).outputText,
  );
  const { default: seed } = await import(pathToFileURL(output));
  process.chdir(web);
  console.log("[review] Seeding owner, service, app and a real request");
  await seed({
    projects: [
      { use: { baseURL, storageState: `./test-results/auth-${port}.json` } },
    ],
  });
  console.log("[review] Seed complete; starting the review command");
  review = spawn(command[0], command.slice(1), {
    cwd: web,
    env: { ...process.env, APP_URL: baseURL, UI_E2E_PORT: String(port) },
    stdio: "inherit",
  });
  const exitCode = await new Promise((resolve, reject) => {
    review.once("error", reject);
    review.once("exit", (code) => resolve(code ?? 1));
  });
  process.exitCode = exitCode;
} finally {
  console.log("[review] Stopping isolated gateway and test engine");
  stop();
  await serverStopped;
}

// Gate test for the live Node probes.
//
// Runs each probe file in a child Node process whose fetch and sockets are
// replaced before any SDK loads. The oracle is the list of attempted requests:
// a probe that is opted out must not start provider work.

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const directory = fileURLToPath(new URL(".", import.meta.url));

function capturedRequests(filename, suppliedEnv) {
  const preload = [
    'import { writeSync } from "node:fs";',
    'import net from "node:net";',
    'const calls = [];',
    'globalThis.fetch = async (input, init = {}) => {',
    '  const url = typeof input === "string" ? input : input.url || String(input);',
    '  let body = init.body;',
    '  if (body === undefined && typeof input?.clone === "function") {',
    '    body = await input.clone().text();',
    '  }',
    '  let parsed = null;',
    '  try { parsed = JSON.parse(String(body ?? "")); } catch {}',
    '  calls.push({ path: new URL(url).pathname, body: parsed });',
    '  throw new Error("compat gate test intercepted fetch");',
    '};',
    'net.Socket.prototype.connect = function () {',
    '  calls.push({ path: "UNEXPECTED_SOCKET", body: null });',
    '  throw new Error("compat gate test blocked a real socket");',
    '};',
    'process.on("exit", () => {',
    '  writeSync(1, "\\nCOMPAT_REQUESTS=" + JSON.stringify(calls) + "\\n");',
    '});',
  ].join("\n");
  const env = { ...process.env };
  for (const name of Object.keys(env)) {
    if (name.startsWith("AZIZAM_")) delete env[name];
  }
  delete env.OPENAI_API_KEY;
  delete env.ANTHROPIC_API_KEY;
  Object.assign(env, {
    AZIZAM_BASE_URL: "http://compat.invalid",
    ...suppliedEnv,
  });
  const child = spawnSync(
    process.execPath,
    ["--import", "data:text/javascript," + encodeURIComponent(preload), filename],
    { cwd: directory, env, encoding: "utf8", timeout: 20000, maxBuffer: 4194304 },
  );
  assert.ifError(child.error);
  assert.equal(child.signal, null, child.stderr);
  const match = child.stdout.match(/^COMPAT_REQUESTS=(.*)$/m);
  assert.ok(match, child.stdout + "\n" + child.stderr);
  const registration = {
    "anthropic.test.mjs": "client exposes messages and models resources",
    "ollama.test.mjs": "client exposes Ollama resources",
    "openai.test.mjs": "GET /v1/models returns a list",
  }[filename];
  assert.ok(
    child.stdout.includes(registration),
    "Probe failed before registering tests: " + child.stderr,
  );
  const calls = JSON.parse(match[1]);
  assert.deepEqual(
    calls.filter((call) => call.path === "UNEXPECTED_SOCKET"),
    [],
    "An SDK bypassed the fail-closed fetch seam",
  );
  return calls;
}

function missingPackage(name) {
  try {
    require.resolve(name);
    return false;
  } catch (error) {
    if (error.code !== "MODULE_NOT_FOUND") throw error;
    return "optional SDK not installed: " + name;
  }
}

function isProviderWork(call) {
  if (/\/api\/(chat|generate|embed|embeddings)$/.test(call.path)) return true;
  if (/\/v1\/(embeddings|chat\/completions|responses)$/.test(call.path)) return true;
  return /\/v1\/messages$/.test(call.path) && call.body?.max_tokens !== 0;
}

for (const [filename, dependency] of [
  ["anthropic.test.mjs", "@anthropic-ai/sdk"],
  ["ollama.test.mjs", "ollama"],
]) {
  test(filename + " makes no request without an API key", {
    skip: missingPackage(dependency),
  }, () => {
    assert.deepEqual(capturedRequests(filename, {}), []);
  });
}

for (const [filename, dependency] of [
  ["anthropic.test.mjs", "@anthropic-ai/sdk"],
  ["ollama.test.mjs", "ollama"],
  ["openai.test.mjs", "openai"],
]) {
  test(filename + " does no provider work without live flags", {
    skip: missingPackage(dependency),
  }, () => {
    const calls = capturedRequests(filename, { AZIZAM_API_KEY: "compat-fixture" });
    assert.deepEqual(calls.filter(isProviderWork), []);
  });
}

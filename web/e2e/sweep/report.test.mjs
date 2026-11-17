import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, utimesSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const report = fileURLToPath(new URL("./report.mjs", import.meta.url));
const started = Date.UTC(2025, 0, 1);
const clean = { surface: "Home", route: "/", state: "ready", role: "owner", width: 1280, theme: "light", status: "checked", loading: false };

function fixture({ receipt = true, old = false, states = [clean], marker = true, receiptBody } = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), "sweep-report-"));
  try {
    mkdirSync(path.join(dir, "findings"));
    writeFileSync(path.join(dir, "findings", "page.json"), JSON.stringify({ findings: [], states }));
    if (receipt) {
      const file = path.join(dir, "playwright.json");
      writeFileSync(file, receiptBody ?? JSON.stringify({
        stats: { expected: 1, unexpected: 0, flaky: 0, skipped: 0, duration: 1000 },
        suites: [{ specs: [{ title: "synthetic clean page", tests: [{ results: [{ status: "passed", errors: [] }] }] }] }],
        errors: [],
      }));
      const mtime = new Date(started + (old ? -10000 : 10000));
      utimesSync(file, mtime, mtime);
    }
    const env = { ...process.env, SWEEP_RESULTS_DIR: dir, SWEEP_TARGET: "fixtures" };
    delete env.SWEEP_RUN_STARTED_MS;
    if (marker) env.SWEEP_RUN_STARTED_MS = String(started);
    const result = spawnSync(process.execPath, [report, "fixtures"], {
      env, encoding: "utf8", timeout: 10000, killSignal: "SIGKILL", maxBuffer: 1024 * 1024,
    });
    assert.ifError(result.error);
    assert.equal(result.signal, null, result.stderr);
    return { ...result, summary: JSON.parse(readFileSync(path.join(dir, "sweep.json"), "utf8")).summary };
  } finally {
    rmSync(dir, { recursive: true });
  }
}

test("missing runner receipt fails even with a clean checked state", () => {
  const result = fixture({ receipt: false });
  assert.equal(result.status, 1);
  assert.match(result.stdout, /broken: no runner receipt from this run/);
});
test("old runner receipt fails for the current run", () => {
  const result = fixture({ old: true });
  assert.equal(result.status, 1);
  assert.match(result.stdout, /broken: no runner receipt from this run/);
});
test("fresh runner receipt and one clean state pass", () => {
  const result = fixture();
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.equal(result.summary.pages_checked, 1);
  assert.doesNotMatch(result.stdout, /broken:/);
});
test("fresh runner receipt with zero page states fails", () => {
  const result = fixture({ states: [] });
  assert.equal(result.status, 1);
  assert.match(result.stdout, /broken: no page states were checked/);
});
test("manual report still requires a receipt but does not judge its age", () => {
  assert.equal(fixture({ marker: false, old: true }).status, 0);
  const missing = fixture({ marker: false, receipt: false });
  assert.equal(missing.status, 1);
  assert.match(missing.stdout, /no runner receipt from this run/);
  const empty = fixture({ marker: false, states: [] });
  assert.equal(empty.status, 1);
  assert.match(empty.stdout, /no page states were checked/);
});

test("all skipped states fail the checked-page gate", () => {
  const result = fixture({states: [{...clean, status: "skipped", reason: "synthetic skip"}]});
  assert.equal(result.status, 1);
  assert.match(result.stdout, /broken: no page states were checked/);
});
test("one checked and one skipped state still pass", () => {
  const result = fixture({states: [clean, {...clean, status: "skipped", reason: "synthetic skip"}]});
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.equal(result.summary.pages_checked, 1);
});
test("fresh receipt without stats still reports top-level and suite errors", () => {
  const result = fixture({receiptBody: JSON.stringify({errors: [{message: "synthetic runner failure"}],
    suites: [{specs: [{title: "broken suite", tests: [{results: [{status: "failed", errors: [{message: "synthetic test failure"}]}]}]}]}]})});
  assert.equal(result.status, 1);
  assert.match(result.stdout, /broken: runner receipt is malformed/);
  assert.match(result.stdout, /run error: synthetic runner failure/);
  assert.match(result.stdout, /broken suite: failed synthetic test failure/);
});
for (const receiptBody of ["{", "null", "[]", "42", '{"stats":null}', '{"stats":[]}', '{"stats":true}']) {
  test("malformed fresh receipt fails plainly: " + receiptBody, () => {
    const result = fixture({receiptBody});
    assert.equal(result.status, 1);
    assert.match(result.stdout, /broken: runner receipt is malformed/);
    assert.equal(result.summary.pages_checked, 1);
  });
}

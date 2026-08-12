import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const page = new URL("../../src/app/admin/services/page.tsx", import.meta.url);

async function source() {
  return readFile(page, "utf8");
}

test("Services keeps registry identity and live observation as separate facts", async () => {
  const contents = await source();

  assert.match(contents, /backend\.service_id \|\| backend\.key/);
  assert.match(contents, /service\.service_id/);
  assert.match(contents, /live status unavailable/);
  assert.match(contents, /required service down/);
  assert.match(contents, /optional service down/);
  assert.match(contents, /standby, stopped/);
  assert.match(contents, /standby, disabled/);
});

test("Services keeps model discovery, routability, and probing honest", async () => {
  const contents = await source();

  assert.match(
    contents,
    /successful probe does not prove that a model is routable/,
  );
  assert.match(contents, /Detection does not[\s\S]*create routable mappings/);
  assert.match(contents, /Configure mappings/);
  assert.match(contents, /latencyPoints\.length >= 3/);
});

test("Services uses the frozen operational primitives for context and destructive work", async () => {
  const contents = await source();

  for (const primitive of [
    "RightContextPanel",
    "SheetContent",
    "ConfirmDialog",
    "FilterToolbar",
    "IncidentBanner",
    "EmptyOperationalState",
  ]) {
    assert.match(contents, new RegExp(`\\b${primitive}\\b`));
  }
  assert.doesNotMatch(contents, /window\.confirm\(/);
});

test("Services keeps responsive context, live removal, and role semantics honest", async () => {
  const contents = await source();

  assert.match(contents, /\(min-width: 980px\)/);
  assert.match(contents, /isDesktopServiceContext \? \(/);
  assert.match(contents, /<SheetDescription>/);
  assert.match(contents, /aria-expanded=\{isSelected\}/);
  assert.doesNotMatch(contents, /aria-selected=\{isSelected\}/);
  assert.match(
    contents,
    /await removeMutation\.mutateAsync\(removeTarget\.id\)/,
  );
  assert.match(
    contents,
    /stop working immediately when\s+this live change is applied/,
  );
  assert.match(contents, /provisioned seed is not re-imported/);
  assert.match(contents, /outages raise required alerts/);
  assert.match(contents, /outages raise optional alerts/);
  assert.match(contents, /outages raise standby alerts/);
  assert.doesNotMatch(contents, /routes around if unavailable/);
  assert.doesNotMatch(contents, /only used when explicitly requested/);
});

test("Services retains editable managed metadata and accessible validation", async () => {
  const contents = await source();

  assert.match(contents, /const \[managedNote, setManagedNote\]/);
  assert.match(contents, /note: managedNote/);
  assert.match(contents, /<Field label="Note">/);
  assert.match(contents, /label="Provider"\s+required/);
  assert.match(contents, /errorSummaryRef/);
  assert.match(contents, /formErrorRef\.current\?\.focus\(\)/);
  assert.match(contents, /Advanced — managed in config/);
});

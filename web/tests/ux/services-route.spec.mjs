import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const addServiceDialog = new URL(
  "../../src/components/admin/add-service-dialog.tsx",
  import.meta.url,
);

test("Engine connection dialog retains accessible validation", async () => {
  const dialogContents = await readFile(addServiceDialog, "utf8");

  assert.match(dialogContents, /label="Preset"\s+required/);
  assert.match(dialogContents, /errorSummaryRef/);
  assert.match(dialogContents, /errorSummaryRef\.current\?\.focus\(\)/);
});

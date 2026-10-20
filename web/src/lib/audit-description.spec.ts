import { expect, it } from "vitest";
import type { AuditEntry } from "./api";
import { auditDescription } from "./audit-description";

const entry = (action: string): AuditEntry => ({
  id: "a1",
  created_at: 1800000000,
  actor_kind: "user",
  actor_label: "Sam",
  actor_id: "u1",
  action,
  target_kind: null,
  target_id: null,
  metadata: null,
});

it.each([
  ["app.self_rename", "Renamed an app"],
  ["credential.rename", "Renamed a key"],
  ["credential.self_rename", "Renamed a key"],
  ["app.update", "Updated an app"],
])("describes %s in plain words", (action, words) => {
  expect(auditDescription(entry(action))).toBe(words);
});

it("falls back to a plain verb and thing, or says only that something changed", () => {
  expect(auditDescription(entry("app.rename"))).toBe("Renamed an app");
  expect(auditDescription(entry("unknown.thing"))).toBe("Recorded a change");
});

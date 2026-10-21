import { expect, it } from "vitest";
import { headersFrom, headersBody } from "./more-options";
it("never echoes a stored header marker into an inline values write", () => {
  const rows = headersFrom([
    { id: "ent_header", name: "X-Tenant", configured: true },
  ]);
  expect(rows[0].value).toBe("");
  expect(headersBody(rows)).toEqual([]);
});

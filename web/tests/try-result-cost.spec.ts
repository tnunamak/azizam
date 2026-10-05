import { expect, it } from "vitest";
import { costFrom } from "../src/app/try/result-cost";

it("reports no provider charge for a local result", () => {
  expect(costFrom({ decision: "yes" }, undefined, "Local")).toBe(
    "Local · no provider charge",
  );
});

it("keeps a local label for a measured zero provider charge", () => {
  expect(
    costFrom(
      {},
      new Response(null, { headers: { "x-litellm-response-cost": "0" } }),
      "Local",
    ),
  ).toBe("Local · no provider charge");
});

it("omits missing locality and cost", () => {
  expect(costFrom({ decision: "yes" })).toBeUndefined();
});

it("retains a measured remote charge", () => {
  expect(
    costFrom(
      {},
      new Response(null, { headers: { "x-litellm-response-cost": "0.12" } }),
      "Remote",
    ),
  ).toBe("$0.12");
});

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

it.each([
  { cost: 0.12 },
  { remote_cost: 0.12 },
  { measured_cost: { amount_usd: 0.12 } },
  { cost: "0.12" },
])("TM-08 retains a positive payload charge for a Local result: %j", (payload) => {
  const expected = typeof payload.cost === "string" ? "0.12" : "$0.12";
  expect(costFrom(payload, undefined, "Local")).toBe(expected);
  expect(costFrom(payload, new Response(null, {
    headers: { "x-litellm-response-cost": "0" },
  }), "Local")).toBe(expected);
  expect(costFrom(payload, new Response(null, {
    headers: { "x-litellm-response-cost": "0.25" },
  }), "Local")).toBe("$0.25");
});

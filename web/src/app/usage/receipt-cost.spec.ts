import { expect, test } from "vitest";
import { receiptCost, receiptCostUsd } from "@/app/usage/receipt-cost";

test("cost_usd 0.0123 is an amount", () => {
  expect(receiptCost({ cost_usd: 0.0123 })).toEqual({ kind: "amount", usd: 0.0123, excluded: 0, local: 0 });
});
test("served_locally without cost is local", () => {
  expect(receiptCost({ served_locally: true })).toEqual({ kind: "local", local: 1 });
});
test("remote without cost is not-reported with excluded 1", () => {
  expect(receiptCost({ remote: true })).toEqual({ kind: "not-reported", excluded: 1, local: 0 });
});
test("cost.usd is read when top fields are absent", () => {
  expect(receiptCost({ cost: { usd: "0.025" } })).toEqual({ kind: "amount", usd: 0.025, excluded: 0, local: 0 });
});
test("metadata.cost_usd is read", () => {
  expect(receiptCost({ metadata: { cost_usd: 0.03 } })).toEqual({ kind: "amount", usd: 0.03, excluded: 0, local: 0 });
});
test("an empty object is unrecorded", () => {
  expect(receiptCost({})).toEqual({ kind: "unrecorded", local: 0 });
});
test("paid-remote classification preserves missing-cost exclusion even with local flag", () => {
  expect(receiptCost({ served_locally: true, cost: { cost_class: "paid-remote" } })).toEqual({ kind: "not-reported", excluded: 1, local: 1 });
});
test("receipt amount precedence and invalid-value fallback remain unchanged", () => {
  expect(receiptCost({ remote_spend: 0, cost_usd: 5, cost: { usd: 9 } })).toEqual({ kind: "amount", usd: 0, excluded: 0, local: 0 });
  expect(receiptCost({ cost_usd: "invalid", remote_cost_usd: 5, cost: { usd: 9 } })).toEqual({ kind: "amount", usd: 9, excluded: 0, local: 0 });
  expect(receiptCost({ cost_usd: 0, served_locally: true })).toEqual({ kind: "local", local: 1 });
});
test("normalizer amount excludes nested cost.usd while receipt display includes it", () => {
  const raw = { cost: { usd: 0.5 } };
  expect(receiptCostUsd(raw)).toBeNull();
  expect(receiptCost(raw)).toEqual({ kind: "amount", usd: 0.5, excluded: 0, local: 0 });
});
test("invalid receipt input remains unrecorded", () => {
  for (const raw of [null, undefined, [], "receipt", { cost_usd: -1 }, { cost_usd: Infinity }])
    expect(receiptCost(raw)).toEqual({ kind: "unrecorded", local: 0 });
});

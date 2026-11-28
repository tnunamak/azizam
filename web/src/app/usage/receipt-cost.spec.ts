import { expect, test } from "vitest";
import { costCopy, costState, receiptCost, receiptCostUsd } from "@/app/usage/receipt-cost";

test("cost_usd 0.0123 is an amount", () => {
  expect(receiptCost({ cost_usd: 0.0123 })).toEqual({ kind: "amount", usd: 0.0123, excluded: 0, local: 0 });
});
test("served_locally without cost is local", () => {
  expect(receiptCost({ served_locally: true })).toEqual({ kind: "local", local: 1 });
});
test("a locally served receipt is not plan-served even with a zero-cost plan marker", () => {
  expect(receiptCost({ served_locally: true, cost: { cost_class: "plan", usd: 0 } })).toEqual({
    kind: "local", local: 1,
  });
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
test("receipt display takes the first valid positive amount instead of stopping at zero or invalid input", () => {
  expect(receiptCost({ remote_spend: 0, cost_usd: 5, cost: { usd: 9 } })).toEqual({ kind: "amount", usd: 5, excluded: 0, local: 0 });
  expect(receiptCost({ cost_usd: "invalid", remote_cost_usd: 5, cost: { usd: 9 } })).toEqual({ kind: "amount", usd: 5, excluded: 0, local: 0 });
  expect(receiptCost({ cost_usd: 0, served_locally: true })).toEqual({ kind: "local", local: 1 });
});

test("a positive nested plan charge outranks an earlier zero", () => {
  expect(receiptCost({ remote_spend: 0, cost: { cost_class: "plan", usd: 0.02 } }))
    .toEqual({ kind: "amount", usd: 0.02, excluded: 0, local: 0 });
});
test("a locally served receipt still displays a reported nested amount", () => {
  expect(receiptCost({ served_locally: true, cost: { amount_usd: "0.02" } }))
    .toEqual({ kind: "amount", usd: 0.02, excluded: 0, local: 1 });
});
test("a refusal outranks a plan marker when no charge was reported", () => {
  expect(receiptCost({ refusal: { code: "limit" }, cost: { cost_class: "plan" } }))
    .toEqual({ kind: "refused", local: 0 });
});
test("a plan-marked fallback served locally without a reading is still local", () => {
  expect(receiptCost({ served_locally: true, cost: { cost_class: "plan" } }))
    .toEqual({ kind: "local", local: 1 });
});
test("CSV retains top-field precedence even when receipt display uses a later positive price", () => {
  expect(receiptCostUsd({ remote_spend: 0, cost_usd: 5, cost: { amount_usd: "0.02" } })).toBe(0);
  expect(receiptCostUsd({ cost: { amount_usd: "0.02" } })).toBeNull();
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

test("zero reported spend with unknown paid requests is not-reported", () => {
  expect(costState({ usd: 0, unknownPaid: 2, local: 1, requests: 4 }))
    .toEqual({ kind: "not-reported", excluded: 2, local: 1 });
});
test("positive reported spend retains its amount and unknown paid exclusions", () => {
  expect(costState({ usd: 0.5, unknownPaid: 2, local: 1, requests: 4 }))
    .toEqual({ kind: "amount", usd: 0.5, excluded: 2, local: 1 });
});
test("zero reported spend without unknown paid requests remains a remote amount", () => {
  expect(costState({ usd: 0, unknownPaid: 0, local: 1, requests: 3 }))
    .toEqual({ kind: "amount", usd: 0, excluded: 0, local: 1 });
});

test("subscription-plan receipts without a positive charge display the plan", () => {
  const plan = { cost: { usd: null, cost_class: "plan", provider: "chatgpt" } };
  for (const raw of [
    plan,
    { ...plan, remote: true },
    { ...plan, remote_spend: null },
    { ...plan, remote: true, remote_spend: null },
    { cost: { usd: 0, cost_class: "plan", provider: "claude" } },
  ]) {
    expect(receiptCost(raw)).toEqual({ kind: "plan", local: 0 });
  }
});

test("subscription-plan receipts with a positive charge still display the amount", () => {
  expect(receiptCost({ cost: { usd: 0.03, cost_class: "plan", provider: "chatgpt" } }))
    .toEqual({ kind: "amount", usd: 0.03, excluded: 0, local: 0 });
  expect(receiptCost({ remote_spend: 0.07, cost: { usd: null, cost_class: "plan", provider: "claude" } }))
    .toEqual({ kind: "amount", usd: 0.07, excluded: 0, local: 0 });
});

test("subscription-plan display says which allowance the request used", () => {
  expect(costCopy.plan).toEqual({
    label: "Subscription plan · no API charge",
    help: "A ChatGPT or Claude plan served this request. It uses the plan's allowance, not a paid key.",
  });
});

test("a group a plan served, alone or beside local requests, says the plan; a paid or unreported one never does", () => {
  expect(costState({ usd: null, unknownPaid: 0, local: 0, plan: 3, requests: 3 })).toEqual({ kind: "plan", local: 0 });
  expect(costState({ usd: 0, unknownPaid: 0, local: 2, plan: 1, requests: 3 })).toEqual({ kind: "plan", local: 2 });
  expect(costState({ usd: null, unknownPaid: 1, local: 0, plan: 2, requests: 3 }).kind).toBe("not-reported");
  expect(costState({ usd: 0.5, unknownPaid: 0, local: 0, plan: 2, requests: 3 }).kind).toBe("amount");
  expect(costState({ usd: null, unknownPaid: 0, local: 0, plan: 2, requests: 3 }).kind).toBe("unrecorded");
});
test("a request the gateway refused costs nothing and says so", () => {
  expect(receiptCost({ refusal: { code: "not_in_access_plan", limit: null, side: "app" } })).toEqual({ kind: "refused", local: 0 });
  expect(costCopy.refused.label).toBe("No charge · refused before it ran");
});

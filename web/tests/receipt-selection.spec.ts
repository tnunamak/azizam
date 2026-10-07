import { expect, test } from "vitest";
import { receiptSelectionKeys } from "@/lib/receipt-selection";

test("receipt selection preserves real IDs and avoids duplicate and fallback collisions", () => {
  const missing = { intent: "chat", metadata: { b: 2, a: 1 } };
  const fallback = receiptSelectionKeys([missing])[0];
  const receipts = [
    missing,
    { request_id: fallback },
    { request_id: "real" },
    { request_id: "real" },
    missing,
  ];
  const keys = receiptSelectionKeys(receipts);
  expect(new Set(keys).size).toBe(receipts.length);
  expect(keys[1]).toBe(fallback);
  expect(receiptSelectionKeys([{ request_id: "real" }])).toEqual(["real"]);
  expect(keys).toEqual(
    receiptSelectionKeys(receipts.map((receipt) => ({ ...receipt }))),
  );
  expect(
    receiptSelectionKeys([{ metadata: { a: 1, b: 2 }, intent: "chat" }])[0],
  ).toBe(fallback);
  expect(receiptSelectionKeys([{ id: "legacy" }])).toEqual(["legacy"]);
});

test("reordering duplicate real IDs keeps each key on its own receipt", () => {
  const receipts = [
    { request_id: "duplicate", status: "failed" },
    { request_id: "duplicate", status: "succeeded" },
  ];
  const before = receiptSelectionKeys(receipts);
  const after = receiptSelectionKeys([...receipts].reverse());
  expect(after[1]).toBe(before[0]);
  expect(after[0]).toBe(before[1]);
  expect(before).not.toContain("duplicate");
});

test("removing a row cannot hand its key to an ID-less row that shifts up", () => {
  const receipt = { status: "succeeded" };
  const alice = { person_label: "Alice", app_label: "Chat", created_at: 1 };
  const bob = { person_label: "Bob", app_label: "Images", created_at: 2 };
  const before = receiptSelectionKeys([receipt, { ...receipt }], [alice, bob]);
  expect(new Set(before).size).toBe(2);
  const after = receiptSelectionKeys([{ ...receipt }], [bob]);
  expect(after).not.toContain(before[0]);
  expect(after[0]).toBe(before[1]);
});

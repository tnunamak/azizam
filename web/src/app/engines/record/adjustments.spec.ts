import { describe, expect, it } from "vitest";
import type { TransformRow } from "@/lib/engines-api";
import {
  ADJUSTMENT_KINDS,
  cardsFromTransforms,
  newCard,
  sayAdjustment,
  transformsFromCards,
  type AdjustmentCard,
} from "./adjustments";

function roundTrip(rows: TransformRow[]) {
  return transformsFromCards(cardsFromTransforms(rows));
}

describe("request adjustments: stored rows <-> typed forms", () => {
  it("names the six primitives in plain words", () => {
    expect(ADJUSTMENT_KINDS.map((k) => k.name)).toEqual([
      "Replace values",
      "Rename fields",
      "Remove fields",
      "Set defaults",
      "Add a message",
      "Keep a number in range",
    ]);
  });

  it("round-trips every primitive, a built-in and unknown keys exactly", () => {
    const rows: TransformRow[] = [
      "reasoning-effort-final-chat",
      { kind: "value-map", param: "reasoning_effort", map: { xhigh: "high", minimal: "low", n: 3 }, default: "medium", phase: "request" },
      { kind: "param-map", from: "max_completion_tokens", to: "max_tokens" },
      { kind: "param-map", from: "a", to: "b", only_if_destination_absent: true, when: { model: "x" } },
      { kind: "param-map", from: "c", to: "d", phase: "response" },
      { kind: "drop-params", drop: ["logit_bias", "user"], phase: "request" },
      { kind: "default-params", set: { top_p: 0.9, stream: false, tag: "x" } },
      { kind: "message-inject", role: "system", text: "Be brief.", mode: "append-or-insert" },
      { kind: "message-inject", role: "user", text: "Hi" },
      { kind: "clamp", param: "temperature", min: 0, max: 1, phase: "request", when: { route: "chat" } },
      { kind: "clamp", param: "top_k", max: 40 },
    ];
    expect(roundTrip(rows)).toEqual(rows);
    expect(JSON.stringify(roundTrip(rows))).toBe(JSON.stringify(rows));
  });

  it("groups consecutive param-map rows with the same phase into one card", () => {
    const cards = cardsFromTransforms([
      { kind: "param-map", from: "a", to: "b" },
      { kind: "param-map", from: "c", to: "d" },
      { kind: "param-map", from: "e", to: "f", phase: "response" },
      { kind: "drop-params", drop: ["x"] },
      { kind: "param-map", from: "g", to: "h" },
    ]);
    expect(cards.map((c) => c.kind)).toEqual(["param-map", "param-map", "drop-params", "param-map"]);
    const first = cards[0] as Extract<AdjustmentCard, { kind: "param-map" }>;
    expect(first.rows.map((r) => [r.a, r.b])).toEqual([["a", "b"], ["c", "d"]]);
  });

  it("emits one param-map primitive per row, in order", () => {
    const card = newCard("param-map") as Extract<AdjustmentCard, { kind: "param-map" }>;
    card.rows = [
      { a: "max_completion_tokens", b: "max_tokens" },
      { a: "stop_sequences", b: "stop" },
    ];
    expect(transformsFromCards([card])).toEqual([
      { kind: "param-map", from: "max_completion_tokens", to: "max_tokens" },
      { kind: "param-map", from: "stop_sequences", to: "stop" },
    ]);
  });

  it("parses numbers and true/false in Set defaults, keeps other text", () => {
    const card = newCard("default-params") as Extract<AdjustmentCard, { kind: "default-params" }>;
    card.rows = [
      { a: "top_p", b: "0.9" },
      { a: "stream", b: "false" },
      { a: "user", b: "home" },
      { a: "", b: "ignored" },
    ];
    expect(transformsFromCards([card])).toEqual([
      { kind: "default-params", set: { top_p: 0.9, stream: false, user: "home" } },
    ]);
  });

  it("maps Where to the message mode and Applies to to the phase", () => {
    const msg = newCard("message-inject") as Extract<AdjustmentCard, { kind: "message-inject" }>;
    msg.text = "Answer in plain language.";
    msg.where = "end";
    const clamp = newCard("clamp") as Extract<AdjustmentCard, { kind: "clamp" }>;
    clamp.field = "temperature";
    clamp.min = "0";
    clamp.max = "1.5";
    clamp.phase = "response";
    const drop = newCard("drop-params") as Extract<AdjustmentCard, { kind: "drop-params" }>;
    drop.items = ["logit_bias", ""];
    const vm = newCard("value-map") as Extract<AdjustmentCard, { kind: "value-map" }>;
    vm.field = "reasoning_effort";
    vm.rows = [{ a: "xhigh", b: "high" }];
    expect(transformsFromCards([msg, clamp, drop, vm])).toEqual([
      { kind: "message-inject", role: "system", text: "Answer in plain language.", mode: "append" },
      { kind: "clamp", param: "temperature", min: 0, max: 1.5, phase: "response" },
      { kind: "drop-params", drop: ["logit_bias"] },
      { kind: "value-map", param: "reasoning_effort", map: { xhigh: "high" } },
    ]);
  });

  it("says what each adjustment does in a plain sentence", () => {
    const [vm, pm, drop, defs, msg, clamp] = cardsFromTransforms([
      { kind: "value-map", param: "reasoning_effort", map: { xhigh: "high", minimal: "low" } },
      { kind: "param-map", from: "max_completion_tokens", to: "max_tokens", phase: "response" },
      { kind: "drop-params", drop: ["logit_bias", "user"] },
      { kind: "default-params", set: { top_p: 0.9 } },
      { kind: "message-inject", role: "system", text: "Be brief.", mode: "prepend" },
      { kind: "clamp", param: "temperature", min: 0, max: 1 },
    ]);
    expect(sayAdjustment(vm)).toBe(
      "In requests, Azizam changes the value of “reasoning_effort”: “xhigh” becomes “high”; “minimal” becomes “low”.",
    );
    expect(sayAdjustment(pm)).toBe("In replies, Azizam renames “max_completion_tokens” to “max_tokens”.");
    expect(sayAdjustment(drop)).toBe("Azizam leaves “logit_bias”, “user” out of requests.");
    expect(sayAdjustment(defs)).toBe("Azizam adds “top_p” = “0.9” to requests that do not set it.");
    expect(sayAdjustment(msg)).toBe("Azizam adds a system message at the start of every chat: “Be brief.”.");
    expect(sayAdjustment(clamp)).toBe(
      "In requests, Azizam keeps “temperature” between 0 and 1. A value outside moves to the nearest limit.",
    );
  });

  it("shows a named built-in read-only in words", () => {
    const [card] = cardsFromTransforms(["reasoning-effort-final-chat"]);
    expect(card.kind).toBe("builtin");
    expect(sayAdjustment(card)).toBe("Built-in adjustment: reasoning effort final chat");
  });
});

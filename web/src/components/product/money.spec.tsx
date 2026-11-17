import { describe, expect, it } from "vitest";
import { Money } from "./money";

describe("Money", () => {
  it("does not display a tiny positive non-precise charge as zero", () => {
    expect(Money({ amount: 1e-7 })?.props.children).toBe("<$0.000001");
    expect(Money({ amount: 0.000001 })?.props.children).toBe("$0.000001");
  });

  it.each([
    [0, "$0.00"],
    [0.5, "$0.50"],
    [0.004, "$0.004"],
    [12345.67, "$12,345.67"],
  ] as const)("preserves ordinary amount %s", (amount, text) => {
    expect(Money({ amount })?.props.children).toBe(text);
  });

  it("preserves local, precise, missing and currency behavior", () => {
    expect(Money({ amount: 1e-7, local: true })?.props.children).toBe("Local · no charge");
    expect(Money({ amount: 2, local: true })?.props.children).toBe("Local · no charge");
    expect(Money({ amount: 1e-7, precise: true })?.props.children).toBe("<$0.0001");
    expect(Money({ amount: 0, precise: true })?.props.children).toBe("$0.00");
    expect(Money({ amount: 0.004, precise: true })?.props.children).toBe("$0.004");
    expect(Money({ amount: null })?.props.children).toBe("Not measured yet");
    expect(Money({ amount: NaN })?.props.children).toBe("Not measured yet");
    expect(Money({ amount: undefined, summary: true })).toBeNull();
    expect(Money({ amount: 1e-7, currency: "EUR" })?.props.children).toBe("<€0.000001");
  });
});

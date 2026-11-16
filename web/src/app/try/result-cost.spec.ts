import { describe, expect, it } from "vitest";
import { costFrom } from "./result-cost";

const response = (value: string) => new Response(null, {
  headers: { "x-litellm-response-cost": value },
});

describe("costFrom numeric display", () => {
  it.each(["cost", "remote_cost", "measured_cost"])("formats %s payload numbers consistently", key => {
    expect(costFrom({ [key]: 0.1 + 0.2 })).toBe("$0.30");
    expect(costFrom({ [key]: 0.004 })).toBe("$0.004000");
    expect(costFrom({ [key]: 0 })).toBe("$0.00");
    expect(costFrom({ [key]: { amount_usd: 0.1 + 0.2 } })).toBe("$0.30");
    expect(costFrom({ [key]: { amount_usd: 0.004 } })).toBe("$0.004000");
  });

  it.each([Infinity, NaN, -1])("omits invalid payload amount %s", amount => {
    expect(costFrom({ cost: amount })).toBeUndefined();
    expect(costFrom({ cost: { amount_usd: amount } })).toBeUndefined();
  });

  it("preserves header formatting and precedence", () => {
    expect(costFrom({ cost: 9 }, response("0.30000000000000004"))).toBe("$0.30");
    expect(costFrom({}, response("0.004"))).toBe("$0.004000");
    expect(costFrom({}, response("0"))).toBe("$0.00");
    expect(costFrom({}, response("12.5"))).toBe("$12.50");
    for (const value of ["Infinity", "NaN", "-1", "", " "])
      expect(costFrom({ cost: 0.5 }, response(value))).toBe("$0.50");
  });

  it("preserves local decisions, missing costs and text costs", () => {
    expect(costFrom({}, undefined, "Local")).toBe("Local · no charge");
    expect(costFrom({}, response("0"), "Local")).toBe("Local · no charge");
    expect(costFrom({ cost: 0.5 }, response("0"), "Local")).toBe("$0.50");
    expect(costFrom({ cost: 0.5 }, response("2"), "Local")).toBe("$2.00");
    expect(costFrom({})).toBeUndefined();
    expect(costFrom(null)).toBeUndefined();
    expect(costFrom({ cost: "Not measured" })).toBe("Not measured");
  });
});

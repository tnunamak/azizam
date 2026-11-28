import { describe, expect, it } from "vitest";
import { costFrom, localityLabel, PLAN_COST_LABEL, PLAN_LOCALITY } from "./result-cost";

const response = (value: string) => new Response(null, {
  headers: { "x-litellm-response-cost": value },
});

describe("costFrom numeric display", () => {
  it("shows a positive sub-micro-dollar charge from direct, nested and header amounts", () => {
    expect(costFrom({ cost: 1e-7 })).toBe("<$0.000001");
    expect(costFrom({ cost: { amount_usd: 1e-7 } })).toBe("<$0.000001");
    expect(costFrom({}, response("1e-7"))).toBe("<$0.000001");
  });

  it("keeps zero-header precedence for positive sub-micro-dollar payloads", () => {
    expect(costFrom({ cost: 1e-7 }, response("0"), "Local")).toBe("<$0.000001");
    expect(costFrom({ cost: { amount_usd: 1e-7 } }, response("0"), "Local")).toBe("<$0.000001");
    expect(costFrom({ cost: 1e-7 }, response("0"))).toBe("$0.00");
  });

  it.each([
    [0.000001, "$0.000001"],
    [0, "$0.00"],
  ])("keeps the boundary amount %s unchanged", (amount, expected) => {
    expect(costFrom({ cost: amount })).toBe(expected);
    expect(costFrom({ cost: { amount_usd: amount } })).toBe(expected);
    expect(costFrom({}, response(String(amount)))).toBe(expected);
  });

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

  it("labels uncharged subscription answers without hiding reported charges", () => {
    expect(PLAN_COST_LABEL).toBe("Subscription plan · no API charge");
    expect(costFrom({}, response("0"), PLAN_LOCALITY)).toBe(PLAN_COST_LABEL);
    expect(costFrom({}, undefined, PLAN_LOCALITY)).toBe(PLAN_COST_LABEL);
    expect(costFrom({ cost: { amount_usd: 0 } }, undefined, PLAN_LOCALITY)).toBe(PLAN_COST_LABEL);
    expect(costFrom({}, response("0.02"), PLAN_LOCALITY)).toBe("$0.02");
    expect(costFrom({ cost: 0.02 }, response("0"), PLAN_LOCALITY)).toBe("$0.02");
    expect(costFrom({ cost: { amount_usd: 0.02 } }, undefined, PLAN_LOCALITY)).toBe("$0.02");
    expect(costFrom({ cost: 0, remote_cost: { amount_usd: 0.02 } }, response("0"), PLAN_LOCALITY)).toBe("$0.02");
    expect(costFrom({ cost: "-1" }, response("-1"), PLAN_LOCALITY)).toBeUndefined();
    expect(costFrom({ cost: "bad", remote_cost: 0, measured_cost: { amount_usd: "0.03" } }, undefined, PLAN_LOCALITY)).toBe("$0.03");
    expect(costFrom({ cost: 0, remote_cost: 0.02 }, response("0.04"), PLAN_LOCALITY)).toBe("$0.04");
    expect(costFrom({ cost: -1 }, undefined, PLAN_LOCALITY)).toBeUndefined();
    expect(costFrom({}, response("NaN"), PLAN_LOCALITY)).toBeUndefined();
    expect(costFrom({}, response("0"), "Local")).toBe("Local · no charge");
    expect(costFrom({}, response("0"), "Remote")).toBe("$0.00");
  });

  it("ignores radix text for plan costs without masking positive decimal amounts", () => {
    expect(costFrom({ cost: 0 }, response("0x10"), PLAN_LOCALITY)).toBe(PLAN_COST_LABEL);
    expect(costFrom({ cost: "0b10" }, undefined, PLAN_LOCALITY)).toBe(PLAN_COST_LABEL);
    expect(costFrom({ cost: "0b10", remote_cost: { amount_usd: " +2e-2 " } }, undefined, PLAN_LOCALITY)).toBe("$0.02");
    expect(costFrom({ cost: " +2e-2 " }, undefined, PLAN_LOCALITY)).toBe("$0.02");
  });

  it("distinguishes an underflowing decimal plan charge from an actual zero", () => {
    expect(costFrom({ cost: "1e-400" }, undefined, PLAN_LOCALITY)).toBeUndefined();
    expect(costFrom({ measured_cost: { amount_usd: "-1e-400" } }, undefined, PLAN_LOCALITY)).toBeUndefined();
    expect(costFrom({}, response("1e-400"), PLAN_LOCALITY)).toBeUndefined();
    expect(costFrom({ cost: "0.0e-400" }, undefined, PLAN_LOCALITY)).toBe(PLAN_COST_LABEL);
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

describe("localityLabel", () => {
  it("names a plan-served catalog model so its answer reads as no API charge", () => {
    const record = { locality: "remote", provider: "openai", cost: { kind: "included", price: null } };
    expect(localityLabel(record)).toBe(PLAN_LOCALITY);
    expect(costFrom({}, undefined, localityLabel(record))).toBe(PLAN_COST_LABEL);
  });

  it("keeps the local and remote labels for models no plan serves", () => {
    expect(localityLabel({ locality: "local", cost: { kind: "free" } })).toBe("Local");
    expect(localityLabel({ locality: "remote", provider: "openai", cost: { kind: "paid" } })).toBe("Remote · openai");
    expect(localityLabel({ locality: "remote", provider: " " })).toBe("Remote");
    expect(localityLabel({})).toBe("");
  });
});

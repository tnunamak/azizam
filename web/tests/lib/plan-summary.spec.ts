import { describe, expect, it } from "vitest";
import {
  inviteeSummary,
  planCounts,
  planSummary,
} from "@/app/people/people-api";

describe("access plan summaries", () => {
  it("uses capability names and separates counts from permissions", () => {
    const policy = {
      allow: { intents: ["chat", "images"], exposure: ["local"] },
      models: ["demo-chat"],
      fallback: { cloud_allowed: false },
    };
    expect(planSummary(policy)).toBe(
      "Chat and images. Local models only. No paid providers. Selected models only.",
    );
    expect(planCounts(policy)).toBe("1 selected model");
  });

  it("keeps omitted capability and location restrictions explicit", () => {
    expect(planSummary({})).toBe(
      "Capabilities follow gateway defaults. Model locations follow gateway defaults. No paid providers. No cloud fallback.",
    );
    expect(planCounts({})).toBe("");
  });

  it("does not equate a blocked fallback with blocked explicit paid calls", () => {
    const summary = planSummary({
      allow: { intents: ["chat.completions"], exposure: ["remote"] },
      fallback: { cloud_allowed: false },
      remote: { providers: ["openrouter"], budget_usd_per_month: "10.00" },
    });
    expect(summary).toBe(
      "Chat. Remote models only. Paid providers: openrouter. No cloud fallback. Remote spending limit: $10.00 per month.",
    );
    expect(summary).not.toContain("No paid providers");
  });

  it("retains empty restrictions and operator-defined capabilities", () => {
    const summary = planSummary({
      allow: {
        intents: ["extension.run", "image.edit"],
        exposure: [],
        tags: [],
      },
      models: [],
      protocols: [],
      workflows: [],
      backends: [],
    });
    expect(summary).toContain("Extension run and image editing.");
    expect(summary).toContain("No model locations allowed.");
    expect(summary).toContain("No models allowed.");
    expect(summary).toContain("No app protocols allowed.");
    expect(summary).toContain("No workflows allowed.");
    expect(summary).toContain("No engines allowed.");
    expect(summary).toContain("Additional capability restrictions apply.");
    expect(planCounts({ models: [], protocols: ["openai", "anthropic"] })).toBe(
      "0 selected models · 2 selected protocols",
    );
  });

  it("keeps local restrictions even if remote permissions are saved", () => {
    const summary = planSummary({
      allow: { intents: [], exposure: ["local"] },
      remote: { providers: ["openrouter"] },
      fallback: { cloud_allowed: true },
    });
    expect(summary).toBe(
      "No capabilities allowed. Local models only. No paid providers.",
    );
  });
});

describe("what an invitation says to the invitee", () => {
  it("says nothing about fields the plan leaves at the gateway's defaults", () => {
    expect(inviteeSummary({})).toBe("No paid online services.");
    expect(inviteeSummary({})).not.toMatch(/capabilit|gateway defaults|protocol|fallback/i);
  });

  it("names what the invitee can do in plain verbs, leaving out app plumbing", () => {
    expect(
      inviteeSummary({
        allow: { intents: ["chat", "image.generate", "text.embeddings"], exposure: ["local"] },
        remote: { providers: [] },
      }),
    ).toBe("You can chat and make pictures. Everything runs on this gateway's own computer.");
  });

  it("says when some models cost money, without provider ids", () => {
    const summary = inviteeSummary({
      remote: { providers: ["household-remote"], budget_usd_per_day: "1" },
    });
    expect(summary).toBe("Some models are paid online services, within a spending limit.");
    expect(inviteeSummary({ remote: { providers: ["openrouter"] } })).toBe(
      "Some models are paid online services.",
    );
    expect(summary).not.toContain("household-remote");
  });
});

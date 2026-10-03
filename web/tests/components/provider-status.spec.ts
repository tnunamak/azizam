import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchProviderStatus, providerViews } from "@/lib/provider-status";

function mockFetch(response: Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => response),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("provider status contract", () => {
  it("hides only when the optional provider API returns 404", async () => {
    mockFetch(
      new Response(JSON.stringify({ error: { message: "not found" } }), {
        status: 404,
      }),
    );
    await expect(fetchProviderStatus()).resolves.toBeNull();
  });

  it("surfaces provider API failures", async () => {
    mockFetch(
      new Response(
        JSON.stringify({ error: { message: "provider probe failed" } }),
        { status: 503 },
      ),
    );
    await expect(fetchProviderStatus()).rejects.toThrow(
      "provider probe failed",
    );
  });

  it("rejects malformed successful responses", async () => {
    mockFetch(new Response("not-json", { status: 200 }));
    await expect(fetchProviderStatus()).rejects.toThrow(
      "Provider status returned an unsupported response.",
    );
    mockFetch(new Response("null", { status: 200 }));
    await expect(fetchProviderStatus()).rejects.toThrow(
      "Provider status returned an unsupported response.",
    );
    mockFetch(new Response(JSON.stringify({ items: [] }), { status: 200 }));
    await expect(fetchProviderStatus()).rejects.toThrow(
      "Provider status returned an unsupported response.",
    );
  });

  it("returns no views for pending or hidden provider status", () => {
    expect(providerViews(undefined)).toEqual([]);
    expect(providerViews(null)).toEqual([]);
  });

  it("normalizes provider status without carrying secret fields", () => {
    const views = providerViews({
      providers: [
        {
          id: "openrouter",
          name: "OpenRouter",
          enabled: true,
          reachable: true,
          key_set: true,
          spend_usd: 1.25,
          cap_usd: 10,
          api_key: "sk-secret-value",
          secret_ref: "OPENROUTER_API_KEY",
        },
      ],
    });

    expect(views).toEqual([
      {
        id: "openrouter",
        name: "OpenRouter",
        enabled: true,
        reachable: true,
        keyState: "set",
        spendText: "$1.25 of $10.00",
        tone: "ok",
      },
    ]);
    expect(JSON.stringify(views)).not.toContain("sk-secret-value");
    expect(JSON.stringify(views)).not.toContain("OPENROUTER_API_KEY");
  });
});

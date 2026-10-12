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

  it.each([
    {
      payload: [{ id: "fixture-provider", name: "Fixture provider", enabled: true }],
    },
    {
      payload: {
        providers: [{ id: "fixture-provider", name: "Fixture provider", enabled: true }],
      },
    },
  ])(
    "returns valid provider status and preserves its transport contract",
    async ({ payload }) => {
      const fetchMock = vi.fn(async () => Response.json(payload));
      vi.stubGlobal("fetch", fetchMock);
      await expect(fetchProviderStatus()).resolves.toEqual(payload);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledWith("/admin/api/providers", {
        credentials: "same-origin",
        cache: "no-store",
      });
    },
  );

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
        keySuffix: null,
        spendText: "$1.25 of $10.00",
        tone: "ok",
        monthlySpend: null,
        monthlyCap: null,
        checkedAt: null,
      },
    ]);
    expect(JSON.stringify(views)).not.toContain("sk-secret-value");
    expect(JSON.stringify(views)).not.toContain("OPENROUTER_API_KEY");
  });

  it("reads the production provider contract and reports monthly spend with its monthly cap", () => {
    expect(
      providerViews({
        providers: [
          {
            id: "openrouter",
            name: "OpenRouter",
            enabled: true,
            key_status: { state: "set", api_key_env: "OPENROUTER_API_KEY", suffix: "2c66" },
            spend: { daily_usd: 1.25, monthly_usd: 4.25, status: "known" },
            caps: { daily_usd: 5, monthly_usd: 10 },
          },
        ],
      }),
    ).toEqual([
      {
        id: "openrouter",
        name: "OpenRouter",
        enabled: true,
        reachable: null,
        keyState: "set",
        keySuffix: "2c66",
        spendText: "$4.25 of $10.00 this month",
        tone: "neutral",
        monthlySpend: 4.25,
        monthlyCap: 10,
        checkedAt: null,
      },
    ]);
  });

  it("uses the daily amount with the daily cap when monthly spend is absent", () => {
    const [provider] = providerViews({
      providers: [
        {
          id: "remote-one",
          name: "Remote One",
          spend: { daily_usd: 2.5, monthly_usd: null, status: "known" },
          caps: { daily_usd: 5, monthly_usd: 100 },
        },
      ],
    });

    expect(provider.spendText).toBe("$2.50 of $5.00 today");
  });

  it("does not display stale amounts when spend status is unknown", () => {
    const [provider] = providerViews({
      providers: [
        {
          id: "remote-one",
          spend: { daily_usd: 2, monthly_usd: 99, status: "unknown" },
          caps: { daily_usd: 5, monthly_usd: 100 },
          spend_usd: 88,
          cap_usd: 90,
        },
      ],
    });

    expect(provider.spendText).toBe("Not measured yet");
    expect(provider.spendText).not.toContain("99");
    expect(provider.spendText).not.toContain("88");
    expect(provider.monthlySpend).toBeNull();
  });

  it("uses a generic display name when the provider name is missing or only its ID", () => {
    expect(
      providerViews({
        providers: [
          { id: "provider-one", name: "provider-one" },
          { id: "provider-two" },
          {
            id: "provider-three",
            name: "provider-three",
            label: "Remote Three",
          },
        ],
      }).map(({ id, name }) => [id, name]),
    ).toEqual([
      ["provider-one", "Remote provider"],
      ["provider-two", "Remote provider"],
      ["provider-three", "Remote Three"],
    ]);
  });
});

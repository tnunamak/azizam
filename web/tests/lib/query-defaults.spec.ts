import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { focusManager, QueryObserver } from "@tanstack/react-query";
import { createQueryClient } from "@/lib/api";
import { providerStatusInterval } from "@/lib/provider-status";

describe("client fetching defaults", () => {
  let client: ReturnType<typeof createQueryClient>;
  let subscriptions: Array<() => void>;
  let visibility: DocumentVisibilityState;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    visibility = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibility,
    );
    focusManager.setFocused(undefined);
    client = createQueryClient();
    subscriptions = [];
    client.mount();
  });

  afterEach(() => {
    subscriptions.forEach((unsubscribe) => unsubscribe());
    client.unmount();
    client.clear();
    vi.restoreAllMocks();
    focusManager.setFocused(undefined);
    vi.useRealTimers();
  });

  it("defines bounded freshness and foreground-only polling", () => {
    expect(client.getDefaultOptions().queries).toMatchObject({
      staleTime: 30_000,
      refetchIntervalInBackground: false,
    });
    expect(client.getDefaultOptions().mutations?.retry).toBe(false);
  });

  it("reuses fresh navigation reads", async () => {
    const queryFn = vi.fn(async () => ({ ok: true }));
    const options = { queryKey: ["navigation-regression"], queryFn };
    await client.fetchQuery(options);
    await vi.advanceTimersByTimeAsync(20_000);
    await client.fetchQuery(options);
    expect(queryFn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_001);
    await client.fetchQuery(options);
    expect(queryFn).toHaveBeenCalledTimes(2);
  });

  it("pauses hidden polling and refreshes on return", async () => {
    expect(
      client.getDefaultOptions().queries?.refetchIntervalInBackground,
    ).toBe(false);
    const queryFn = vi.fn(async () => ({ ok: true }));
    const observer = new QueryObserver(client, {
      queryKey: ["visibility-regression"],
      queryFn,
      refetchInterval: 30_000,
    });
    subscriptions.push(observer.subscribe(() => {}));
    await vi.advanceTimersByTimeAsync(0);
    expect(queryFn).toHaveBeenCalledTimes(1);
    visibility = "hidden";
    window.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(90_000);
    expect(queryFn).toHaveBeenCalledTimes(1);
    visibility = "visible";
    window.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(queryFn).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(queryFn).toHaveBeenCalledTimes(3);
  });

  it("backs off idle providers without delaying live checks", () => {
    expect(providerStatusInterval(null)).toBe(300_000);
    expect(providerStatusInterval([])).toBe(60_000);
    expect(
      providerStatusInterval({ providers: [{ enabled: false }] }),
    ).toBe(60_000);
    expect(
      providerStatusInterval({ providers: [{ enabled: true, reachable: false }] }),
    ).toBe(30_000);
    expect(providerStatusInterval({ providers: [{}] })).toBe(30_000);
    expect(providerStatusInterval(undefined)).toBe(30_000);
  });
});

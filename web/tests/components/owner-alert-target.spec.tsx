import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { queryKeys, type CurrentHealth } from "@/lib/api";
import { useOwnerAlertTarget } from "@/components/shell/use-owner-alert";

const mocks = vi.hoisted(() => ({
  query: vi.fn(),
  providers: vi.fn(),
}));
vi.mock("@tanstack/react-query", async (original) => ({
  ...await original<typeof import("@tanstack/react-query")>(),
  useQuery: mocks.query,
}));
vi.mock("@/lib/provider-status", async (original) => ({
  ...await original<typeof import("@/lib/provider-status")>(),
  useOptionalProviderStatus: mocks.providers,
}));

const health: CurrentHealth = {
  state: "warn",
  tone: "warn",
  label: "Needs attention",
  detail: "",
  observation_available: true,
  services: [{
    service_id: "fixture-engine",
    name: "Fixture engine",
    configured: true,
    enabled: true,
    in_use: true,
    role: "required",
    routable: false,
    observation: "down",
    tone: "bad",
    last_error: null,
  }],
};
let healthError = false;
let currentHealth: CurrentHealth | undefined;
type QueryOptions = {
  queryKey: readonly unknown[];
  enabled: boolean;
  refetchInterval?: number;
};
function configureProviders(state = "ready") {
  mocks.providers.mockReturnValue({
    state,
    providers: [{
      id: "fixture-provider",
      name: "Fixture provider",
      enabled: true,
      reachable: false,
      keyState: "set",
      keySuffix: null,
      spendText: "",
      tone: "bad",
    }],
  });
}
beforeEach(() => {
  vi.clearAllMocks();
  healthError = false;
  currentHealth = health;
  configureProviders();
  mocks.query.mockImplementation((options: QueryOptions) => {
    if (JSON.stringify(options.queryKey) === JSON.stringify(queryKeys.serviceRegistry)) {
      return { data: { schema_version: 1, services: {} }, isError: false };
    }
    if (JSON.stringify(options.queryKey) === JSON.stringify(queryKeys.status)) {
      return { data: { current_health: currentHealth }, isError: healthError };
    }
    throw new Error("Unexpected query");
  });
});
afterEach(cleanup);

it("disables both queries and the provider hook for a disabled caller", () => {
  const { result } = renderHook(() => useOwnerAlertTarget(false));
  expect(result.current).toBeNull();
  expect(mocks.providers).toHaveBeenCalledWith(false);
  const options = mocks.query.mock.calls.map(([value]) => value as QueryOptions);
  expect(options).toHaveLength(2);
  expect(options.every((value) => value.enabled === false)).toBe(true);
});

it("prefers current engine evidence, then switches to the provider after recovery", () => {
  const { result, rerender } = renderHook(() => useOwnerAlertTarget(true));
  expect(result.current).toEqual({
    message: "Fixture engine can’t take requests right now.",
    section: "Engines",
  });
  currentHealth = { ...health, services: [] };
  rerender();
  expect(result.current).toEqual({
    message: "Fixture provider can’t take requests right now.",
    section: "Providers",
  });
  const options = mocks.query.mock.calls.map(([value]) => value as QueryOptions);
  expect(options.every((value) => value.enabled)).toBe(true);
  expect(options.find((value) =>
    JSON.stringify(value.queryKey) === JSON.stringify(queryKeys.status),
  )?.refetchInterval).toBe(30_000);
});

it("ignores retained engine data after a health error", () => {
  healthError = true;
  const { result } = renderHook(() => useOwnerAlertTarget(true));
  expect(result.current?.section).toBe("Providers");
  expect(result.current?.message).toBe(
    "Fixture provider can’t take requests right now.",
  );
});

it.each(["hidden", "pending", "error", "unavailable"])(
  "ignores provider rows outside ready state: %s",
  (state) => {
    currentHealth = undefined;
    configureProviders(state);
    const { result } = renderHook(() => useOwnerAlertTarget(true));
    expect(result.current).toBeNull();
  },
);

it("removes an already visible result when access is disabled", () => {
  const { result, rerender } = renderHook(
    ({ enabled }) => useOwnerAlertTarget(enabled),
    { initialProps: { enabled: true } },
  );
  expect(result.current?.section).toBe("Engines");
  rerender({ enabled: false });
  expect(result.current).toBeNull();
  expect(mocks.providers).toHaveBeenLastCalledWith(false);
  const last = mocks.query.mock.calls.slice(-2);
  expect(last.every(([value]) => (value as QueryOptions).enabled === false)).toBe(true);
});

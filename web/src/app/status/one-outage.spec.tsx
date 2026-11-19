import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, api, type ServiceRegistry, type StatusPayload } from "@/lib/api";
import { StatusClient } from "./status-client";

// One outage, one error: when the gateway stops answering, every source on the
// page fails together. The page says so once and offers one "Try again".
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("next/link", () => ({
  default: ({ children, href }: React.ComponentProps<"a">) => <a href={href}>{children}</a>,
}));
const down = vi.hoisted(() => ({
  error: null as unknown,
  /** The hook-backed sources that fail; the rest answer. */
  only: null as null | "providers",
}));
const refetches = vi.hoisted(() => ({
  activity: vi.fn(),
  model: vi.fn(),
  providers: vi.fn(),
}));
vi.mock("@/lib/use-capability-views", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/use-capability-views")>()),
  useActivitySample: () => ({
    data: down.only === null ? undefined : [],
    isPending: false,
    isError: down.error !== null && down.only === null,
    isFetching: false,
    error: down.error,
    refetch: refetches.activity,
  }),
  useCapabilityViews: () => ({
    home: undefined,
    views: [],
    verdict: { tone: "unknown", text: "Checking", problems: [], unconfirmed: 0, checkedAt: null },
    requests: [],
    isPending: false,
    isError: down.error !== null && down.only === null,
    isFetching: false,
    error: down.error,
    ownerHealthMissing: true,
    ownerHealthError: true,
    refetch: refetches.model,
  }),
}));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () =>
    down.error === null || (down.only !== null && down.only !== "providers")
      ? { state: "hidden", providers: [], raw: null, message: "", refetch: refetches.providers }
      : { state: "error", providers: [], raw: null, message: "", error: down.error, refetch: refetches.providers },
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  down.error = null;
  down.only = null;
  Object.values(refetches).forEach((fn) => fn.mockClear());
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <StatusClient />
    </QueryClientProvider>,
  );
}

it("shows one error and one Try again when every source fails with a 503", async () => {
  down.error = new ApiError("upstream_unavailable", "Unavailable for this test", 503);
  const status = vi.spyOn(api, "status").mockRejectedValue(down.error);
  const registry = vi.spyOn(api.services, "registry").mockRejectedValue(down.error);
  vi.spyOn(api, "overview").mockRejectedValue(down.error);
  mount();
  const heading = await screen.findByRole("heading", { name: "Couldn’t check status" });
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  const alert = heading.closest<HTMLElement>('[role="alert"]') as HTMLElement;
  expect(within(alert).getByText("The gateway didn’t answer. Nothing was changed. Try again.")).toBeVisible();
  // The server's own words stay behind "Technical details".
  expect(screen.queryByText("Unavailable for this test")).toBeNull();
  expect(within(alert).getByText(/HTTP 503/)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: /try again|retry/i })).toHaveLength(1);
  expect(screen.queryByText(/Retry (recent requests|capabilities|providers)/)).toBeNull();
  expect(document.body.textContent).not.toMatch(/Engine (health|list) couldn’t load/);

  status.mockClear();
  registry.mockClear();
  await userEvent.click(within(alert).getByRole("button", { name: /try again/i }));
  expect(status).toHaveBeenCalled();
  expect(registry).toHaveBeenCalled();
  expect(refetches.activity).toHaveBeenCalledOnce();
  expect(refetches.model).toHaveBeenCalledOnce();
  expect(refetches.providers).toHaveBeenCalledOnce();
});

it("groups a network failure the same way", async () => {
  down.error = new TypeError("Failed to fetch");
  vi.spyOn(api, "status").mockRejectedValue(down.error);
  vi.spyOn(api.services, "registry").mockRejectedValue(down.error);
  vi.spyOn(api, "overview").mockRejectedValue(down.error);
  mount();
  await screen.findByRole("heading", { name: "Couldn’t check status" });
  expect(screen.getAllByRole("alert")).toHaveLength(1);
});

it("keeps a lone failure in its own section when its siblings loaded", async () => {
  vi.spyOn(api, "status").mockResolvedValue({
    current_health: { state: "ok", services: [], observation_available: true },
    backends: [],
    generated_at: 1,
  } as unknown as StatusPayload);
  vi.spyOn(api.services, "registry").mockResolvedValue({ schema_version: 1, services: {} } as ServiceRegistry);
  vi.spyOn(api, "overview").mockResolvedValue(undefined as never);
  down.error = new ApiError("upstream_unavailable", "x", 503);
  down.only = "providers";
  mount();
  const heading = await screen.findByRole("heading", { name: "Couldn’t load provider status" });
  expect(screen.queryByRole("heading", { name: "Couldn’t check status" })).toBeNull();
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  await userEvent.click(within(heading.closest<HTMLElement>('[role="alert"]') as HTMLElement).getByRole("button", { name: "Try again to load provider status" }));
  expect(refetches.providers).toHaveBeenCalledOnce();
  expect(refetches.model).not.toHaveBeenCalled();
});

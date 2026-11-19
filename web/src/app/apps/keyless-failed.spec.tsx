import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AppsPage from "./page";
import { accountApi, type AccountSnapshot } from "@/lib/account-api";
import { ApiError, adminFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams("") }));
vi.mock("@/lib/account-api", () => ({
  accountApi: { session: vi.fn(), discovery: vi.fn(), mcpConnections: vi.fn() },
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));

const app = (id: string, name: string) => ({
  id,
  name,
  kind: "service",
  disabled_at: null,
  created_at: 1800000000,
  access_plan: null,
  can_mint_keys: true,
  credentials: [],
});
const snapshot: AccountSnapshot = {
  authenticated: true,
  csrf_token: "test",
  user: { id: "u", email: "o@example.test", name: "Owner", role: "owner" },
  apps: [app("one", "Hall display"), app("two", "Study display")],
};
const down = () => new ApiError("unavailable", "Unavailable for this test", 503);

beforeEach(() => {
  vi.mocked(accountApi.session).mockResolvedValue(snapshot);
  vi.mocked(accountApi.discovery).mockResolvedValue({ recipes: {} });
  vi.mocked(accountApi.mcpConnections).mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderApps() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AppsPage />
    </QueryClientProvider>,
  );
}

it("says it once, with one retry, when one app's keyless access did not load", async () => {
  vi.mocked(accountApi.session).mockResolvedValue({ ...snapshot, apps: [app("one", "Hall display")] });
  let failing = true;
  vi.mocked(adminFetch).mockImplementation(async () => {
    if (failing) throw down();
    return { trusted_addresses: [] };
  });
  renderApps();
  const line = await screen.findByText("Keyless access didn’t load. Nothing was changed.");
  const row = line.closest('[role="alert"]') as HTMLElement;
  failing = false;
  await userEvent.click(within(row).getByRole("button", { name: "Try again to load keyless access" }));
  await waitFor(() => expect(screen.queryByText(/Keyless access didn’t load/)).toBeNull());
});

it("shows one page-level error, not one line per app, when several reads fail together", async () => {
  vi.mocked(adminFetch).mockRejectedValue(down());
  renderApps();
  // Two apps failed with a 503: the gateway is down, so the page says so once.
  const heading = await screen.findByRole("heading", { name: "Couldn’t load some app details" });
  await waitFor(() => expect(screen.getAllByText("Keyless access didn’t load. Nothing was changed.")).toHaveLength(1));
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  const retry = screen.getAllByRole("button", { name: /^Try again/ });
  expect(retry).toHaveLength(1);
  expect(heading.closest('[role="alert"]')).toContainElement(retry[0]);
  vi.mocked(adminFetch).mockResolvedValue({ trusted_addresses: [] });
  await userEvent.click(retry[0]);
  await waitFor(() => expect(screen.queryByRole("heading", { name: "Couldn’t load some app details" })).toBeNull());
});

it("also folds the signed-in apps failure into the one error", async () => {
  vi.mocked(adminFetch).mockRejectedValue(down());
  vi.mocked(accountApi.mcpConnections).mockRejectedValue(down());
  renderApps();
  await screen.findByRole("heading", { name: "Couldn’t load some app details" });
  await waitFor(() => expect(screen.getByText("Couldn’t load signed-in apps.")).toBeVisible());
  expect(screen.getAllByRole("alert")).toHaveLength(1);
});

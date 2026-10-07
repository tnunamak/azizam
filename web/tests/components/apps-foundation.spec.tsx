import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  discovery: vi.fn(),
  session: vi.fn(),
  createApp: vi.fn(),
  mintKey: vi.fn(),
  revokeKey: vi.fn(),
  search: new URLSearchParams(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.search }));
vi.mock("@/lib/account-api", () => ({ accountApi: mocks }));
import AppsPage from "@/app/apps/page";

const app = {
  id: "owned-app",
  name: "Laptop",
  kind: "personal",
  disabled_at: null,
  created_at: 1,
  can_mint_keys: true,
  profile: { name: "Local access" },
  credentials: [{ id: "old-key", last_used_at: null }],
};
function show() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AppsPage />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  mocks.discovery.mockResolvedValue({
    recipes: {
      openai: { canonical_base_url: "https://gateway.example/openai/v1" },
    },
  });
  mocks.search = new URLSearchParams();
  mocks.session.mockResolvedValue({
    authenticated: true,
    csrf_token: "test",
    apps: [app],
  });
  mocks.mintKey.mockResolvedValue({ api_key: "test-reveal-once-value" });
  mocks.revokeKey.mockResolvedValue({});
});
afterEach(cleanup);
describe("Apps command destinations", () => {
  it("opens key creation from the command palette after the session loads", async () => {
    mocks.search = new URLSearchParams("action=create-key");
    show();
    expect(
      await screen.findByRole("dialog", { name: "New app" }),
    ).toBeVisible();
    expect(screen.getByLabelText("App name")).toHaveFocus();
  });
  it("focuses and marks the app named by an object jump", async () => {
    mocks.search = new URLSearchParams("app=owned-app");
    show();
    await waitFor(() =>
      expect(document.getElementById("app-owned-app")).toHaveFocus(),
    );
    expect(document.getElementById("app-owned-app")).toHaveClass("ring-2");
  });
  it("keeps the previous key working until the replacement is saved", async () => {
    const user = userEvent.setup();
    show();
    await user.click(
      await screen.findByRole("button", { name: "Actions for Laptop" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Rotate keys" }));
    await user.click(
      screen.getByRole("button", { name: "Create replacement key" }),
    );
    expect(
      await screen.findByDisplayValue("test-reveal-once-value"),
    ).toBeVisible();
    expect(
      screen.getByDisplayValue("https://gateway.example/openai/v1"),
    ).toBeVisible();
    expect(mocks.revokeKey).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "I’ve saved it" }));
    await waitFor(() =>
      expect(mocks.revokeKey).toHaveBeenCalledWith("old-key"),
    );
    expect(
      screen.queryByDisplayValue("test-reveal-once-value"),
    ).not.toBeInTheDocument();
  });
});

describe("App creation dialog sizing", () => {
  it("limits creation dialog height and allows vertical scrolling", async () => {
    const user = userEvent.setup();
    show();
    await screen.findByRole("button", { name: "Actions for Laptop" });
    await user.click(screen.getByRole("button", { name: "New app" }));
    expect(await screen.findByRole("dialog", { name: "New app" })).toHaveClass(
      "max-h-[calc(100dvh-2rem)]",
      "overflow-y-auto",
    );
  });
});

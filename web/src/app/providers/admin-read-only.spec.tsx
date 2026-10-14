import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import ProvidersPage from "./page";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const viewer = (role: "owner" | "admin"): Viewer => ({
  authenticated: true,
  name: "Someone",
  email: null,
  role,
  admin: true,
});

const provider = {
  id: "provider-openai",
  name: "OpenAI",
  enabled: true,
  reachable: true,
  models: [{ id: "openai/gpt-4.1-nano", name: "Gpt 4.1 Nano", enabled: true }],
  access_plans: [],
  spend: {
    daily_usd: 1,
    monthly_usd: 1,
    status: "needs_reconciliation",
    pending_reconciliation_count: 1,
  },
};

function renderAs(role: "owner" | "admin", providers: unknown[] = [provider]) {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(["providers"], {
    global: { caps: { daily_usd: 12, monthly_usd: 120 } },
    providers,
  });
  client.setQueryData(["providers", "catalog"], []);
  client.setQueryData(["providers", "plans"], []);
  render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={viewer(role)}>
        <ProvidersPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

const WRITE_BUTTONS = [
  "Add provider",
  "Edit models",
  "Edit caps",
  "Check provider connection",
  "Check charges",
  "Disable provider",
];

it("shows an administrator the providers and caps read-only with one note", () => {
  renderAs("admin");
  expect(screen.getByText("Only the owner can change this.")).toBeVisible();
  // They can still read what is set.
  expect(screen.getByText("Global spend cap")).toBeVisible();
  expect(screen.getAllByText("openai/gpt-4.1-nano").length).toBeGreaterThan(0);
  // None of the controls that now answer 403 are offered.
  for (const name of WRITE_BUTTONS) {
    expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
  }
  expect(screen.queryByText("Edit", { exact: true })).not.toBeInTheDocument();
  expect(screen.queryByText("Save global cap")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /More actions for OpenAI/ }),
  ).not.toBeInTheDocument();
});

it("shows an administrator an empty provider list without an add button", () => {
  renderAs("admin", []);
  expect(screen.getByText("No providers yet")).toBeVisible();
  expect(screen.getByText("Only the owner can change this.")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Add provider" }),
  ).not.toBeInTheDocument();
});

it("gives the owner every provider and cap control and no read-only note", () => {
  renderAs("owner");
  expect(
    screen.queryByText("Only the owner can change this."),
  ).not.toBeInTheDocument();
  for (const name of WRITE_BUTTONS) {
    expect(screen.getByRole("button", { name })).toBeInTheDocument();
  }
  expect(screen.getByText("Edit", { exact: true })).toBeInTheDocument();
});

import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProvidersPage from "./page";

vi.mock("@/lib/api", () => ({ adminFetch: vi.fn() }));
afterEach(cleanup);

it("shows exact provider model ids and discloses provider identity", () => {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity } },
  });
  client.setQueryData(["providers"], {
    global: { caps: { daily_usd: 12, monthly_usd: 120 } },
    providers: [
      {
        id: "provider-openai",
        name: "OpenAI",
        enabled: true,
        reachable: true,
        models: [
          { id: "openai/gpt-4.1-nano", name: "Gpt 4.1 Nano", enabled: true },
        ],
        allowed_plans: [],
      },
    ],
  });
  client.setQueryData(["providers", "catalog"], []);
  client.setQueryData(["providers", "plans"], []);
  render(
    <QueryClientProvider client={client}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
  expect(screen.getAllByText("openai/gpt-4.1-nano")).toHaveLength(2);
  expect(screen.queryByText("Gpt 4.1 Nano")).not.toBeInTheDocument();
  expect(
    screen.queryByText("Plans allowed by configured providers"),
  ).not.toBeInTheDocument();
  const identity = screen.getByText("Provider ID: provider-openai");
  expect(identity.closest("details")).not.toHaveAttribute("open");
  screen.getByText("Manage", { exact: true }).click();
  expect(screen.getByText("Provider details")).toBeInTheDocument();
  screen.getByText("Edit", { exact: true }).click();
  expect(
    screen.getByLabelText("Global daily provider cap (USD)"),
  ).toBeVisible();
  expect(
    screen.getByLabelText("Global monthly provider cap (USD)"),
  ).toBeVisible();
  expect(screen.getByText("Input: USD per 1 million tokens")).toBeVisible();
  expect(screen.getByText("Output: USD per 1 million tokens")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Check provider connection" }),
  ).toBeVisible();
  expect(
    screen.getByText("No access plans can use this provider"),
  ).toBeVisible();
  expect(screen.queryByText("Gateway check")).not.toBeInTheDocument();
});

it.each([
  { spent: null, cap: 10, label: "Spend unknown" },
  { spent: 0, cap: null, label: "No cap set" },
  { spent: null, cap: null, label: "Spend unknown · No cap set" },
])("SE-25 omits the percentage bar for $label", ({ spent, cap, label }) => {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  client.setQueryData(["providers"], {
    providers: [{
      id: "cost-provider", name: "Cost provider", models: [], allowed_plans: [],
      spend: { daily_usd: spent, monthly_usd: spent },
      caps: { daily_usd: cap, monthly_usd: cap },
    }],
  });
  client.setQueryData(["providers", "catalog"], []);
  client.setQueryData(["providers", "plans"], []);
  render(<QueryClientProvider client={client}><ProvidersPage /></QueryClientProvider>);
  const provider = screen.getByText("Provider ID: cost-provider").closest("article")!;
  expect(provider.querySelector('[role="progressbar"]')).toBeNull();
  expect(provider.textContent).toContain(label);
});

it("SE-25 distinguishes positive spend over a zero cap from recorded zero spend", () => {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  client.setQueryData(["providers"], {
    providers: [{
      id: "cost-provider", name: "Cost provider", models: [], allowed_plans: [],
      spend: { daily_usd: 0, monthly_usd: 1 },
      caps: { daily_usd: 0, monthly_usd: 0 },
    }],
  });
  client.setQueryData(["providers", "catalog"], []);
  client.setQueryData(["providers", "plans"], []);
  render(<QueryClientProvider client={client}><ProvidersPage /></QueryClientProvider>);
  const provider = screen.getByText("Provider ID: cost-provider").closest("article")!;
  expect(provider.querySelector('[aria-label="Today spend"]')).toHaveAttribute("aria-valuenow", "0");
  const monthly = provider.querySelectorAll('[aria-label="This month spend"]');
  expect(monthly).toHaveLength(2);
  for (const bar of monthly) expect(bar).toHaveAttribute("aria-valuenow", "100");
});

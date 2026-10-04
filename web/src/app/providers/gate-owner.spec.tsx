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

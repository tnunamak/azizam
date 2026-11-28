import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import ProvidersPage from "./page";

afterEach(cleanup);

const viewer = (role: "owner" | "admin"): Viewer => ({ authenticated: true, name: "Someone", email: null, role, admin: true });

function renderPage(subscriptions: unknown[], role: "owner" | "admin" = "owner") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, enabled: false } } });
  client.setQueryData(["providers"], { providers: [] });
  client.setQueryData(["providers", "catalog"], []);
  client.setQueryData(["providers", "plans"], []);
  client.setQueryData(["providers", "connections"], { connections: [
    { id: "account-owner", label: "Connected account", available: true },
  ] });
  client.setQueryData(["subscriptions"], { subscriptions });
  render(<QueryClientProvider client={client}><ViewerContext.Provider value={viewer(role)}><ProvidersPage /></ViewerContext.Provider></QueryClientProvider>);
}

it("inspects connection route pins from the common owner status", () => {
  renderPage([
    { kind: "account", display_name: "Account", connection_id: "account-owner", status: "connected", models: ["model"], routes: [
      { id: "account/model", logical_model: "model", service_id: "account", model_id: "model" },
    ] },
  ]);
  const region = screen.getByRole("region", { name: "Subscriptions" });
  fireEvent.click(within(region).getByRole("button", { name: "Open Account subscription" }));
  expect(within(region).getByDisplayValue("account/model")).toBeInTheDocument();
  expect(within(region).getByRole("button", { name: "Copy the pinned name for model" })).toBeInTheDocument();
});

it("lists a kind that was never connected, with a Connect button", () => {
  renderPage([{ kind: "account", display_name: "Account", status: "not_connected" }]);
  const region = screen.getByRole("region", { name: "Subscriptions" });
  expect(within(region).getByText("Account subscription")).toBeInTheDocument();
  expect(within(region).getAllByText("Off").length).toBeGreaterThan(0);
  expect(within(region).getByRole("button", { name: "Connect Account subscription" })).toBeInTheDocument();
  expect(within(region).queryByText("account-owner")).not.toBeInTheDocument();
});

it("leaves the subscriptions to the owner: an admin who is not the owner sees none", () => {
  renderPage([{ kind: "account", display_name: "Account", status: "not_connected" }], "admin");
  expect(screen.queryByRole("region", { name: "Subscriptions" })).not.toBeInTheDocument();
});

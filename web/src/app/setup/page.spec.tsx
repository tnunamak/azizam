import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SetupPage from "./page";

type State = {
  owner_exists: boolean;
  oidc_enabled: boolean;
  can_recover: boolean;
};
let client: QueryClient;
function serve(state: State | null) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(state ?? {}), {
          status: state ? 200 : 503,
        }),
      ),
    ),
  );
}
function mount() {
  return render(
    <QueryClientProvider client={client}>
      <SetupPage />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.unstubAllGlobals();
});

it("creates the owner account on a gateway with no owner yet", async () => {
  serve({ owner_exists: false, oidc_enabled: false, can_recover: true });
  mount();
  expect(await screen.findByLabelText("Email or username")).toBeVisible();
  expect(
    screen.getByRole("heading", { level: 1, name: "Set up your gateway" }),
  ).toBeVisible();
  expect(screen.getByLabelText("Confirm password")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Create owner account" }),
  ).toBeVisible();
  expect(
    screen.getByLabelText("Email or username").closest("form"),
  ).toHaveAttribute("action", "/setup/local-owner");
});

it("signs the existing owner in without asking to confirm the password", async () => {
  serve({ owner_exists: true, oidc_enabled: false, can_recover: true });
  mount();
  expect(await screen.findByRole("button", { name: "Sign in" })).toBeVisible();
  expect(
    screen.getByRole("heading", { level: 1, name: "Sign in to your gateway" }),
  ).toBeVisible();
  expect(screen.queryByLabelText("Confirm password")).toBeNull();
  expect(
    screen.getByLabelText("Email or username").closest("form"),
  ).toHaveAttribute("action", "/setup/local-login");
});

it("keeps the recovery key form behind the recovery disclosure", async () => {
  serve({ owner_exists: true, oidc_enabled: false, can_recover: true });
  mount();
  const summary = await screen.findByText("Recovery and advanced setup");
  const details = summary.closest("details")!;
  expect(details).not.toHaveAttribute("open");
  await userEvent.click(summary);
  expect(details).toHaveAttribute("open");
  expect(screen.getByLabelText("Recovery admin key")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Continue with recovery key" }),
  ).toBeVisible();
});

it("explains that browser recovery is off when the host does not allow it", async () => {
  serve({ owner_exists: true, oidc_enabled: false, can_recover: false });
  mount();
  await userEvent.click(await screen.findByText("Recovery and advanced setup"));
  expect(screen.getByText(/Browser recovery is not enabled/)).toBeVisible();
  expect(screen.queryByLabelText("Recovery admin key")).toBeNull();
});

it("uses configured single sign-on without a password form", async () => {
  serve({ owner_exists: false, oidc_enabled: true, can_recover: false });
  mount();
  expect(
    await screen.findByRole("link", { name: "Continue with single sign-on" }),
  ).toHaveAttribute("href", "/login?return_to=/setup/");
  expect(screen.queryByLabelText("Password")).toBeNull();
});

it("offers a retry that loads the options again after a failure", async () => {
  serve(null);
  mount();
  const retry = await screen.findByRole("button", { name: "Try again" });
  expect(screen.getByRole("heading", { level: 1 })).toBeVisible();
  expect(screen.queryByLabelText("Email or username")).toBeNull();
  serve({ owner_exists: false, oidc_enabled: false, can_recover: true });
  await userEvent.click(retry);
  expect(await screen.findByLabelText("Email or username")).toBeVisible();
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { adminFetch } from "@/lib/api";
import ProvidersPage from "./page";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

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
  { spent: null, cap: 10, label: "Cost not recordedCap USD 10.00" },
  { spent: 0, cap: null, label: "No cap set" },
  { spent: null, cap: null, label: "Cost not recordedNo cap set" },
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
  const provider = screen.getByText("Provider ID: cost-provider").closest("li")!;
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
  const provider = screen.getByText("Provider ID: cost-provider").closest("li")!;
  expect(provider.querySelector('[aria-label="Today spend"]')).toHaveAttribute("aria-valuenow", "0");
  const monthly = provider.querySelectorAll('[aria-label="This month spend"]');
  // A sole provider shows its overview once (no collapsed summary copy).
  expect(monthly).toHaveLength(1);
  for (const bar of monthly) expect(bar).toHaveAttribute("aria-valuenow", "100");
});

// WP-2: safe key entry, state reset and honest connection states.

const adminFetchMock = vi.mocked(adminFetch);

function renderProviders(
  providers: unknown[],
  {
    catalog = [] as unknown[],
    plans = [] as unknown[],
  }: { catalog?: unknown[]; plans?: unknown[] } = {},
) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { staleTime: Infinity, retry: false },
      mutations: { retry: false },
    },
  });
  // A mutation refetches the provider list; answer with the current data.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json(client.getQueryData(["providers"]))),
  );
  client.setQueryData(["providers"], { providers });
  client.setQueryData(["providers", "catalog"], catalog);
  client.setQueryData(["providers", "plans"], plans);
  render(
    <QueryClientProvider client={client}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
  return client;
}

const savedProvider = {
  id: "saved",
  name: "Saved provider",
  enabled: true,
  reachable: null,
  key_status: { state: "set", source: "secret", suffix: "ab12" },
  models: [{ id: "saved-model", price_input: "1", price_output: "2" }],
  allowed_plans: [],
  caps: { daily_usd: 5, monthly_usd: 50 },
};

function openMore(name: string) {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  const more = screen.getByRole("button", { name: `More actions for ${name}` });
  more.focus();
  fireEvent.keyDown(more, { key: "Enter" });
}

beforeEach(() => adminFetchMock.mockReset());

it("PF-101 replaces a provider key in a masked dialog, never window.prompt", async () => {
  const prompt = vi.spyOn(window, "prompt");
  let finish!: (value: unknown) => void;
  adminFetchMock
    .mockRejectedValueOnce(new Error("The provider rejected this key."))
    .mockReturnValueOnce(new Promise((resolve) => (finish = resolve)));
  renderProviders([savedProvider]);
  fireEvent.click(screen.getByText("Manage"));
  openMore("Saved provider");
  fireEvent.click(await screen.findByRole("menuitem", { name: /Replace key/ }));
  const dialog = within(screen.getByRole("dialog"));
  const input = dialog.getByLabelText("New provider key");
  expect(input).toHaveAttribute("type", "password");
  expect(dialog.getByRole("button", { name: "Replace key" })).toBeDisabled();
  fireEvent.change(input, { target: { value: "fake-key-1" } });
  fireEvent.click(dialog.getByRole("button", { name: "Replace key" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent(
    "The provider rejected this key.",
  );
  fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  openMore("Saved provider");
  fireEvent.click(await screen.findByRole("menuitem", { name: /Replace key/ }));
  const reopened = within(screen.getByRole("dialog"));
  expect(reopened.getByLabelText("New provider key")).toHaveValue("");
  expect(reopened.queryByRole("alert")).toBeNull();
  fireEvent.change(reopened.getByLabelText("New provider key"), {
    target: { value: "fake-key-2" },
  });
  fireEvent.click(reopened.getByRole("button", { name: "Replace key" }));
  expect(
    await reopened.findByRole("button", { name: "Replacing…" }),
  ).toBeDisabled();
  expect(adminFetchMock).toHaveBeenLastCalledWith(
    "/admin/api/providers/saved/key",
    { method: "PUT", body: { api_key: "fake-key-2" } },
  );
  finish({});
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(prompt).not.toHaveBeenCalled();
  prompt.mockRestore();
});

async function checkAddProvider(name: RegExp) {
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.click(dialog.getByRole("button", { name }));
  fireEvent.change(dialog.getByLabelText("Provider API key"), {
    target: { value: "fake-key" },
  });
  fireEvent.click(
    dialog.getByRole("button", { name: "Check provider connection" }),
  );
  await dialog.findByText("Connection worked.");
  return dialog;
}

const preset = { logoKey: "pr", protocol: "openai-chat", billingDimensions: [] };
const twoPresets = [
  { ...preset, id: "first", name: "First preset", baseUrl: "https://first.example" },
  { ...preset, id: "second", name: "Second preset", baseUrl: "https://second.example" },
];

it("PF-102 does not carry a chosen access plan across cancel or provider change", async () => {
  adminFetchMock.mockResolvedValue({
    ok: true,
    models: [{ id: "m", price_input: "1", price_output: "2" }],
  });
  renderProviders([savedProvider], {
    catalog: twoPresets,
    plans: [{ id: "family", name: "Family" }],
  });
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  let dialog = await checkAddProvider(/First preset/);
  fireEvent.click(dialog.getByRole("button", { name: "4. limits" }));
  fireEvent.click(dialog.getByRole("switch", { name: "Allow Family" }));
  expect(dialog.getByRole("switch", { name: "Allow Family" })).toBeChecked();
  // Choosing another provider in the same flow starts with no plan.
  fireEvent.click(dialog.getByRole("button", { name: "1. catalog" }));
  dialog = await checkAddProvider(/Second preset/);
  fireEvent.click(dialog.getByRole("button", { name: "4. limits" }));
  expect(dialog.getByRole("switch", { name: "Allow Family" })).not.toBeChecked();
  fireEvent.click(dialog.getByRole("switch", { name: "Allow Family" }));
  fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByLabelText("Search providers")).toBeInTheDocument();
  dialog = await checkAddProvider(/Second preset/);
  fireEvent.click(dialog.getByRole("button", { name: "4. limits" }));
  expect(dialog.getByRole("switch", { name: "Allow Family" })).not.toBeChecked();
});

it("PF-103 refreshes clean cap drafts and holds a dirty draft as a conflict", async () => {
  const client = renderProviders([savedProvider]);
  fireEvent.click(screen.getByText("Manage"));
  fireEvent.click(screen.getByRole("button", { name: "Edit caps" }));
  const daily = () => screen.getByLabelText("Saved provider daily cap (USD)");
  expect(daily()).toHaveValue("5");
  act(() =>
    client.setQueryData(["providers"], {
      providers: [{ ...savedProvider, caps: { daily_usd: 7, monthly_usd: 50 } }],
    }),
  );
  await waitFor(() => expect(daily()).toHaveValue("7"));
  fireEvent.change(daily(), { target: { value: "9" } });
  act(() =>
    client.setQueryData(["providers"], {
      providers: [{ ...savedProvider, caps: { daily_usd: 8, monthly_usd: 50 } }],
    }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "These values changed on the gateway while you were editing.",
  );
  expect(daily()).toHaveValue("9");
  expect(screen.getByRole("button", { name: "Save caps" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Use saved values" }));
  expect(daily()).toHaveValue("8");
  expect(screen.getByRole("button", { name: "Save caps" })).toBeEnabled();
});

it("PF-104 shows the existing-provider check result and treats ok:false as failure", async () => {
  let finish!: (value: unknown) => void;
  adminFetchMock.mockReturnValueOnce(
    new Promise((resolve) => (finish = resolve)),
  );
  renderProviders([savedProvider]);
  fireEvent.click(screen.getByText("Manage"));
  fireEvent.click(
    screen.getByRole("button", { name: "Check provider connection" }),
  );
  expect(await screen.findByRole("button", { name: "Checking…" })).toBeDisabled();
  finish({ ok: false });
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Provider connection could not be confirmed. Try again.",
  );
  adminFetchMock.mockResolvedValueOnce({ ok: true });
  fireEvent.click(
    screen.getByRole("button", { name: "Check provider connection" }),
  );
  expect(await screen.findByText("Connection confirmed")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("PF-105 shows a failure only on its provider and clears it after a later success", async () => {
  adminFetchMock.mockRejectedValueOnce(new Error("First save failed."));
  renderProviders([
    { ...savedProvider, id: "first", name: "First" },
    { ...savedProvider, id: "second", name: "Second" },
  ]);
  const [first, second] = screen.getAllByRole("listitem");
  fireEvent.click(within(first).getByText("Manage"));
  fireEvent.click(within(first).getByRole("button", { name: "Edit caps" }));
  fireEvent.click(within(first).getByRole("button", { name: "Save caps" }));
  expect(await within(first).findByRole("alert")).toHaveTextContent(
    "First save failed.",
  );
  expect(within(second).queryByRole("alert")).toBeNull();
  adminFetchMock.mockResolvedValueOnce({});
  fireEvent.click(within(second).getByText("Manage"));
  fireEvent.click(within(second).getByRole("button", { name: "Edit caps" }));
  fireEvent.click(within(second).getByRole("button", { name: "Save caps" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
});

it("PF-106 says when the catalog is empty, a search matches nothing, or no models exist", () => {
  renderProviders([{ ...savedProvider, models: [] }]);
  fireEvent.click(screen.getByText("Manage"));
  expect(
    screen.getByText("No models configured for this provider."),
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  expect(
    within(screen.getByRole("dialog")).getByText(
      /No provider presets available/,
    ),
  ).toBeVisible();
  cleanup();
  renderProviders([savedProvider], { catalog: twoPresets });
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.change(dialog.getByLabelText("Search providers"), {
    target: { value: "nothing like this" },
  });
  expect(dialog.getByText("No providers match this search.")).toBeVisible();
  fireEvent.click(dialog.getByRole("button", { name: "Clear search" }));
  expect(dialog.getByRole("button", { name: /First preset/ })).toBeVisible();
});

it("PF-110 shows unknown spend on a collapsed row and rejects non-numeric prices", async () => {
  adminFetchMock.mockResolvedValue({
    ok: true,
    models: [{ id: "m", price_input: "1", price_output: "2" }],
  });
  renderProviders(
    [
      { ...savedProvider, id: "first", name: "First", caps: {} },
      { ...savedProvider, id: "second", name: "Second" },
    ],
    { catalog: twoPresets },
  );
  const [first] = screen.getAllByRole("listitem");
  expect(within(first).getAllByText("Cost not recorded")[0]).toBeVisible();
  fireEvent.click(within(first).getByText("Manage"));
  fireEvent.click(within(first).getByRole("button", { name: "Edit models" }));
  fireEvent.change(within(first).getByLabelText("saved-model managed input price"), {
    target: { value: "cheap" },
  });
  fireEvent.click(within(first).getByRole("button", { name: "Save models" }));
  expect(within(first).getByRole("alert")).toHaveTextContent(
    "Enter a price of 0 or more.",
  );
  expect(adminFetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  const dialog = await checkAddProvider(/First preset/);
  fireEvent.click(dialog.getByRole("button", { name: "3. models" }));
  fireEvent.change(dialog.getByLabelText("m input price"), {
    target: { value: "free" },
  });
  expect(dialog.getByText("Needs price")).toBeVisible();
  fireEvent.click(dialog.getByRole("button", { name: "4. limits" }));
  fireEvent.click(dialog.getByRole("button", { name: "Save provider" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent(
    "Enter a price of 0 or more.",
  );
});

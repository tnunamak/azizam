import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { adminFetch } from "@/lib/api";
import { SubscriptionsSection } from "./subscriptions-section";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));
const mocked = vi.mocked(adminFetch);

// The response shapes the gateway sends today (status_one, oauth_pkce status).
const notConnected = { kind: "chatgpt", display_name: "ChatGPT", protocol: "responses", billing_mode: "plan", status: "not_connected" };
const connected = {
  ...notConnected,
  status: "connected",
  connection_id: "chatgpt-owner",
  generation: 3,
  connected_by: "owner-1",
  account_email: "owner@example.com",
  connected_at: 1_700_000_000,
  access_expires_at: 1_900_000_000,
  scopes: ["openid"],
  models: ["gpt-a", "gpt-b"],
  routes: [],
};
const needsReconnect = { ...connected, status: "needs_reconnect" };

let list: unknown[];
let calls: { path: string; method: string }[];
beforeEach(() => {
  calls = [];
  vi.stubGlobal("open", vi.fn());
  mocked.mockReset();
  mocked.mockImplementation(async (path: string, init?: { method?: string }) => {
    const method = init?.method ?? "GET";
    calls.push({ path, method });
    if (path === "/admin/api/subscriptions" && method === "GET") return { subscriptions: list };
    if (path === "/admin/api/access-plans") {
      return { access_plans: [
        { id: "p1", name: "Household", plan: { connections: ["chatgpt-owner"] } },
        { id: "p2", name: "Kids", plan: { connections: [] } },
      ] };
    }
    if (path === "/admin/api/apps") {
      return { apps: [
        { id: "a1", name: "Notes", effective_access_plan_id: "p1", disabled_at: null },
        { id: "a2", name: "Homework", effective_access_plan_id: "p2", disabled_at: null },
      ] };
    }
    if (path === "/admin/api/subscriptions/chatgpt" && method === "DELETE") {
      return { subscription: { ...notConnected, revoked_upstream: false } };
    }
    throw new Error(`unexpected ${method} ${path}`);
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><SubscriptionsSection enabled /></QueryClientProvider>);
  return client;
}

it("loads the list without any connection and shows a Connect row", async () => {
  list = [notConnected];
  renderSection();
  const region = await screen.findByRole("region", { name: "Subscriptions" });
  expect(within(region).getByText("ChatGPT subscription")).toBeInTheDocument();
  expect(within(region).getByText("Let people and apps you choose use your ChatGPT models.")).toBeInTheDocument();
  fireEvent.click(within(region).getByRole("button", { name: "Connect ChatGPT subscription" }));
  expect(await screen.findByRole("heading", { name: "Connect ChatGPT subscription" })).toBeInTheDocument();
});

it("shows a load error and fetches again when the owner retries", async () => {
  list = [notConnected];
  mocked.mockRejectedValueOnce(new Error("fixture failure"));
  renderSection();

  expect(await screen.findByText("Subscriptions couldn't load.")).toBeInTheDocument();
  // The page owns the one alert in an outage (e2e error-states): this section adds none.
  expect(screen.queryByRole("alert")).toBeNull();
  expect(mocked).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));

  expect(await screen.findByRole("button", { name: "Connect ChatGPT subscription" })).toBeInTheDocument();
  expect(mocked).toHaveBeenCalledTimes(2);
});

it("shows Down with a Reconnect button for a sign-in the provider ended", async () => {
  list = [needsReconnect];
  renderSection();
  expect((await screen.findAllByText("Down")).length).toBeGreaterThan(0);
  expect(screen.getByText(/ChatGPT ended this sign-in/)).toBeInTheDocument();
  expect(screen.queryByText("Disconnected")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reconnect ChatGPT subscription" }));
  expect(await screen.findByRole("heading", { name: "Reconnect ChatGPT subscription" })).toBeInTheDocument();
});

it("disconnects from the overflow menu with the impact counts and the upstream message", async () => {
  list = [connected];
  renderSection();
  const user = userEvent.setup();
  await screen.findByText("owner@example.com · 2 models");
  await user.click(screen.getByRole("button", { name: "Actions for ChatGPT subscription" }));
  await user.click(await screen.findByRole("menuitem", { name: "Disconnect" }));
  const dialog = await screen.findByRole("dialog");
  expect(await within(dialog).findByText("1 access plan (Household) and 1 app use it.")).toBeInTheDocument();
  expect(within(dialog).getByText(
    "Requests stop using this subscription right away. A model can still run on a paid route only when its access plan allows paid fallback.",
  )).toBeInTheDocument();
  const confirm = within(dialog).getByRole("button", { name: "Disconnect" });
  expect(confirm).toBeDisabled();
  await user.type(within(dialog).getByRole("textbox"), "ChatGPT subscription");
  list = [notConnected];
  await user.click(confirm);
  expect(await screen.findByText(/ChatGPT could not be reached, so end the sign-in in ChatGPT's settings too/)).toBeInTheDocument();
  expect(calls).toContainEqual({ path: "/admin/api/subscriptions/chatgpt", method: "DELETE" });
  await waitFor(() => expect(screen.getByRole("button", { name: "Connect ChatGPT subscription" })).toBeInTheDocument());
});

it("requires the exact typed name: no outer spaces, no different case", async () => {
  list = [connected];
  renderSection();
  const user = userEvent.setup();
  await screen.findByText("owner@example.com · 2 models");
  await user.click(screen.getByRole("button", { name: "Actions for ChatGPT subscription" }));
  await user.click(await screen.findByRole("menuitem", { name: "Disconnect" }));
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: "Disconnect" });
  const input = within(dialog).getByRole("textbox");
  await user.type(input, "chatgpt subscription");
  expect(confirm).toBeDisabled();
  await user.clear(input);
  await user.type(input, "  ChatGPT subscription  ");
  expect(confirm).toBeDisabled();
  await user.clear(input);
  await user.type(input, "ChatGPT subscription");
  expect(confirm).toBeEnabled();
});

it("says a local sign-in stays on the computer running Azizam instead of promising an upstream sign-out", async () => {
  list = [{ ...connected, kind: "claude", display_name: "Claude", connection_id: "claude-owner", sign_in: "browser", paste_kind: "code", revokes_sign_in: false }];
  renderSection();
  const user = userEvent.setup();
  await screen.findByText(/2 models/);
  await user.click(screen.getByRole("button", { name: "Actions for Claude subscription" }));
  await user.click(await screen.findByRole("menuitem", { name: "Disconnect" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/The sign-in stays on the computer running Azizam/)).toBeInTheDocument();
  expect(within(dialog).queryByText(/We also tell Claude to end this sign-in/)).not.toBeInTheDocument();
});

it("lists the models sorted, with their efforts and pinned names, when the row is opened", async () => {
  list = [{
    ...connected,
    models: ["gpt-z", "gpt-a"],
    model_details: [{ slug: "gpt-z", efforts: [] }, { slug: "gpt-a", efforts: ["low", "high"] }],
    routes: [{ id: "sub/gpt-a", logical_model: "gpt-a", service_id: "sub", model_id: "gpt-a" }],
  }];
  renderSection();
  await screen.findByText("owner@example.com · 2 models");
  const open = screen.getByRole("button", { name: "Open ChatGPT subscription" });
  expect(open).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(open);
  expect(open).toHaveAttribute("aria-expanded", "true");
  const models = await screen.findByRole("list", { name: "Models of ChatGPT subscription" });
  const items = within(models).getAllByRole("listitem");
  expect(items).toHaveLength(2);
  expect(items[0].textContent).toContain("gpt-a · low · high");
  expect(items[1].textContent).toContain("gpt-z");
  expect(within(items[0]).getByDisplayValue("sub/gpt-a")).toBeInTheDocument();
  expect(within(items[1]).queryByRole("button", { name: /pinned name/ })).not.toBeInTheDocument();
  expect(screen.getByText("Use the pinned name to send requests only through this subscription, never through another connection or paid key.")).toBeInTheDocument();
});

async function openModels(sub: Record<string, unknown>) {
  list = [sub];
  renderSection();
  fireEvent.click(await screen.findByRole("button", { name: "Open ChatGPT subscription" }));
}

const planStep = (slug: string) => ({
  route_id: `chatgpt-plan/${slug}`, kind: "plan", service_id: "chatgpt-plan", name: "ChatGPT plan", paid_fallback: false,
  connection_id: "chatgpt-owner",
});
const paidStep = { route_id: "paid/x", kind: "paid", service_id: "openai", name: "OpenAI", paid_fallback: true, provider_id: "openai" };

it.each([
  { name: "an omitted model list", models: undefined, notice: "No model list yet." },
  { name: "a null model list", models: null, notice: "No model list yet." },
  { name: "an empty model list", models: [], notice: "No models listed." },
])("gives accurate opened-row text for $name without an access plans link", async ({ models, notice }) => {
  await openModels({ ...connected, models, routes: [] });
  expect(await screen.findByText(notice)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "access plans" })).not.toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "Models of ChatGPT subscription" })).not.toBeInTheDocument();
});

it.each([
  {
    name: "a pin-only model before the model list is available",
    models: undefined,
    routes: [{ id: "sub/group-b", logical_model: "group-b", service_id: "sub", model_id: "group-b" }],
    expected: [{ text: "group-b", pin: "sub/group-b" }],
  },
  {
    name: "one model",
    models: ["gpt-a"],
    routes: [{ id: "sub/gpt-a", logical_model: "gpt-a", service_id: "sub", model_id: "gpt-a" }],
    expected: [{ text: "gpt-a · low", pin: "sub/gpt-a" }],
  },
  {
    name: "many models, unsorted, with a pin-only model and a repeated name",
    models: ["gpt-z", "gpt-a", "gpt-m", "gpt-a"],
    routes: [
      { id: "sub/gpt-a", logical_model: "gpt-a", service_id: "sub", model_id: "gpt-a" },
      { id: "sub/group-b", logical_model: "group-b", service_id: "sub", model_id: "group-b" },
    ],
    expected: [
      { text: "gpt-a · low", pin: "sub/gpt-a" },
      { text: "gpt-m", pin: null },
      { text: "gpt-z · high · xhigh", pin: null },
      { text: "group-b", pin: "sub/group-b" },
    ],
  },
])("lists $name in the opened row: unique sorted names, each model's efforts, the copyable pinned name", async ({ models, routes, expected }) => {
  await openModels({
    ...connected,
    models,
    routes,
    model_details: [{ slug: "gpt-a", efforts: ["low"] }, { slug: "gpt-z", efforts: ["high", "xhigh"] }],
  });
  const count = expected.length;
  expect(await screen.findByText(`${count} ${count === 1 ? "model" : "models"}. Choose who can use them in`, { exact: false })).toBeInTheDocument();
  // Next's Link drops the trailing slash under jsdom; the built export keeps it.
  expect(screen.getByRole("link", { name: "access plans" }).getAttribute("href")).toMatch(/^\/people\/?$/);
  const items = within(screen.getByRole("list", { name: "Models of ChatGPT subscription" })).getAllByRole("listitem");
  expect(items.map((item) => item.querySelector("p")?.textContent)).toEqual(expected.map((model) => model.text));
  expected.forEach((model, index) => {
    const slug = model.text.split(" · ")[0];
    const copy = within(items[index]).queryByRole("button", { name: `Copy the pinned name for ${slug}` });
    if (model.pin) {
      expect(copy).toBeInTheDocument();
      expect(within(items[index]).getByDisplayValue(model.pin)).toBeInTheDocument();
    } else {
      expect(copy).not.toBeInTheDocument();
    }
  });
});

it("shows the steps once when every model walks the same chain", async () => {
  await openModels({
    ...connected,
    models: ["gpt-a", "gpt-b"],
    model_chains: [
      { slug: "gpt-a", direct: false, steps: [planStep("gpt-a"), paidStep] },
      { slug: "gpt-b", direct: false, steps: [planStep("gpt-b"), paidStep] },
    ],
  });
  const steps = await screen.findByRole("list", { name: "Steps for every model" });
  expect(within(steps).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "1. Your ChatGPT plan.",
    "2. Paid OpenAI key, only when the person's and the app's access plans both allow paid fallback.",
    "3. Otherwise the request fails with a message.",
  ]);
  expect(screen.queryByRole("list", { name: "Steps for gpt-a" })).not.toBeInTheDocument();
  expect(screen.getByText(/A request skips the steps that the person's or the app's access plan leaves out\. If a paid key ends up first for someone, it is used directly, not as a fallback\./)).toBeInTheDocument();
});

it("shows each model's own steps when the chains differ, and says when a plain name does not use the plan", async () => {
  await openModels({
    ...connected,
    models: ["gpt-a", "gpt-b", "gpt-c"],
    model_chains: [
      { slug: "gpt-a", direct: false, steps: [planStep("gpt-a")] },
      { slug: "gpt-b", direct: false, steps: [planStep("gpt-b"), paidStep] },
      { slug: "gpt-c", direct: true, steps: [{ ...paidStep, paid_fallback: false }] },
    ],
  });
  const words = (slug: string) =>
    within(screen.getByRole("list", { name: `Steps for ${slug}` })).getAllByRole("listitem").map((item) => item.textContent);
  expect(await screen.findByRole("list", { name: "Steps for gpt-a" })).toBeInTheDocument();
  expect(words("gpt-a")).toEqual(["1. Your ChatGPT plan.", "2. Otherwise the request fails with a message."]);
  expect(words("gpt-b")).toEqual([
    "1. Your ChatGPT plan.",
    "2. Paid OpenAI key, only when the person's and the app's access plans both allow paid fallback.",
    "3. Otherwise the request fails with a message.",
  ]);
  expect(words("gpt-c")).toEqual(["A plain gpt-c request does not use this plan. It goes to: Paid OpenAI key."]);
  expect(screen.queryByRole("list", { name: "Steps for every model" })).not.toBeInTheDocument();
});

it("offers Check now in the overflow menu of a healthy connected row, and calls the refresh route", async () => {
  list = [connected];
  mocked.mockImplementation(async (path: string, init?: { method?: string }) => {
    const method = init?.method ?? "GET";
    calls.push({ path, method });
    if (path === "/admin/api/subscriptions" && method === "GET") return { subscriptions: list };
    if (path === "/admin/api/subscriptions/chatgpt/refresh-models" && method === "POST") return { models: ["gpt-a"] };
    throw new Error(`unexpected ${method} ${path}`);
  });
  renderSection();
  const user = userEvent.setup();
  await screen.findByText("owner@example.com · 2 models");
  await user.click(screen.getByRole("button", { name: "Actions for ChatGPT subscription" }));
  await user.click(await screen.findByRole("menuitem", { name: "Check now" }));
  await waitFor(() => expect(calls).toContainEqual({ path: "/admin/api/subscriptions/chatgpt/refresh-models", method: "POST" }));
});

it("offers Check now inline without disabling a different subscription", async () => {
  list = [
    { ...connected, models: undefined },
    { ...connected, kind: "claude", display_name: "Claude", connection_id: "claude-owner" },
  ];
  let finishCheck!: (value: { models: string[] }) => void;
  const refresh = new Promise<{ models: string[] }>((resolve) => { finishCheck = resolve; });
  mocked.mockImplementation(async (path: string, init?: { method?: string }) => {
    const method = init?.method ?? "GET";
    calls.push({ path, method });
    if (path === "/admin/api/subscriptions" && method === "GET") return { subscriptions: list };
    if (path === "/admin/api/subscriptions/chatgpt/refresh-models" && method === "POST") return refresh;
    throw new Error(`unexpected ${method} ${path}`);
  });
  renderSection();
  const chatgpt = await screen.findByRole("button", { name: "Open ChatGPT subscription" });
  fireEvent.click(chatgpt);
  expect(await screen.findByText("No model list yet.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Check now" }));

  try {
    await waitFor(() => expect(chatgpt).toBeDisabled());
    expect(screen.getByRole("button", { name: "Open Claude subscription" })).toBeEnabled();
    expect(calls).toContainEqual({ path: "/admin/api/subscriptions/chatgpt/refresh-models", method: "POST" });
  } finally {
    finishCheck({ models: ["gpt-a"] });
  }
  await waitFor(() => expect(chatgpt).toBeEnabled());
});

it("keeps each row busy until its own overlapping check settles", async () => {
  list = [connected, { ...connected, kind: "claude", display_name: "Claude", connection_id: "claude-owner" }];
  let settleChatgpt!: (value: { models: string[] }) => void;
  let settleClaude!: (value: { models: string[] }) => void;
  const chatgptAnswer = new Promise<{ models: string[] }>((resolve) => { settleChatgpt = resolve; });
  const claudeAnswer = new Promise<{ models: string[] }>((resolve) => { settleClaude = resolve; });
  mocked.mockImplementation(async (path: string, init?: { method?: string }) => {
    calls.push({ path, method: init?.method ?? "GET" });
    if (path === "/admin/api/subscriptions") return { subscriptions: list };
    if (path === "/admin/api/subscriptions/chatgpt/refresh-models") return chatgptAnswer;
    if (path === "/admin/api/subscriptions/claude/refresh-models") return claudeAnswer;
    throw new Error(`unexpected ${path}`);
  });
  renderSection();
  const user = userEvent.setup();
  const chatgpt = await screen.findByRole("button", { name: "Open ChatGPT subscription" });
  const claude = screen.getByRole("button", { name: "Open Claude subscription" });
  const check = async (name: string) => {
    await user.click(screen.getByRole("button", { name: `Actions for ${name} subscription` }));
    await user.click(await screen.findByRole("menuitem", { name: "Check now" }));
  };
  try {
    await check("ChatGPT");
    await waitFor(() => expect(chatgpt).toBeDisabled());
    await check("Claude");
    await waitFor(() => expect(claude).toBeDisabled());
    expect(chatgpt).toBeDisabled();
    settleClaude({ models: ["fixture-claude"] });
    await waitFor(() => expect(claude).toBeEnabled());
    expect(chatgpt).toBeDisabled();
  } finally {
    settleChatgpt({ models: ["fixture-chatgpt"] });
    settleClaude({ models: ["fixture-claude"] });
  }
  await waitFor(() => expect(chatgpt).toBeEnabled());
});

it("hides opened model details and Check now when a refetch requires reconnect", async () => {
  list = [{ ...connected, models: undefined }];
  const client = renderSection();
  const row = await screen.findByRole("button", { name: "Open ChatGPT subscription" });
  fireEvent.click(row);
  expect(await screen.findByText("No model list yet.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Check now" })).toBeInTheDocument();
  list = [{ ...needsReconnect, models: undefined }];
  await client.invalidateQueries({ queryKey: ["subscriptions"] });
  expect(await screen.findByRole("button", { name: "Reconnect ChatGPT subscription" })).not.toHaveAttribute("aria-expanded", "true");
  expect(screen.queryByText("No model list yet.")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Check now" })).not.toBeInTheDocument();

  list = [{ ...connected, models: undefined }];
  await client.invalidateQueries({ queryKey: ["subscriptions"] });
  const recovered = await screen.findByRole("button", { name: "Open ChatGPT subscription" });
  expect(recovered).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText("No model list yet.")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Check now" })).not.toBeInTheDocument();
  fireEvent.click(recovered);
  expect(await screen.findByText("No model list yet.")).toBeInTheDocument();
});

it("offers no Check now in the overflow menu of a row that is not connected", async () => {
  list = [needsReconnect];
  renderSection();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Actions for ChatGPT subscription" }));
  expect(await screen.findByRole("menuitem", { name: "Disconnect" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Check now" })).not.toBeInTheDocument();
});

it("never prints the connection id as primary text", async () => {
  list = [connected];
  renderSection();
  await screen.findByText("ChatGPT subscription");
  expect(document.body.textContent).not.toContain("chatgpt-owner");
});

it("renders nothing for a signed-in person who is not the owner", () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><SubscriptionsSection enabled={false} /></QueryClientProvider>);
  expect(screen.queryByRole("region", { name: "Subscriptions" })).not.toBeInTheDocument();
  expect(mocked).not.toHaveBeenCalled();
});

it("links the opened row to the provider's usage page, in a new tab", async () => {
  list = [{ ...connected, usage_page_url: "https://chatgpt.com/settings/usage" }];
  renderSection();
  const region = await screen.findByRole("region", { name: "Subscriptions" });
  fireEvent.click(within(region).getByRole("button", { name: "Open ChatGPT subscription" }));
  const usage = within(region).getByRole("group", { name: "Provider allowance" });
  const link = within(usage).getByRole("link", { name: "Open ChatGPT usage (opens in a new tab)" });
  expect(link.getAttribute("href")).toBe("https://chatgpt.com/settings/usage");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toContain("noreferrer");
});

it("shows the plan's usage in the opened row", async () => {
  const now = Math.floor(Date.now() / 1000);
  list = [{ ...connected, usage_limits: { captured_at: now - 30, windows: [{ used_percent: 42, window_minutes: 300, resets_at: now + 3600 }] } }];
  renderSection();
  const region = await screen.findByRole("region", { name: "Subscriptions" });
  expect(within(region).getByText(/5-hour limit 42% used/)).toBeInTheDocument();
  fireEvent.click(within(region).getByRole("button", { name: "Open ChatGPT subscription" }));
  const usage = within(region).getByRole("group", { name: "Provider allowance" });
  expect(within(usage).getByText(/^5-hour limit: 42% used, resets /)).toBeInTheDocument();
  expect(within(usage).getByText("Read from ChatGPT just now.")).toBeInTheDocument();
});

it("explains the level used when a model also lists an unknown level, and says nothing odd for no levels", async () => {
  list = [{ ...connected, top_effort: "low", model_details: [
    { slug: "gpt-a", efforts: ["future", "high"] }, { slug: "gpt-b", efforts: [] }, { slug: "gpt-c", efforts: ["future"] },
  ] }];
  renderSection();
  fireEvent.click(await screen.findByRole("button", { name: "Open ChatGPT subscription" }));
  expect(screen.getByText("Uses high: this model has no level at or below low.")).toBeInTheDocument();
  expect(screen.queryByText(/Uses undefined/)).toBeNull();
  expect(screen.getAllByText(/: this model has no level at or below low\./)).toHaveLength(1);
});

it("displays an unrecognized saved top reasoning level without asserting a fallback", async () => {
  list = [{ ...connected, top_effort: "future", model_details: [{ slug: "gpt-a", efforts: ["high"] }] }];
  renderSection();
  fireEvent.click(await screen.findByRole("button", { name: "Open ChatGPT subscription" }));
  const picker = screen.getByRole("combobox", { name: "Top reasoning level" });
  expect(picker).toHaveValue("future");
  expect(within(picker).getByRole("option", { name: "Unrecognized level: future" })).toBeDisabled();
  expect(screen.queryByText(/this model has no level at or below/)).toBeNull();
});

it("keeps the level pick-list focusable while it saves, and announces the save in a region that is always there", async () => {
  list = [{ ...connected, top_effort: null, model_details: [{ slug: "gpt-a", efforts: ["low", "high"] }] }];
  let release: (value: unknown) => void = () => undefined;
  mocked.mockImplementation(async (path) => {
    if (path === "/admin/api/subscriptions") return { subscriptions: list };
    calls.push({ path, method: "PATCH" });
    return new Promise((resolve) => { release = resolve; });
  });
  renderSection();
  fireEvent.click(await screen.findByRole("button", { name: "Open ChatGPT subscription" }));
  const select = screen.getByRole("combobox", { name: "Top reasoning level" });
  const status = screen.getByTestId("top-effort-status");
  expect(status).toHaveAttribute("role", "status");
  select.focus();
  fireEvent.change(select, { target: { value: "low" } });
  await waitFor(() => expect(status).toHaveTextContent("Saving…"));
  expect(select).not.toBeDisabled();
  expect(select).toHaveAttribute("aria-disabled", "true");
  expect(document.activeElement).toBe(select);
  fireEvent.change(select, { target: { value: "high" } });
  expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(1);
  release({ subscription: list[0] });
});

it("saves the top reasoning level, refetches, and explains a model above the ceiling", async () => {
  list = [{ ...connected, top_effort: null, model_details: [
    { slug: "gpt-a", efforts: ["high", "low"] }, { slug: "gpt-b", efforts: ["xhigh", "high"] },
  ] }];
  mocked.mockImplementation(async (path, init) => {
    calls.push({ path, method: init?.method ?? "GET" });
    if (path === "/admin/api/subscriptions") return { subscriptions: list };
    if (path === "/admin/api/subscriptions/chatgpt/reasoning") {
      expect(init?.method).toBe("PATCH");
      expect(init?.body).toEqual({ top_effort: "low" });
      list = [{ ...(list[0] as object), top_effort: "low" }];
      return { subscription: list[0] };
    }
    throw new Error(`unexpected ${path}`);
  });
  renderSection();
  fireEvent.click(await screen.findByRole("button", { name: "Open ChatGPT subscription" }));
  const select = screen.getByRole("combobox", { name: "Top reasoning level" });
  expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual(["No limit", "low", "high", "xhigh"]);
  fireEvent.change(select, { target: { value: "low" } });
  expect(await screen.findByText("Uses high: this model has no level at or below low.")).toBeInTheDocument();
  await waitFor(() => expect(calls.filter((call) => call.path === "/admin/api/subscriptions")).toHaveLength(2));
  expect(select).toHaveValue("low");
});

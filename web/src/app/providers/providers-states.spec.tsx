// Display states and request bodies of the Providers page that used to run in
// the browser (e2e/providers.spec.ts). Payloads are copied from that spec; the
// network is a fetch stub that answers the same admin routes the browser spec
// intercepted. Real phone geometry, axe and the real-gateway journey stay in
// the browser spec.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProvidersPage from "./page";

const catalog = {
  providers: [
    {
      id: "openrouter",
      display_name: "OpenRouter",
      logo_key: "or",
      protocol: "openai-chat",
      base_url: "https://openrouter.ai/api/v1",
      key_url: "https://openrouter.ai/keys",
      recommendations: [
        {
          message:
            "Set an OpenRouter credit limit as a second backstop before enabling paid plans.",
          url: "https://openrouter.ai/settings/credits",
        },
      ],
      billing_dimensions: ["input tokens", "output tokens"],
    },
    {
      id: "generic-openai",
      display_name: "Generic OpenAI-compatible",
      logo_key: "go",
      protocol: "openai-chat",
      base_url: "https://provider.example/v1",
      billing_dimensions: ["input tokens", "output tokens"],
    },
  ],
};

// GET /admin/api/access-plans: Paid providers live in each plan's document.
const plans = {
  access_plans: [
    {
      id: "local",
      name: "Local only",
      summary: "No paid providers",
      plan: { paid_providers: [] },
    },
    {
      id: "paid",
      name: "Paid cloud",
      summary: "Can use selected paid providers",
      plan: { paid_providers: ["openrouter"] },
    },
  ],
};

const seededProviders = {
  providers: [
    {
      id: "openrouter",
      name: "OpenRouter",
      enabled: true,
      reachable: true,
      checked_at: Math.floor(Date.now() / 1000) - 90,
      key_status: {
        state: "set",
        source: "secret",
        suffix: "4f2a",
        updated_at: Math.floor(Date.now() / 1000) - 172800,
      },
      spend: {
        daily_usd: 1.2,
        monthly_usd: 14.5,
        status: "needs_reconciliation",
        pending_reconciliation_count: 1,
      },
      caps: { daily_usd: 5, monthly_usd: 50 },
      access_plans: [],
      recommendations: [
        {
          message:
            "Set an OpenRouter credit limit as a second backstop before enabling paid plans.",
          url: "https://openrouter.ai/settings/credits",
        },
      ],
      catalog: {
        preset_id: "openrouter",
        protocol: "openai-chat",
        base_url: "https://openrouter.ai/api/v1",
      },
      models: [
        {
          model: "openrouter/anthropic/claude-sonnet-4.5",
          upstream_model: "anthropic/claude-sonnet-4.5",
          name: "Claude Sonnet 4.5",
          enabled: true,
          price: {
            input_usd_per_million_tokens: 3,
            output_usd_per_million_tokens: 15,
          },
        },
      ],
    },
  ],
};


type Call = { path: string; method: string; body: unknown };

type Hooks = {
  /** Resolve this to hold the OpenRouter connection check. */
  delayOpenRouterCheck?: Promise<void>;
};

function providerTestResponse(providerId: string) {
  const label = providerId === "openrouter" ? "OpenRouter" : "Generic OpenAI";
  return {
    ok: true,
    message: `Listed 1 model from ${label}.`,
    models: [
      {
        id: `${providerId}/anthropic/claude-sonnet-4.5`,
        name: "Claude Sonnet 4.5",
        upstream_model: "anthropic/claude-sonnet-4.5",
        price: {
          input_usd_per_million_tokens: 3,
          output_usd_per_million_tokens: 15,
        },
      },
    ],
  };
}

const openRouterTest = {
  ok: true,
  message: "Listed 2 models from OpenRouter.",
  models: [
    {
      id: "openrouter/anthropic/claude-sonnet-4.5",
      name: "Claude Sonnet 4.5",
      upstream_model: "anthropic/claude-sonnet-4.5",
      context: "200k context",
      price: {
        input_usd_per_million_tokens: 3,
        output_usd_per_million_tokens: 15,
      },
    },
    {
      id: "openrouter/unknown-new-model",
      name: "Unknown new model",
      context: "Context not reported",
      price_input: "",
      price_output: "",
    },
  ],
};

const pending42 = {
  id: 42,
  provider: "openrouter",
  profile_id: "paid",
  status: "needs_reconciliation",
  estimated_usd: "0.015",
  actual_usd: null,
  cost_source: "estimated",
  created_at: 1710000000,
  settled_at: null,
  request_id: "req_pending_42_abcdefghijklmnopqrstuvwxyz",
  generation_id: "gen_pending_42_abcdefghijklmnopqrstuvwxyz",
};
const pending43 = {
  ...pending42,
  id: 43,
  estimated_usd: "0.000001234",
  created_at: 1710000001,
  request_id: "req_pending_43",
  generation_id: "gen_pending_43",
};

function installFetch(
  state: "seeded" | "empty" | "error",
  { catalogOverride, hooks = {} }: { catalogOverride?: unknown; hooks?: Hooks } = {},
) {
  const calls: Call[] = [];
  const settled = new Set<number>();
  let saved = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://gateway.test").pathname;
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      if (path.endsWith("/session")) {
        return Response.json({ actor: { csrf_token: "test-csrf" } });
      }
      if (path === "/admin/api/access-plans") return Response.json(plans);
      if (path === "/admin/api/providers/-/catalog") {
        return Response.json(catalogOverride ?? catalog);
      }
      calls.push({ path, method, body });
      if (path === "/admin/api/providers/-/test") {
        if (body.provider_id === "openrouter" && hooks.delayOpenRouterCheck) {
          await hooks.delayOpenRouterCheck;
        }
        return Response.json(
          body.api_key === "sk-or-browser-test"
            ? openRouterTest
            : providerTestResponse(String(body.provider_id)),
        );
      }
      if (path === "/admin/api/providers/openrouter/reconciliation") {
        return Response.json({
          reconciliations: [pending42, pending43].filter(
            (row) => !settled.has(row.id),
          ),
        });
      }
      const settle = path.match(/reconciliation\/(\d+)$/);
      if (settle) {
        settled.add(Number(settle[1]));
        return Response.json({
          reconciliation: { id: Number(settle[1]), status: "settled" },
        });
      }
      if (
        /\/admin\/api\/providers\/openrouter\/(models|caps|key)$/.test(path)
      ) {
        return Response.json({
          provider: seededProviders.providers[0],
          key_status: { state: "set" },
        });
      }
      if (path === "/admin/api/providers" && method === "POST") {
        saved = true;
        return Response.json({ provider: seededProviders.providers[0] });
      }
      if (path === "/admin/api/providers") {
        if (state === "error") {
          return Response.json(
            { error: { message: "Provider API unavailable" } },
            { status: 503 },
          );
        }
        return Response.json(
          saved || state === "seeded" ? seededProviders : { providers: [] },
        );
      }
      return Response.json({}, { status: 404 });
    }),
  );
  return calls;
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
  return client;
}

const callsTo = (calls: Call[], path: string, method: string) =>
  calls.filter((call) => call.path === path && call.method === method);

beforeEach(() => {
  // Radix menus and sheets scroll their active item into view.
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("providers page states", () => {
  it("seeded: shows the provider and its model ids after Manage", async () => {
    installFetch("seeded");
    renderPage();
    expect(await screen.findByRole("heading", { name: "OpenRouter" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Providers" })).toBeVisible();
    fireEvent.click(screen.getByText("Manage", { exact: true }));
    expect(
      screen.getByRole("cell", { name: "openrouter/anthropic/claude-sonnet-4.5" }),
    ).toBeVisible();
  });

  it("empty: invites adding one provider", async () => {
    installFetch("empty");
    renderPage();
    expect(
      await screen.findByText(/Add one provider, then choose its models/),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Add provider" })[0]).toBeVisible();
  });

  it("error: offers Retry providers", async () => {
    installFetch("error");
    renderPage();
    expect(
      await screen.findByRole("button", { name: "Retry providers" }),
    ).toBeVisible();
  });
});

describe("provider detail edits", () => {
  it("sends models and caps to their own admin routes and names the plans that include the provider", async () => {
    const calls = installFetch("seeded");
    renderPage();
    fireEvent.click(await screen.findByText("Manage", { exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "Edit models" }));
    fireEvent.click(
      screen.getByLabelText("Enable openrouter/anthropic/claude-sonnet-4.5"),
    );
    fireEvent.change(
      screen.getByLabelText(
        "openrouter/anthropic/claude-sonnet-4.5 managed output price",
      ),
      { target: { value: "12" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save models" }));
    await waitFor(() =>
      expect(callsTo(calls, "/admin/api/providers/openrouter/models", "PUT")).toHaveLength(1),
    );
    const [models] = callsTo(calls, "/admin/api/providers/openrouter/models", "PUT");
    expect((models.body as { models: unknown[] }).models[0]).toMatchObject({
      id: "openrouter/anthropic/claude-sonnet-4.5",
      name: "Claude Sonnet 4.5",
      enabled: false,
      price: { output_usd_per_million_tokens: 12 },
    });

    fireEvent.click(await screen.findByRole("button", { name: "Edit caps" }));
    fireEvent.change(screen.getByLabelText("OpenRouter daily cap (USD)"), {
      target: { value: "7" },
    });
    fireEvent.change(screen.getByLabelText("OpenRouter monthly cap (USD)"), {
      target: { value: "70" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save caps" }));
    await waitFor(() =>
      expect(callsTo(calls, "/admin/api/providers/openrouter/caps", "PUT")).toHaveLength(1),
    );
    expect(callsTo(calls, "/admin/api/providers/openrouter/caps", "PUT")[0].body)
      .toEqual({ daily_usd: 7, monthly_usd: 70 });

    // Plans include a paid provider in People → Access plans, not here.
    expect(screen.getByText("No access plan includes OpenRouter.")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Edit access plans" }),
    ).not.toBeInTheDocument();
  });
});

describe("reconciliation before removal", () => {
  it("lists pending charges with full ids and records actuals, including zero", async () => {
    const calls = installFetch("seeded");
    renderPage();
    fireEvent.click(await screen.findByText("Manage", { exact: true }));
    expect(await screen.findByText("1 charge needs checking")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Check charges" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByLabelText("Verified actual charge for charge 42");
    expect(dialog).toHaveTextContent("req_pending_42_abcdefghijklmnopqrstuvwxyz");
    expect(dialog).toHaveTextContent("gen_pending_42_abcdefghijklmnopqrstuvwxyz");
    expect(within(dialog).getByText("USD 0.015")).toBeVisible();
    expect(within(dialog).getByText("USD 0.000001234")).toBeVisible();
    const first = within(dialog).getByLabelText("Verified actual charge for charge 42");
    expect(first).toHaveValue("");
    expect(within(dialog).getAllByRole("button", { name: "Record actual" })[0]).toBeDisabled();

    fireEvent.change(first, { target: { value: "0.000001234" } });
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Record actual" })[0]);
    await waitFor(() => expect(screen.queryByText("Charge #42")).toBeNull());
    expect(
      callsTo(calls, "/admin/api/providers/openrouter/reconciliation/42", "POST")[0].body,
    ).toEqual({ actual_usd: "0.000001234" });

    const zero = within(dialog).getByLabelText("Verified actual charge for charge 43");
    expect(zero).toHaveValue("");
    fireEvent.change(zero, { target: { value: "0" } });
    const zeroButton = within(dialog).getAllByRole("button", { name: "Record actual" })[0];
    expect(zeroButton).toBeEnabled();
    fireEvent.click(zeroButton);
    expect(await within(dialog).findByText("No charges need checking.")).toBeVisible();
    expect(
      callsTo(calls, "/admin/api/providers/openrouter/reconciliation/43", "POST")[0].body,
    ).toEqual({ actual_usd: "0" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

async function openAddProvider() {
  renderPage();
  await screen.findByText(/Add one provider/);
  fireEvent.click(screen.getAllByRole("button", { name: "Add provider" })[0]);
  return within(screen.getByRole("dialog"));
}

function choose(dialog: ReturnType<typeof within>, search: string, name: RegExp) {
  fireEvent.change(dialog.getByLabelText("Search providers"), {
    target: { value: search },
  });
  fireEvent.click(dialog.getByRole("button", { name }));
}

describe("add provider wizard", () => {
  it("adds OpenRouter, lists discovered models and keeps paid access gated", async () => {
    const calls = installFetch("empty");
    const dialog = await openAddProvider();
    choose(dialog, "openrouter", /OpenRouter/);
    expect(dialog.getByLabelText("Provider base URL")).toHaveValue(
      "https://openrouter.ai/api/v1",
    );
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "sk-or-browser-test" },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Check provider connection" }));
    expect(await dialog.findByText("Listed 2 models from OpenRouter.")).toBeVisible();
    expect(callsTo(calls, "/admin/api/providers/-/test", "POST")[0].body).toMatchObject({
      preset_id: "openrouter",
      provider_id: "openrouter",
      key_mode: "secret",
      api_key: "sk-or-browser-test",
      base_url: "https://openrouter.ai/api/v1",
      models: [],
    });
    fireEvent.click(dialog.getByRole("button", { name: "Next" }));
    expect(dialog.getByText("openrouter/anthropic/claude-sonnet-4.5")).toBeVisible();
    expect(dialog.getByText("openrouter/unknown-new-model")).toBeVisible();
    fireEvent.click(dialog.getByRole("button", { name: "Next" }));
    expect(
      dialog.getByText(/No access plan includes a new provider/),
    ).toBeVisible();
    fireEvent.click(dialog.getByRole("button", { name: "Save provider" }));
    await waitFor(() =>
      expect(callsTo(calls, "/admin/api/providers", "POST")).toHaveLength(1),
    );
    const body = callsTo(calls, "/admin/api/providers", "POST")[0].body as {
      models: unknown[];
    };
    expect(body).toMatchObject({
      preset_id: "openrouter",
      provider_id: "openrouter",
      key_mode: "secret",
      api_key: "sk-or-browser-test",
    });
    expect(body).not.toHaveProperty("allowed_plans");
    expect(body.models[0]).toEqual({
      id: "openrouter/anthropic/claude-sonnet-4.5",
      name: "Claude Sonnet 4.5",
      upstream_model: "anthropic/claude-sonnet-4.5",
      enabled: true,
      price: {
        input_usd_per_million_tokens: 3,
        output_usd_per_million_tokens: 15,
      },
    });
    expect(await screen.findByRole("heading", { name: "OpenRouter" })).toBeVisible();
    fireEvent.click(screen.getByText("Manage", { exact: true }));
    expect(
      screen.getByRole("cell", { name: "openrouter/anthropic/claude-sonnet-4.5" }),
    ).toBeVisible();
  });
});

describe("credentials never follow a changed target", () => {
  const checkButton = (dialog: ReturnType<typeof within>) =>
    dialog.getByRole("button", { name: "Check provider connection" });

  it("clears a pasted key when the provider is switched", async () => {
    const calls = installFetch("empty");
    const dialog = await openAddProvider();
    choose(dialog, "openrouter", /OpenRouter/);
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "sk-provider-a-secret" },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Back" }));
    choose(dialog, "generic", /Generic OpenAI-compatible/);
    expect(dialog.getByLabelText("Provider API key")).toHaveValue("");
    expect(checkButton(dialog)).toBeDisabled();
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "sk-provider-b-secret" },
    });
    fireEvent.click(checkButton(dialog));
    expect(await dialog.findByText("Listed 1 model from Generic OpenAI.")).toBeVisible();
    const checks = callsTo(calls, "/admin/api/providers/-/test", "POST");
    expect(checks).toHaveLength(1);
    expect(checks[0].body).toMatchObject({
      provider_id: "generic-openai",
      key_mode: "secret",
      api_key: "sk-provider-b-secret",
    });
    expect(JSON.stringify(checks[0].body)).not.toContain("sk-provider-a-secret");
  });

  it("clears an environment reference when the provider is switched", async () => {
    const calls = installFetch("empty");
    const dialog = await openAddProvider();
    choose(dialog, "openrouter", /OpenRouter/);
    fireEvent.click(dialog.getByRole("button", { name: "Use environment variable" }));
    fireEvent.change(dialog.getByLabelText("Environment variable"), {
      target: { value: "OPENROUTER_A_KEY" },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Back" }));
    choose(dialog, "generic", /Generic OpenAI-compatible/);
    expect(dialog.getByLabelText("Environment variable")).toHaveValue("");
    expect(checkButton(dialog)).toBeDisabled();
    fireEvent.change(dialog.getByLabelText("Environment variable"), {
      target: { value: "GENERIC_B_KEY" },
    });
    fireEvent.click(checkButton(dialog));
    expect(await dialog.findByText("Listed 1 model from Generic OpenAI.")).toBeVisible();
    const checks = callsTo(calls, "/admin/api/providers/-/test", "POST");
    expect(checks).toHaveLength(1);
    expect(checks[0].body).toMatchObject({
      provider_id: "generic-openai",
      key_mode: "env",
      api_key_env: "GENERIC_B_KEY",
    });
    expect(JSON.stringify(checks[0].body)).not.toContain("OPENROUTER_A_KEY");
  });

  it("clears credentials when the base URL changes", async () => {
    const calls = installFetch("empty");
    const dialog = await openAddProvider();
    choose(dialog, "openrouter", /OpenRouter/);
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "sk-before-base-change" },
    });
    fireEvent.change(dialog.getByLabelText("Provider base URL"), {
      target: { value: "https://alt-openrouter.example/v1" },
    });
    expect(dialog.getByLabelText("Provider API key")).toHaveValue("");
    expect(checkButton(dialog)).toBeDisabled();
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "sk-after-base-change" },
    });
    fireEvent.click(checkButton(dialog));
    expect(await dialog.findByText("Listed 1 model from OpenRouter.")).toBeVisible();
    const checks = callsTo(calls, "/admin/api/providers/-/test", "POST");
    expect(checks).toHaveLength(1);
    expect(checks[0].body).toMatchObject({
      provider_id: "openrouter",
      api_key: "sk-after-base-change",
      base_url: "https://alt-openrouter.example/v1",
    });
    expect(JSON.stringify(checks[0].body)).not.toContain("sk-before-base-change");
  });

  it("ignores a late connection check for a provider that is no longer chosen", async () => {
    let release!: () => void;
    const calls = installFetch("empty", {
      hooks: { delayOpenRouterCheck: new Promise<void>((resolve) => (release = resolve)) },
    });
    const dialog = await openAddProvider();
    choose(dialog, "openrouter", /OpenRouter/);
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "sk-provider-a-secret" },
    });
    fireEvent.click(checkButton(dialog));
    await waitFor(() =>
      expect(callsTo(calls, "/admin/api/providers/-/test", "POST")).toHaveLength(1),
    );
    fireEvent.click(dialog.getByRole("button", { name: "Back" }));
    choose(dialog, "generic", /Generic OpenAI-compatible/);
    // Let the held answer arrive while Generic is the chosen provider.
    await act(async () => {
      release();
      await Promise.all(vi.mocked(fetch).mock.results.map((r) => r.value));
    });

    expect(checkButton(dialog)).toBeDisabled();
    expect(dialog.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(dialog.queryByText("Listed 1 model from OpenRouter.")).toBeNull();

    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "sk-provider-b-secret" },
    });
    fireEvent.click(checkButton(dialog));
    expect(await dialog.findByText("Listed 1 model from Generic OpenAI.")).toBeVisible();
    fireEvent.click(dialog.getByRole("button", { name: "Next" }));
    expect(dialog.getByText("generic-openai/anthropic/claude-sonnet-4.5")).toBeVisible();
    const checks = callsTo(calls, "/admin/api/providers/-/test", "POST");
    expect(checks).toHaveLength(2);
    expect(checks[0].body).toMatchObject({ provider_id: "openrouter", api_key: "sk-provider-a-secret" });
    expect(checks[1].body).toMatchObject({ provider_id: "generic-openai", api_key: "sk-provider-b-secret" });
  });
});

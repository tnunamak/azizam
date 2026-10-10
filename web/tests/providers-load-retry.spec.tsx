import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ProvidersPage from "@/app/providers/page";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each(["/admin/api/providers/-/catalog", "/admin/api/profiles"])(
  "shows the existing error and retries failed %s metadata",
  async (failedPath) => {
    let failing = true;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      return path === failedPath && failing
        ? Response.json(
            { error: { message: "Metadata load failed" } },
            { status: 503 },
          )
        : Response.json(
            path.includes("profiles") ? { plans: [] } : { providers: [] },
          );
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <ProvidersPage />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Metadata load failed",
    );
    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry providers" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input) === failedPath),
    ).toHaveLength(2);
    client.clear();
  },
);

it.each([
  ["/admin/api/providers/-/catalog", false],
  ["/admin/api/profiles", false],
  ["/admin/api/providers/-/catalog", true],
  ["/admin/api/profiles", true],
] as const)(
  "recovers failed %s metadata inside Add provider (already open: %s)",
  async (failedPath, alreadyOpen) => {
    let failing = !alreadyOpen;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/session")) {
        return Response.json({ actor: { csrf_token: "test-csrf" } });
      }
      if (path === failedPath && failing) {
        return Response.json(
          { error: { message: "Metadata load failed" } },
          { status: 503 },
        );
      }
      return Response.json(
        path.endsWith("/catalog")
          ? {
              providers: [
                {
                  id: "test",
                  name: "Test provider",
                  base_url: "https://example.test",
                },
              ],
            }
          : path.endsWith("/profiles")
            ? { plans: [{ id: "test-plan", name: "Test plan" }] }
            : path.endsWith("/-/test")
              ? {
                  ok: true,
                  models: [
                    { id: "test-model", price_input: "1", price_output: "2" },
                  ],
                }
              : { providers: [] },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    try {
      render(
        <QueryClientProvider client={client}>
          <ProvidersPage />
        </QueryClientProvider>,
      );
      if (alreadyOpen) {
        await screen.findByText("No providers yet");
      } else {
        await screen.findByRole("alert");
      }
      fireEvent.click(
        screen.getAllByRole("button", { name: "Add provider" })[0],
      );
      const dialog = within(screen.getByRole("dialog"));
      if (alreadyOpen) {
        fireEvent.click(dialog.getByRole("button", { name: /Test provider/ }));
        fireEvent.change(dialog.getByLabelText("Provider API key"), {
          target: { value: "test-key" },
        });
        fireEvent.click(
          dialog.getByRole("button", { name: "Check provider connection" }),
        );
        await dialog.findByText("Connection worked.");
        fireEvent.click(dialog.getByRole("button", { name: "4. limits" }));
        expect(
          dialog.getByRole("button", { name: "Save provider" }),
        ).toBeEnabled();
        failing = true;
        await act(async () => {
          await client.refetchQueries({
            queryKey: [
              "providers",
              failedPath.endsWith("/catalog") ? "catalog" : "plans",
            ],
          });
        });
      }
      expect(await dialog.findByRole("alert")).toHaveTextContent(
        "Metadata load failed",
      );
      for (const name of ["2. credentials", "3. models", "4. limits"]) {
        expect(dialog.getByRole("button", { name })).toBeDisabled();
      }
      expect(
        dialog.getByRole("button", {
          name: alreadyOpen ? "Save provider" : "Next",
        }),
      ).toBeDisabled();
      expect(dialog.queryByLabelText("Search providers")).toBeNull();
      expect(dialog.queryByLabelText("Allow Test plan")).toBeNull();
      failing = false;
      fireEvent.click(dialog.getByRole("button", { name: "Retry providers" }));
      await waitFor(() => expect(dialog.queryByRole("alert")).toBeNull());
      // Retry restores the steps whose prerequisites were met before the
      // failure; with no provider chosen yet, later steps stay gated (SE-27).
      expect(dialog.getByRole("button", { name: "4. limits" })).toHaveProperty(
        "disabled",
        !alreadyOpen,
      );
      if (alreadyOpen) {
        expect(dialog.getByLabelText("Allow Test plan")).toBeInTheDocument();
        expect(
          dialog.getByRole("button", { name: "Save provider" }),
        ).toBeEnabled();
      } else {
        expect(
          dialog.getByRole("button", { name: /Test provider/ }),
        ).toBeInTheDocument();
      }
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input) === failedPath),
      ).toHaveLength(alreadyOpen ? 3 : 2);
    } finally {
      client.clear();
    }
  },
);

async function renderProviderRegression(checkResponse: unknown, existing = false) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith("/session")) {
      return Response.json({ actor: { csrf_token: "test-csrf" } });
    }
    if (path.endsWith("/-/test")) return Response.json(checkResponse);
    if (path.endsWith("/catalog")) {
      return Response.json({
        providers: [{ id: "test", name: "Test provider", base_url: "https://example.test" }],
      });
    }
    if (path.endsWith("/profiles")) return Response.json({ plans: [] });
    if (init?.method && init.method !== "GET") return Response.json({});
    return Response.json({
      providers: existing ? [{ id: "test", name: "Test provider", caps: { daily_usd: 5, monthly_usd: 50 } }] : [],
      global: { caps: { daily_usd: 5, monthly_usd: 50 } },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
  await screen.findByText(existing ? "Test provider" : "No providers yet");
  return { client, fetchMock };
}

it("finds a provider in the named list and its models in a table", async () => {
  const { client } = await renderProviderRegression({ ok: true }, true);
  try {
    const list = screen.getByRole("list", { name: "Providers" });
    const row = within(list).getByRole("listitem");
    expect(within(row).getByRole("heading", { name: "Test provider" })).toBeVisible();
    fireEvent.click(within(row).getByText("Manage"));
    const table = within(row).getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Model" })).toBeVisible();
    expect(within(table).getByRole("columnheader", {
      name: "Input: USD per 1 million tokens",
    })).toBeVisible();
    expect(within(table).getByRole("columnheader", {
      name: "Output: USD per 1 million tokens",
    })).toBeVisible();
  } finally {
    client.clear();
  }
});

async function checkRegressionProvider() {
  fireEvent.click(screen.getAllByRole("button", { name: "Add provider" })[0]);
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.click(dialog.getByRole("button", { name: /Test provider/ }));
  fireEvent.change(dialog.getByLabelText("Provider API key"), {
    target: { value: "test-key" },
  });
  fireEvent.click(dialog.getByRole("button", { name: "Check provider connection" }));
  await waitFor(() =>
    expect(dialog.getByRole("button", { name: "Check provider connection" })).toBeEnabled(),
  );
  return dialog;
}

it.each([
  ["missing", { message: "Connection worked." }, false, "Connection not confirmed."],
  ["null body", null, false, "Connection not confirmed."],
  ["malformed body", "success", false, "Connection not confirmed."],
  ["string evidence", { ok: "true", message: "Connection worked." }, false, "Connection not confirmed."],
  ["numeric evidence", { ok: 1 }, false, "Connection not confirmed."],
  ["null evidence", { ok: null }, false, "Connection not confirmed."],
  ["explicit failure", { ok: false }, false, "Connection failed."],
  ["explicit failure with a success message", { ok: false, message: "Connection worked." }, false, "Connection failed: Connection worked."],
  ["explicit success", { ok: true }, true, "Connection worked."],
] as const)(
  "requires explicit connection success evidence: %s",
  async (_name, response, confirmed, message) => {
    const { client } = await renderProviderRegression(response);
    try {
      const dialog = await checkRegressionProvider();
      expect(await dialog.findByText(message)).toBeInTheDocument();
      for (const name of ["Next", "3. models", "4. limits"]) {
        const button = dialog.getByRole("button", { name });
        if (confirmed) expect(button).toBeEnabled();
        else expect(button).toBeDisabled();
      }
      if (!confirmed) expect(dialog.queryByText("Connection worked.")).toBeNull();
    } finally {
      client.clear();
    }
  },
);

for (const target of ["add", "provider", "global"] as const) {
  it.each([
    ["blank", "", "  ", { daily_usd: null, monthly_usd: null }],
    ["zero", "0", "0", { daily_usd: 0, monthly_usd: 0 }],
    ["positive", "2.5", "10", { daily_usd: 2.5, monthly_usd: 10 }],
    ["negative daily", "-1", "5", null],
    ["negative monthly", "5", "-1", null],
    ["non-numeric daily", "bad", "5", null],
    ["non-numeric monthly", "5", "bad", null],
    ["non-finite daily", "Infinity", "5", null],
    ["non-finite monthly", "5", "1e999", null],
  ] as const)(
    `validates ${target} caps before saving: %s`,
    async (_name, daily, monthly, expected) => {
      const { client, fetchMock } = await renderProviderRegression({
        ok: true,
        models: [{ id: "test-model", price_input: "1", price_output: "2" }],
      }, target !== "add");
      try {
        let scope = within(document.body);
        let dailyLabel = "Daily cap (USD)";
        let monthlyLabel = "Monthly cap (USD)";
        let saveName = "Save provider";
        let savePath = "/admin/api/providers";
        if (target === "add") {
          scope = await checkRegressionProvider();
          await scope.findByText("Connection worked.");
          fireEvent.click(scope.getByRole("button", { name: "4. limits" }));
        } else if (target === "provider") {
          fireEvent.click(screen.getByText("Manage"));
          fireEvent.click(screen.getByRole("button", { name: "Edit caps" }));
          dailyLabel = "Test provider daily cap (USD)";
          monthlyLabel = "Test provider monthly cap (USD)";
          saveName = "Save caps";
          savePath = "/admin/api/providers/test/caps";
        } else {
          fireEvent.click(screen.getByText("Edit", { exact: true }));
          dailyLabel = "Global daily provider cap (USD)";
          monthlyLabel = "Global monthly provider cap (USD)";
          saveName = "Save global cap";
          savePath = "/admin/api/providers/-/global-caps";
        }
        fireEvent.change(scope.getByLabelText(dailyLabel), { target: { value: daily } });
        fireEvent.change(scope.getByLabelText(monthlyLabel), { target: { value: monthly } });
        fireEvent.click(scope.getByRole("button", { name: saveName }));
        const saves = () => fetchMock.mock.calls.filter(
          ([input, init]) => String(input) === savePath &&
            init?.method === (target === "add" ? "POST" : "PUT"),
        );
        if (expected === null) {
          expect(await scope.findByRole("alert")).toHaveTextContent(
            "Caps must be non-negative numbers or blank for no cap.",
          );
          expect(saves()).toHaveLength(0);
        } else {
          await waitFor(() => expect(saves()).toHaveLength(1));
          const body = JSON.parse(String(saves()[0][1]?.body));
          expect(target === "add" ? body.caps : body).toEqual(expected);
        }
      } finally {
        client.clear();
      }
    },
  );
}

it("shows a new cap validation error even after an older action failed", async () => {
  const { client } = await renderProviderRegression({ ok: true }, true);
  const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  try {
    fireEvent.click(screen.getByText("Manage"));
    const more = screen.getByRole("button", { name: "More actions for Test provider" });
    more.focus();
    fireEvent.keyDown(more, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: /Rotate key/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Rotation cancelled.");
    fireEvent.click(screen.getByRole("button", { name: "Edit caps" }));
    fireEvent.change(screen.getByLabelText("Test provider daily cap (USD)"), { target: { value: "-1" } });
    fireEvent.change(screen.getByLabelText("Test provider monthly cap (USD)"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save caps" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Caps must be non-negative numbers or blank for no cap.",
      ),
    );
  } finally {
    prompt.mockRestore();
    client.clear();
  }
});

it("gates wizard headers by every preceding Next prerequisite", async () => {
  const { client } = await renderProviderRegression({
    ok: true,
    models: [{ id: "test-model", price_input: "1", price_output: "2" }],
  });
  try {
    fireEvent.click(screen.getAllByRole("button", { name: "Add provider" })[0]);
    const dialog = within(screen.getByRole("dialog"));
    for (const name of ["Next", "2. credentials", "3. models", "4. limits"]) {
      expect(dialog.getByRole("button", { name })).toBeDisabled();
      fireEvent.click(dialog.getByRole("button", { name }));
    }
    expect(dialog.getByLabelText("Search providers")).toBeInTheDocument();
    fireEvent.click(dialog.getByRole("button", { name: /Test provider/ }));
    for (const name of ["Next", "3. models", "4. limits"]) {
      expect(dialog.getByRole("button", { name })).toBeDisabled();
    }
    expect(dialog.getByRole("button", { name: "2. credentials" })).toBeEnabled();
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "test-key" },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Check provider connection" }));
    await dialog.findByText("Connection worked.");
    for (const name of ["Next", "3. models", "4. limits"]) {
      expect(dialog.getByRole("button", { name })).toBeEnabled();
    }
    fireEvent.click(dialog.getByRole("button", { name: "3. models" }));
    expect(dialog.getByLabelText("test-model input price")).toBeInTheDocument();
    fireEvent.click(dialog.getByRole("button", { name: "4. limits" }));
    expect(dialog.getByLabelText("Daily cap (USD)")).toBeInTheDocument();
    fireEvent.click(dialog.getByRole("button", { name: "2. credentials" }));
    fireEvent.change(dialog.getByLabelText("Provider API key"), {
      target: { value: "changed-key" },
    });
    for (const name of ["Next", "3. models", "4. limits"]) {
      expect(dialog.getByRole("button", { name })).toBeDisabled();
    }
  } finally {
    client.clear();
  }
});

it("saves only the selected model row and refreshes clean drafts without losing pending edits", async () => {
  let models = [
    { id: "model-a", price_input: "1", price_output: "2", enabled: true },
    { id: "model-b", price_input: "3", price_output: "4", enabled: true },
  ];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith("/session")) {
      return Response.json({ actor: { csrf_token: "test-csrf" } });
    }
    if (path.endsWith("/catalog")) return Response.json({ providers: [] });
    if (path.endsWith("/profiles")) return Response.json({ plans: [] });
    if (path.endsWith("/test/models") && init?.method === "PUT") {
      // Like the real endpoint, the body replaces the whole model list.
      const body = JSON.parse(String(init.body));
      models = body.models.map((saved: {
        id: string;
        enabled: boolean;
        price: { input_usd_per_million_tokens: number; output_usd_per_million_tokens: number };
      }) => ({
        id: saved.id,
        enabled: saved.enabled,
        price_input: String(saved.price.input_usd_per_million_tokens),
        price_output: String(saved.price.output_usd_per_million_tokens),
      }));
      return Response.json({});
    }
    return Response.json({
      providers: [{ id: "test", name: "Test provider", models }],
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  try {
    render(
      <QueryClientProvider client={client}>
        <ProvidersPage />
      </QueryClientProvider>,
    );
    await screen.findByText("Test provider");
    fireEvent.click(screen.getByText("Manage"));
    fireEvent.click(screen.getByRole("button", { name: "Edit models" }));
    fireEvent.change(screen.getByLabelText("model-a managed input price"), {
      target: { value: "9" },
    });
    fireEvent.change(screen.getByLabelText("model-b managed input price"), {
      target: { value: "8" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "Save models" })[0]);
    const saves = () => fetchMock.mock.calls.filter(
      ([input, init]) => String(input) === "/admin/api/providers/test/models" &&
        init?.method === "PUT",
    );
    await waitFor(() => expect(saves()).toHaveLength(1));
    expect(JSON.parse(String(saves()[0][1]?.body))).toEqual({
      models: [
        {
          id: "model-a", name: "model-a", upstream_model: "model-a", enabled: true,
          price: { input_usd_per_million_tokens: 9, output_usd_per_million_tokens: 2 },
        },
        {
          id: "model-b", name: "model-b", upstream_model: "model-b", enabled: true,
          price: { input_usd_per_million_tokens: 3, output_usd_per_million_tokens: 4 },
        },
      ],
      expected_generation: null,
    });
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Save models" })[0]).toBeEnabled(),
    );
    expect(screen.getByLabelText("model-b managed input price")).toHaveValue("8");
    models = models.map((model) => ({
      ...model,
      price_input: model.id === "model-a" ? "10" : "6",
    }));
    await act(async () => {
      await client.refetchQueries({ queryKey: ["providers"], exact: true });
    });
    await waitFor(() =>
      expect(screen.getByLabelText("model-a managed input price")).toHaveValue("10"),
    );
    expect(screen.getByLabelText("model-b managed input price")).toHaveValue("8");
    fireEvent.change(screen.getByLabelText("model-b managed input price"), {
      target: { value: "6" },
    });
    models = models.map((model) =>
      model.id === "model-b" ? { ...model, price_input: "7" } : model,
    );
    await act(async () => {
      await client.refetchQueries({ queryKey: ["providers"], exact: true });
    });
    await waitFor(() =>
      expect(screen.getByLabelText("model-b managed input price")).toHaveValue("7"),
    );
  } finally {
    client.clear();
  }
});

it("treats a saved price typed as 9.0 as clean once the server stores 9", async () => {
  let models = [{ id: "model-a", price_input: "1", price_output: "2", enabled: true }];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith("/session")) {
      return Response.json({ actor: { csrf_token: "test-csrf" } });
    }
    if (path.endsWith("/catalog")) return Response.json({ providers: [] });
    if (path.endsWith("/profiles")) return Response.json({ plans: [] });
    if (path.endsWith("/test/models") && init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      models = body.models.map((saved: {
        id: string;
        enabled: boolean;
        price: { input_usd_per_million_tokens: number; output_usd_per_million_tokens: number };
      }) => ({
        id: saved.id,
        enabled: saved.enabled,
        price_input: String(saved.price.input_usd_per_million_tokens),
        price_output: String(saved.price.output_usd_per_million_tokens),
      }));
      return Response.json({});
    }
    return Response.json({
      providers: [{ id: "test", name: "Test provider", models }],
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  try {
    render(
      <QueryClientProvider client={client}>
        <ProvidersPage />
      </QueryClientProvider>,
    );
    await screen.findByText("Test provider");
    fireEvent.click(screen.getByText("Manage"));
    fireEvent.click(screen.getByRole("button", { name: "Edit models" }));
    fireEvent.change(screen.getByLabelText("model-a managed input price"), {
      target: { value: "9.0" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save models" }));
    await waitFor(() => expect(models[0].price_input).toBe("9"));
    await waitFor(() =>
      expect(screen.getByLabelText("model-a managed input price")).toHaveValue("9"),
    );
    models = [{ ...models[0], price_input: "10" }];
    await act(async () => {
      await client.refetchQueries({ queryKey: ["providers"], exact: true });
    });
    await waitFor(() =>
      expect(screen.getByLabelText("model-a managed input price")).toHaveValue("10"),
    );
  } finally {
    client.clear();
  }
});

async function renderProviderOverview(count = 1, unknownSpend = false) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/catalog")) return Response.json({ providers: [] });
    if (path.endsWith("/profiles")) {
      return Response.json({
        plans: [
          { id: "family", name: "Family plan" },
          { id: "guests", name: "Guest plan" },
        ],
      });
    }
    return Response.json({
      providers: Array.from({ length: count }, (_, index) => ({
        id: `provider-${index}`,
        name: `Household provider ${index + 1}`,
        enabled: true,
        reachable: true,
        key_set: true,
        spend: unknownSpend
          ? { status: "unknown" }
          : { daily_usd: 2, monthly_usd: 12 },
        caps: unknownSpend ? {} : { daily_usd: 5, monthly_usd: 50 },
        models: [
          { id: "enabled-model", enabled: true },
          { id: "disabled-model", enabled: false },
        ],
        allowed_plans: ["family"],
      })),
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ProvidersPage />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "Household provider 1" });
  return { client, fetchMock };
}

it("shows a single provider's state, spend, enabled models and allowed plans before Manage", async () => {
  const { client, fetchMock } = await renderProviderOverview();
  try {
    const list = screen.getByRole("list", { name: "Providers" });
    const row = within(list).getByRole("listitem");
    const scope = within(row);
    const manage = scope.getByText("Manage");
    const disclosure = manage.closest("details")!;
    expect(disclosure).not.toHaveAttribute("open");
    expect(scope.getByText("Ready")).toBeVisible();
    expect(scope.getByText("USD 12.00 of USD 50.00")).toBeVisible();
    expect(scope.getByText("USD 2.00 of USD 5.00")).toBeVisible();
    expect(scope.getByRole("progressbar", { name: "This month spend" })).toBeVisible();
    expect(scope.getByRole("heading", { name: "Enabled models" })).toBeVisible();
    expect(scope.getByRole("heading", { name: "Access plans" })).toBeVisible();
    const enabledModel = scope.getAllByText("enabled-model").find((node) => !disclosure.contains(node));
    expect(enabledModel).toBeVisible();
    expect(scope.getByText("Family plan")).toBeVisible();
    expect(scope.queryByText("Guest plan")).toBeNull();
    expect(scope.getAllByText("disabled-model").every((node) => disclosure.contains(node))).toBe(true);
    expect(list.compareDocumentPosition(screen.getByRole("heading", { name: "Global spend cap" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(manage);
    expect(scope.getByRole("button", { name: "Edit models" })).toBeVisible();
    fireEvent.click(manage);
    expect(scope.getByText("USD 12.00 of USD 50.00")).toBeVisible();
    expect(scope.getByText("Family plan")).toBeVisible();
    expect(fetchMock.mock.calls.map(([input]) => String(input)).sort()).toEqual([
      "/admin/api/profiles",
      "/admin/api/providers",
      "/admin/api/providers/-/catalog",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
    expect(screen.getByRole("dialog")).toBeVisible();
  } finally {
    client.clear();
  }
});

it("keeps unknown spend and unset caps visible in the single-provider overview", async () => {
  const { client } = await renderProviderOverview(1, true);
  try {
    const row = within(screen.getByRole("list", { name: "Providers" })).getByRole("listitem");
    for (const label of ["Cost not recorded", "Spend unknown · No cap set"]) {
      const values = within(row).getAllByText(label);
      expect(values).toHaveLength(2);
      values.forEach((value) => expect(value).toBeVisible());
    }
    expect(within(row).queryByRole("progressbar")).toBeNull();
  } finally {
    client.clear();
  }
});

it("keeps multiple providers as collapsed management rows", async () => {
  const { client } = await renderProviderOverview(2);
  try {
    const rows = within(screen.getByRole("list", { name: "Providers" })).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      const scope = within(row);
      expect(scope.getByText("Manage").closest("details")).not.toHaveAttribute("open");
      expect(scope.getByText("Family plan")).not.toBeVisible();
      fireEvent.click(scope.getByText("Manage"));
      expect(scope.getByText("Family plan")).toBeVisible();
      expect(scope.getByRole("button", { name: "Edit caps" })).toBeVisible();
    }
  } finally {
    client.clear();
  }
});

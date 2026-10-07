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
      expect(dialog.getByRole("button", { name: "4. limits" })).toBeEnabled();
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

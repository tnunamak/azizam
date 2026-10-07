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

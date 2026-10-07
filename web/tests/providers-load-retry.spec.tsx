import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
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

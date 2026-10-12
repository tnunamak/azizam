// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { createElement } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import HomePage from "@/app/_home/home-page";
import { homeApi } from "@/app/_home/home-api";
import { accountApi } from "@/lib/account-api";
import type { HomeAlias } from "@/app/_home/home-api";

vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: "member", admin: false, name: "Alex" }),
}));
vi.mock("@/lib/use-capability-views", () => ({
  useCapabilityViews: () => ({
    views: [],
    requests: [],
    verdict: { tone: "off", text: "No tasks available", problems: [], checkedAt: null, unconfirmed: false },
  }),
}));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const alias = (
  id: string,
  operation: string,
  status: HomeAlias["status"] = "ready",
): HomeAlias => ({
  id,
  operation,
  status,
  available: status === "ready",
  reason_code: status === "ready" ? "ready" : "unavailable_disabled",
});

function renderHome(aliases: HomeAlias[] = [], assets: object[] = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(["account", "home"], {
    authenticated: true,
    user: { id: "alex", role: "member", name: "Alex", email: "alex@example.test" },
    apps: [], capabilities: [], aliases, new_app_aliases: aliases, receipts: [],
    api_base_url: "https://example.test/v1", public_base_url: "https://example.test",
  });
  client.setQueryData(["library", "home-recent-creations"], { assets });
  render(createElement(QueryClientProvider, { client }, createElement(HomePage)));
  return client;
}


it("retries a partial key creation on the same app without creating a second one", async () => {
  const makeApp = (id: string, name: string) =>
    ({ id, name } as Awaited<ReturnType<typeof accountApi.createApp>>);
  const create = vi.spyOn(accountApi, "createApp")
    .mockResolvedValue(makeApp("fixture-app-1", "Open WebUI"));
  const mint = vi.spyOn(accountApi, "mintKey")
    .mockRejectedValueOnce(new Error("fixture mint failure"))
    .mockResolvedValue({
      id: "fixture-key",
      api_key: "fixture-secret-not-a-real-key",
    } as Awaited<ReturnType<typeof accountApi.mintKey>>);

  const client = renderHome([alias("allowed-chat", "chat")]);
  vi.spyOn(client, "invalidateQueries").mockResolvedValue(undefined);
  fireEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
  fireEvent.click(screen.getByRole("button", { name: "Create key for Open WebUI" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent)
      .toBe("We couldn't tell where key creation stopped. Try again."),
  );
  expect(create.mock.calls).toEqual([["Open WebUI"]]);
  expect(mint.mock.calls).toEqual([["fixture-app-1", "Open WebUI"]]);

  fireEvent.click(screen.getByRole("button", { name: "Retry key creation" }));
  await waitFor(() => expect(mint).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(create.mock.calls).toEqual([["Open WebUI"]]);
  expect(mint.mock.calls).toEqual([
    ["fixture-app-1", "Open WebUI"],
    ["fixture-app-1", "Open WebUI"],
  ]);
});

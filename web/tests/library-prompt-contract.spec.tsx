import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import LibraryPage from "@/app/library/page";
import { ViewerContext } from "@/components/shell/session";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

function mountPromptContract(personId: string, remember: boolean) {
  const unexpected: string[] = [];
  const asset = {
    asset_id: "image",
    kind: "image",
    prompt: null,
    model: "",
    created_at: null,
    download_url: "/fixture.png",
    source: { kind: "app", label: "Fixture app", person_id: personId },
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "https://fixture.invalid");
    const method = init?.method ?? "GET";
    if (method === "GET" && url.pathname === "/me/api/models/catalog")
      return Response.json({ aliases: [], models: [] });
    if (method === "GET" && url.pathname === "/me/api/home")
      return Response.json({
        authenticated: true,
        user: { id: "me", name: "Alex", role: "member", email: null },
        apps: [], receipts: [], capabilities: [], aliases: [],
      });
    if (method === "GET" && url.pathname === "/me/api/media")
      return Response.json({ assets: [asset], models: [], next_cursor: null });
    if (method === "GET" && url.pathname === "/me/api/media/image/download")
      return Response.json({ download_url: "/fixture.png" });
    if (method === "GET" && url.pathname === "/me/api/prompt-memory")
      return Response.json({
        prompt_memory: {
          remember,
          retention: "never",
          effective_remember: remember,
          effective_retention: "never",
          household: {
            default_remember: true,
            never_keep: false,
            retention_cap: "never",
          },
        },
      });
    // These are synthetic responses, so an accidental early deletion is
    // observable rather than swallowed as an unrelated request error.
    if (method === "GET" && url.pathname === "/me/api/session")
      return Response.json({ csrf_token: "fixture-csrf" });
    if (method === "DELETE" && url.pathname === "/me/api/media/image")
      return new Response(null, { status: 204 });
    unexpected.push(method + " " + String(input));
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={{
        authenticated: true, id: "me", name: "Alex",
        email: null, role: "member", admin: false,
      }}>
        <LibraryPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  return {
    client, fetchMock, unexpected,
    dispose() { view.unmount(); client.clear(); },
  };
}

it.each([
  { remember: true, expected: "No saved prompt" },
  { remember: false, expected: "No saved prompt. You turned off prompt keeping." },
])("renders the maker's missing-prompt explanation: $remember", async ({ remember, expected }) => {
  const user = userEvent.setup();
  const fixture = mountPromptContract("me", remember);
  try {
    await user.click(await screen.findByRole("button", { name: "Open Picture" }));
    await waitFor(() => expect(
      fixture.client.getQueryState(["account", "prompt-memory"]),
    ).toMatchObject({ status: "success", fetchStatus: "idle" }));
    const dialog = screen.getByRole("dialog", { name: "Picture" });
    expect(await within(dialog).findByText(expected, { exact: true })).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: "Copy prompt" })).toBeNull();
    expect(fixture.unexpected).toEqual([]);
  } finally {
    fixture.dispose();
  }
});

it("does not reveal missing-prompt history or load settings for another maker", async () => {
  const user = userEvent.setup();
  const fixture = mountPromptContract("someone-else", false);
  try {
    await user.click(await screen.findByRole("button", { name: "Open Picture" }));
    await waitFor(() => expect(
      fixture.client.getQueryState(["library", "download", "image", false]),
    ).toMatchObject({ status: "success", fetchStatus: "idle" }));
    const dialog = screen.getByRole("dialog", { name: "Picture" });
    expect(within(dialog).queryByText(/No saved prompt|turned off prompt keeping/)).toBeNull();
    expect(fixture.fetchMock.mock.calls.filter(([input]) =>
      new URL(String(input), "https://fixture.invalid").pathname === "/me/api/prompt-memory",
    )).toHaveLength(0);
    expect(fixture.unexpected).toEqual([]);
  } finally {
    fixture.dispose();
  }
});

it("shows the actual deletion scope before confirmation and Cancel deletes nothing", async () => {
  const user = userEvent.setup();
  const fixture = mountPromptContract("me", true);
  try {
    await user.click(await screen.findByRole("button", { name: "Open Picture" }));
    await user.click(screen.getByRole("button", { name: "Actions for Picture" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    const confirmation = await screen.findByRole("dialog", { name: "Delete Picture?" });
    expect(confirmation).toHaveAccessibleDescription(
      "Removes this creation from your Library. Downloaded copies stay.",
    );
    expect(fixture.fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE"))
      .toHaveLength(0);
    await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(
      screen.queryByRole("dialog", { name: "Delete Picture?" }),
    ).toBeNull());
    expect(fixture.fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE"))
      .toHaveLength(0);
    expect(fixture.unexpected).toEqual([]);
  } finally {
    fixture.dispose();
  }
});

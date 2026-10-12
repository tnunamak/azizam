import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import HomePage from "../home-page";
import { ViewerContext } from "@/components/shell/session";

// Moved down from the bugfix-home browser spec. The fixtures are the ones the
// browser spec intercepted at the network edge: one chat alias that was
// renamed, a chat capability that recommends a different model, and no apps.

const renamedAlias = {
  id: "renamed-paid-chat",
  operation: "chat.completions",
  available: true,
  status: "ready",
  reason: "Chat alias is ready.",
};

const focusedHome = {
  authenticated: true,
  user: { email: "browser@example.test", name: "Browser" },
  api_base_url: "http://127.0.0.1:18180/openai/v1",
  public_base_url: "http://127.0.0.1:18180",
  apps: [],
  aliases: [renamedAlias],
  capabilities: [
    ["chat", "Chat"],
    ["image", "Images"],
    ["video", "Video"],
    ["speech", "Speech"],
    ["transcribe", "Transcription"],
    ["decide", "Decisions"],
    ["rerank", "Reranking"],
  ].map(([capability, capability_title]) => ({
    capability,
    capability_title,
    intent: capability === "chat" ? "chat.completions" : `${capability}.generate`,
    model: capability === "chat" ? "fast" : capability,
    use_name: capability === "chat" ? "fast" : capability,
    icon_key: capability,
    available: capability !== "rerank",
    status: capability === "rerank" ? "unavailable" : "ready",
    reason:
      capability === "rerank"
        ? "Reranking is not available."
        : `${capability_title} is ready.`,
  })),
  credential_aliases: {},
  new_app_aliases: [renamedAlias],
  receipts: [],
  creations: null,
  status: { state: "ready", reason: "Ready" },
};

const emptyMedia = () => Response.json({ assets: [], next_cursor: null });

function stubGateway(media: () => Response = emptyMedia) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/me/api/home") return Response.json(focusedHome);
    if (path === "/admin/api/setup-progress")
      return Response.json({ complete: true, steps: [] });
    if (path.startsWith("/me/api/media")) return media();
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderHome() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: "Browser",
          email: "browser@example.test",
          role: "member",
          admin: false,
        }}
      >
        <HomePage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Home (bugfix regressions)", () => {
  it("opens Try in chat mode with the home's chat model, not the catalog's first alias", async () => {
    stubGateway();
    renderHome();
    await screen.findByRole("heading", { name: "Make something" });
    const links = () =>
      Array.from(
        document.querySelectorAll<HTMLAnchorElement>(
          'a[href*="/try"][href*="capability=chat"]',
        ),
      );
    await waitFor(() => expect(links()).toHaveLength(1));
    const chatLinks = links();
    // Next adds the trailing slash at build time; jsdom renders the bare path.
    expect(chatLinks[0].getAttribute("href")).toMatch(/^\/try\/?\?capability=chat&model=fast$/);
  });

  it("picks the app recipe before creating a key", async () => {
    stubGateway();
    renderHome();
    await screen.findByRole("button", { name: "Open WebUI" });
    expect(screen.queryByRole("textbox", { name: "Base URL" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Create key for/ })).toBeNull();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Open WebUI" }));
    expect(screen.getByRole("button", { name: "Create key for Open WebUI" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Another app" }));
    expect(screen.getByRole("button", { name: "Create key for Python" })).toBeVisible();
  });

  it("connects a renamed paid chat model without a default-model warning", async () => {
    stubGateway();
    renderHome();
    await userEvent.setup().click(
      await screen.findByRole("button", { name: "Open WebUI" }),
    );
    expect(screen.getByRole("textbox", { name: "Model" })).toHaveValue(
      "renamed-paid-chat",
    );
    expect(screen.queryByText(/This key has no chat models|default model/)).toBeNull();
    expect(await screen.findByText("Your creations will appear here.")).toBeVisible();
  });

  it("explains a failed recent fetch without the status code, then retries it", async () => {
    let failed = true;
    stubGateway(() =>
      failed ? Response.json({}, { status: 503 }) : emptyMedia(),
    );
    renderHome();
    const recent = await screen.findByRole("region", { name: "Recent" });
    expect(
      await within(recent).findByText(
        "We couldn't tell why your recent creations didn't load. Try again.",
      ),
    ).toBeVisible();
    expect(recent).not.toHaveTextContent("503");
    failed = false;
    await userEvent.setup().click(within(recent).getByRole("button", { name: "Try again" }));
    expect(await within(recent).findByText("Your creations will appear here.")).toBeVisible();
  });
});

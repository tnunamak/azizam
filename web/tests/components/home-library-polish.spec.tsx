import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import HomePage from "@/app/home/page";
import LibraryPage from "@/app/library/page";
import { ViewerContext } from "@/components/shell/session";

function renderPage(page: React.ReactElement, owner = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider
        value={{
          authenticated: true,
          name: "Alex",
          email: null,
          role: owner ? "owner" : "member",
          admin: owner,
        }}
      >
        {page}
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
const home = {
  authenticated: true,
  user: { id: "alex", name: "Alex", email: null, role: "member" },
  apps: [],
  public_base_url: "https://gateway.example",
  api_base_url: "https://gateway.example/openai/v1",
  capabilities: [
    {
      capability: "image",
      intent: "image.generate",
      model: "image",
      available: true,
      status: "ready",
      reason: "Images are ready.",
    },
  ],
  aliases: [],
  credential_aliases: {},
  new_app_aliases: [],
  status: { state: "ready", reason: "Your models are ready." },
  receipts: [],
};
const assets = [
  {
    asset_id: "image1",
    kind: "image",
    prompt: "An ocean",
    model: "image",
    created_at: 1700000000,
    download_url: "/signed/image1",
  },
  {
    asset_id: "video1",
    kind: "video",
    prompt: "Swimming fish",
    model: "video",
    created_at: 1700000000,
    download_url: "/signed/video1",
    duration_s: 6,
  },
  {
    asset_id: "audio1",
    kind: "audio",
    prompt: "Ocean sounds",
    model: "voice",
    created_at: 1700000000,
    download_url: "/signed/audio1",
    duration_s: 12,
  },
];
describe("Home and Library polish", () => {
  it("omits greetings and shows connection steps, capability chips, and recent media", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        switch (String(input)) {
          case "/me/api/home":
            return Response.json(home);
          case "/me/api/media?limit=50":
            return Response.json({ assets: [assets[0]] });
          default:
            throw new Error(`Unexpected request: ${input}`);
        }
      }),
    );
    const { container } = renderPage(<HomePage />);
    expect(
      await screen.findByRole("heading", { name: "Connect an app" }),
    ).toBeVisible();
    expect(screen.queryByText(/^(Welcome to|Hello,)/)).toBeNull();
    // The page outline keeps an h1, but no visible title repeats the navigation.
    expect(screen.getByRole("heading", { level: 1, name: "Home" })).toHaveClass("sr-only");
    const connect = screen.getByRole("region", { name: "Connect an app" });
    expect(
      within(connect).getByText("One address. A key for each app."),
    ).toBeVisible();
    expect(
      within(connect).queryByRole("button", { name: "Check connection" }),
    ).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
    expect(within(connect).getByLabelText("Base URL")).toBeVisible();
    expect(within(connect).getByText("API key", { exact: true })).toBeVisible();
    expect(
      within(connect).getByRole("button", {
        name: "Create key for Open WebUI",
      }),
    ).toBeVisible();
    expect(
      within(connect).getByRole("button", { name: "Check connection" }),
    ).toBeVisible();
    expect(
      within(connect).getByRole("button", { name: "Setup notes" }),
    ).toBeVisible();
    expect(await screen.findByText("An ocean")).toBeVisible();
    expect(
      container.querySelector('img[src="/signed/image1"]'),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Images" })).toHaveClass(
      "bg-capability-image/5",
    );
  });
  it("keeps setup notes and key checks inside the selected app flow", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        switch (String(input)) {
          case "/me/api/home":
            return Response.json({
              ...home,
              receipts: [
                {
                  request_id: "real-request",
                  status: "succeeded",
                  requested_model: "image",
                  intent: "image.generate",
                },
              ],
            });
          case "/me/api/media?limit=50":
            return Response.json({ assets: [assets[0]] });
          default:
            throw new Error(`Unexpected request: ${input}`);
        }
      }),
    );
    renderPage(<HomePage />);
    const connect = await screen.findByRole("region", {
      name: "Connect an app",
    });
    await userEvent.click(
      await within(connect).findByRole("button", { name: "Open WebUI" }),
    );
    expect(
      within(connect).getByRole("button", {
        name: "Create key for Open WebUI",
      }),
    ).toBeVisible();
    expect(
      within(connect).getByRole("button", { name: "Check connection" }),
    ).toBeVisible();
    expect(
      within(connect).getByRole("button", { name: "Setup notes" }),
    ).toBeVisible();
    expect(screen.queryByText(/App setup:/)).toBeNull();
    expect(screen.queryByText(/Confirm only what you did/)).toBeNull();
    expect(await screen.findByText("An ocean")).toBeVisible();
    await userEvent.click(
      within(connect).getByRole("button", { name: "Setup notes" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Setup notes" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Recent requests" }),
    ).toBeNull();
    expect(screen.queryByText("real-request")).toBeNull();
  });
  it("shows capability chips without readiness labels and gives unusable chips a reason", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        switch (String(input)) {
          case "/me/api/home":
            return Response.json({
              ...home,
              capabilities: [
                {
                  capability: "image",
                  intent: "image.generate",
                  model: "image",
                  available: false,
                  status: "unavailable",
                  reason_code: "unavailable_unconfigured",
                },
                {
                  capability: "decide",
                  intent: "decision.systemone",
                  model: "decide",
                  available: true,
                  status: "standby_stopped",
                  reason: "Starts when you send a request.",
                },
              ],
            });
          case "/me/api/media?limit=50":
            return Response.json({ assets: [] });
          default:
            throw new Error(`Unexpected request: ${input}`);
        }
      }),
    );
    renderPage(<HomePage />);
    await screen.findByRole("heading", { name: "What you can use" });
    expect(
      screen.getByRole("link", {
        name: "Images: Unavailable. Ask the owner to finish setup.",
      }),
    ).toHaveAttribute("title", "Unavailable. Ask the owner to finish setup.");
    expect(screen.getByRole("link", { name: "Decisions" })).toBeVisible();
    expect(screen.queryByText("Unknown")).toBeNull();
    expect(screen.queryByText("Ready to start")).toBeNull();
    expect(screen.queryByRole("button", { name: "Details" })).toBeNull();
    expect(screen.queryByText("Unavailable right now")).toBeNull();
  });
  it("keeps audio controls outside the open action and uses a duration fallback for video", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ assets, models: [] })),
    );
    const { container } = renderPage(<LibraryPage />);
    await screen.findByRole("button", { name: "Open Ocean sounds" });
    const audio = container.querySelector("audio");
    expect(audio).not.toHaveAttribute("controls");
    expect(audio).toHaveAttribute("preload", "metadata");
    const play = screen.getByRole("button", { name: "Play Ocean sounds" });
    expect(
      screen.getByRole("button", { name: "Open Ocean sounds" }).contains(play),
    ).toBe(false);
    expect(audio).toHaveAttribute("src", "/signed/audio1");
    expect(audio?.closest("button")).toBeNull();
    expect(container.querySelector("video")).not.toBeNull();
    expect(screen.getByText("0:06")).toBeVisible();
    expect(screen.getByText("0:12")).toBeVisible();
    const playAudio = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue();
    await userEvent.click(play);
    expect(playAudio).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "Open Ocean sounds" }),
    );
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(screen.getByLabelText("Generated audio")).toHaveAttribute(
      "controls",
    );
  });
  it("resolves a supplied poster through the scoped signed-download API", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        requests.push(path);
        if (path.includes("/poster1/download"))
          return Response.json({ download_url: "/signed/poster1" });
        return Response.json({
          assets: [{ ...assets[1], poster_asset_id: "poster1" }],
          models: [],
        });
      }),
    );
    const { container } = renderPage(<LibraryPage />, true);
    await screen.findByRole("button", { name: "Open Swimming fish" });
    expect(requests).toContain("/me/api/media/poster1/download?scope=everyone");
    screen.getByRole("combobox", { name: "Owner" }).focus();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: () => {},
    });
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("option", { name: "Mine" }));
    await waitFor(() =>
      expect(requests).toContain("/me/api/media/poster1/download?scope=me"),
    );
  });
  it("does not invent an unknown duration and preserves kind filter requests", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requests.push(String(input));
        return Response.json({
          assets: [{ ...assets[1], duration_s: null }],
          models: [],
        });
      }),
    );
    renderPage(<LibraryPage />);
    await screen.findByRole("button", { name: "Open Swimming fish" });
    expect(screen.queryByText(/^\d+ s$/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Video" }));
    expect(screen.getByRole("button", { name: "Video" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() =>
      expect(requests).toContain("/me/api/media?limit=40&kind=video"),
    );
  });
});

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
    // Every page has the same header: a visible title and the verdict line.
    expect(screen.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    // This snapshot carries no check time, so nothing may claim Ready.
    const header = screen.getByRole("heading", { level: 1, name: "Home" }).closest("header")!;
    expect(within(header).getByRole("status")).toHaveTextContent("Images unknown");
    const connect = screen.getByRole("region", { name: "Connect an app" });
    // The address belongs to an app's recipe, so it waits for the choice.
    expect(within(connect).queryByLabelText("Gateway address")).toBeNull();
    expect(
      within(connect).queryByRole("button", { name: "Check connection" }),
    ).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Open WebUI" }));
    // The recipe's base URL is the gateway address, so it is not repeated.
    expect(within(connect).queryByLabelText("Base URL")).toBeNull();
    expect(within(connect).getByLabelText("Gateway address")).toHaveValue(
      home.api_base_url,
    );
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
    // Recent tiles carry the prompt in their accessible name, not as text.
    expect(
      await screen.findByRole("link", {
        name: "Open Image in Library: An ocean",
      }),
    ).toBeVisible();
    expect(screen.queryByText("An ocean")).toBeNull();
    expect(
      container.querySelector(
        'section[aria-labelledby="recent-heading"] img[src="/signed/image1"]',
      ),
    ).toHaveAttribute("alt", "An ocean");
    // Tasks come first, in the member's words, and open Try.
    const picture = screen.getByRole("link", { name: /Make a picture/ });
    expect(picture).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/try\/?\?capability=image(&|$)/),
    );
    // One row system: every task leads with its icon (the person's own work
    // is in Recent), and a Ready task carries no "Used …" note.
    expect(picture.querySelector("img")).toBeNull();
    expect(within(picture).queryByText(/^Used /)).toBeNull();
    // Ready stays quiet: only exceptions carry a state word.
    expect(within(picture).queryByText("Ready")).toBeNull();
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
    expect(
      await screen.findByRole("link", {
        name: "Open Image in Library: An ocean",
      }),
    ).toBeVisible();
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
  it("shows capability tiles with their state and gives unusable tiles a reason", async () => {
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
    await screen.findByRole("heading", { name: "Make something" });
    // An unusable task is not a link; it names its state and gives the reason.
    expect(screen.queryByRole("link", { name: /Make a picture/ })).toBeNull();
    const picture = screen.getByText("Make a picture").closest("li")!;
    expect(within(picture).getByText("Off")).toBeVisible();
    // The reason is a trailing fact that may be cut; its full text is its title.
    const reason = within(picture).getByText("Not set up yet.");
    expect(reason).toBeVisible();
    expect(reason).toHaveAttribute("title", "Not set up yet.");
    expect(within(picture).queryByText("Ready")).toBeNull();
    expect(screen.queryByText("Unknown")).toBeNull();
    expect(screen.queryByRole("button", { name: "Details" })).toBeNull();
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
    expect(audio).toHaveAttribute("preload", "none");
    const play = screen.getByRole("button", { name: "Play Ocean sounds" });
    expect(
      screen.getByRole("button", { name: "Open Ocean sounds" }).contains(play),
    ).toBe(false);
    expect(audio).not.toHaveAttribute("src");
    expect(audio?.closest("button")).toBeNull();
    expect(container.querySelector("video")).not.toBeNull();
    expect(screen.getByText("0:06")).toBeVisible();
    // D1.6: the length sits in the type badge; the play disc is the button.
    expect(screen.getByText("0:12")).toBeVisible();
    expect(play).not.toHaveTextContent("0:12");
    const playAudio = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockImplementation(function (this: HTMLMediaElement) {
        expect(this).toHaveAttribute(
          "src",
          expect.stringContaining("/signed/audio1"),
        );
        return Promise.resolve();
      });
    await userEvent.click(play);
    expect(playAudio).toHaveBeenCalledOnce();
    expect(audio).toHaveAttribute(
      "src",
      expect.stringContaining("/signed/audio1"),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "Open Ocean sounds" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeVisible();
    // The viewer draws its own player around the labelled audio element.
    expect(screen.getByLabelText("Generated audio")).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Play Ocean sounds" }),
    ).toBeVisible();
    expect(within(dialog).getByRole("slider", { name: "Position" })).toBeVisible();
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
    renderPage(<LibraryPage />, true);
    await screen.findByRole("button", { name: "Open Swimming fish" });
    expect(requests).toContain("/me/api/media/poster1/download?scope=everyone");
    screen.getByRole("combobox", { name: "Person" }).focus();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: () => {},
    });
    await userEvent.keyboard("{Enter}");
    await userEvent.click(
      await screen.findByRole("option", { name: "Alex (you)" }),
    );
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

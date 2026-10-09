import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import HomePage from "@/app/home/page";
import { ViewerContext } from "@/components/shell/session";

const home = {
  authenticated: true,
  user: { id: "alex", name: "Alex", email: null, role: "member" },
  apps: [],
  public_base_url: "https://gateway.example",
  api_base_url: "https://gateway.example/openai/v1",
  capabilities: [],
  aliases: [],
  credential_aliases: {},
  new_app_aliases: [],
  status: { state: "ready", reason: "Ready." },
  receipts: [],
};

function renderHome() {
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
          role: "member",
          admin: false,
        }}
      >
        <HomePage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

function mockHomeMedia(
  assets: unknown[],
  extra?: (path: string) => Response | Promise<Response> | undefined,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const response = extra?.(path);
      if (response) return response;
      if (path === "/me/api/home") return Response.json(home);
      if (path === "/me/api/media?limit=50") {
        return Response.json({ assets, has_more: false });
      }
      if (path.includes("/download?"))
        return Response.json({ download_url: "/renewed.wav" });
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Home recent media recovery", () => {
  it("HL-02 gives image and poster links names without prompts", async () => {
    mockHomeMedia(
      [
        { asset_id: "image", kind: "image", download_url: "/image.png" },
        {
          asset_id: "video",
          kind: "video",
          prompt: "   ",
          download_url: "/video.mp4",
          poster_asset_id: "poster",
        },
      ],
      (path) =>
        path.includes("/poster/download")
          ? Response.json({ download_url: "/poster.png" })
          : undefined,
    );

    renderHome();

    expect(
      await screen.findByRole("link", { name: "Open Image in Library" }),
    ).toBeVisible();
    expect(
      await screen.findByRole("link", { name: "Open Video in Library" }),
    ).toBeVisible();
  });

  it("HL-03 reports broken images and audio media errors", async () => {
    mockHomeMedia([
      { asset_id: "image", kind: "image", download_url: "/broken.png" },
      {
        asset_id: "audio",
        kind: "audio",
        prompt: "broken audio",
        download_url: "/broken.wav",
      },
    ]);

    const { container } = renderHome();
    await screen.findByRole("link", { name: "Open Image in Library" });
    fireEvent.error(container.querySelector('img[src="/broken.png"]')!);
    expect(
      await screen.findByRole("img", { name: "Image preview unavailable" }),
    ).toBeVisible();

    fireEvent.error(container.querySelector("audio")!);
    expect(
      await screen.findByText("Audio preview unavailable. Play to retry."),
    ).toBeVisible();
  });

  it("HL-03 resets audio failures when playback is retried", async () => {
    mockHomeMedia([
      {
        asset_id: "audio",
        kind: "audio",
        prompt: "spoken result",
        download_url: "/audio.wav",
      },
    ]);
    const load = vi
      .spyOn(HTMLMediaElement.prototype, "load")
      .mockImplementation(() => undefined);
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);

    const { container } = renderHome();
    const button = await screen.findByRole("button", {
      name: "Play spoken result",
    });
    fireEvent.error(container.querySelector("audio")!);
    await userEvent.click(button);

    expect(load).toHaveBeenCalled();
    expect(play).toHaveBeenCalled();
    expect(container.querySelector("audio")).toHaveAttribute(
      "src",
      "/renewed.wav",
    );
  });

  it("HL-03 announces blocked playback without leaving a playing state", async () => {
    mockHomeMedia([
      {
        asset_id: "audio",
        kind: "audio",
        prompt: "spoken result",
        download_url: "/audio.wav",
      },
    ]);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockRejectedValue(
      new Error("Blocked"),
    );
    renderHome();
    await userEvent.click(
      await screen.findByRole("button", { name: "Play spoken result" }),
    );
    expect(
      await screen.findByText("Playback didn't start. Try again."),
    ).toHaveAttribute("role", "status");
    expect(
      screen.getByRole("button", { name: "Play spoken result" }),
    ).toBeVisible();
  });

  it("HL-06 waits for poster resolution before using the video source", async () => {
    let resolvePoster!: (response: Response) => void;
    mockHomeMedia(
      [
        {
          asset_id: "video",
          kind: "video",
          prompt: "poster backed video",
          download_url: "/video.mp4",
          poster_asset_id: "poster",
        },
      ],
      (path) =>
        path.includes("/poster/download")
          ? new Promise<Response>((resolve) => {
              resolvePoster = resolve;
            })
          : undefined,
    );

    const { container } = renderHome();
    await screen.findByRole("link", {
      name: "Open Video in Library: poster backed video",
    });
    expect(container.querySelector("video")).toBeNull();

    resolvePoster(Response.json({ download_url: "/poster.png" }));
    await waitFor(() =>
      expect(container.querySelector('img[src="/poster.png"]')).toBeTruthy(),
    );
    expect(container.querySelector("video")).toBeNull();
  });

  it("HL-06 defers video sources until the tile enters view", async () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    let enter!: IntersectionObserverCallback;
    let observed!: Element;
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(callback: IntersectionObserverCallback) {
          enter = callback;
        }
        observe(element: Element) {
          observed = element;
        }
        disconnect() {}
      },
    );
    mockHomeMedia([
      { asset_id: "video", kind: "video", download_url: "/video.mp4" },
    ]);
    const { container } = renderHome();
    await screen.findByRole("link", { name: "Open Video in Library" });
    expect(container.querySelector("video")).not.toHaveAttribute("src");
    expect(container.querySelector("video")).not.toHaveAttribute("autoplay");
    act(() =>
      enter(
        [
          {
            isIntersecting: true,
            target: observed,
          } as IntersectionObserverEntry,
        ],
        {} as IntersectionObserver,
      ),
    );
    await waitFor(() =>
      expect(container.querySelector("video")).toHaveAttribute(
        "src",
        "/video.mp4",
      ),
    );
    const preview = container.querySelector("video")!;
    expect(preview).toHaveAttribute("autoplay");
    fireEvent.loadedData(preview);
    expect(pause).toHaveBeenCalledOnce();
  });
  it("HL-09 wraps long prompts and shows partial Recent status", async () => {
    const prompt = "x".repeat(500);
    const audioPrompt = "y".repeat(500);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(Response.json(home))
        .mockResolvedValueOnce(
          Response.json({
            assets: [
              {
                asset_id: "image",
                kind: "image",
                prompt,
                download_url: "/image.png",
              },
              {
                asset_id: "audio",
                kind: "audio",
                prompt: audioPrompt,
                download_url: "/audio.wav",
              },
            ],
            has_more: true,
            next_cursor: "older",
          }),
        )
        .mockResolvedValueOnce(
          Response.json({ error: { message: "failed" } }, { status: 503 }),
        ),
    );

    renderHome();

    // Tiles carry the prompt only in their accessible name (D1.5: audio too).
    expect(
      await screen.findByRole("link", {
        name: `Open Image in Library: ${prompt}`,
      }),
    ).toBeVisible();
    expect(screen.queryByText(prompt)).toBeNull();
    expect(
      screen.getByRole("link", { name: /^Open Audio in Library: y+/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^y+/)).toBeNull();
    expect(
      await screen.findByText("Recent results may be incomplete."),
    ).toBeVisible();
  });
});

describe("Home audio Library navigation", () => {
  it.each([
    ["spoken result", "Open Audio in Library: spoken result"],
    ["", "Open Audio in Library"],
  ])("HA-08 links audio to Library with prompt %j", async (prompt, name) => {
    mockHomeMedia([
      {
        asset_id: "audio/with space",
        kind: "audio",
        prompt,
        download_url: "/audio.wav",
      },
    ]);
    renderHome();

    expect(await screen.findByRole("link", { name })).toHaveAttribute(
      "href",
      "/library?asset=audio%2Fwith%20space",
    );
  });

  it("HA-08 keeps audio play and pause separate from the Library link", async () => {
    mockHomeMedia([
      {
        asset_id: "audio",
        kind: "audio",
        prompt: "spoken result",
        download_url: "/audio.wav",
      },
    ]);
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockImplementation(function (this: HTMLMediaElement) {
        fireEvent.play(this);
        return Promise.resolve();
      });
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(function (this: HTMLMediaElement) {
        fireEvent.pause(this);
      });
    renderHome();
    const link = await screen.findByRole("link", {
      name: "Open Audio in Library: spoken result",
    });
    const navigate = vi.fn();
    link.addEventListener("click", navigate);
    const button = screen.getByRole("button", { name: "Play spoken result" });
    expect(button.closest("a")).toBeNull();
    await userEvent.click(button);
    expect(play).toHaveBeenCalledOnce();
    await userEvent.click(
      await screen.findByRole("button", { name: "Pause spoken result" }),
    );
    expect(pause).toHaveBeenCalledOnce();
    expect(navigate).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Play spoken result" }),
    ).toBeVisible();
  });
});

describe("Home connection check errors", () => {
  it.each([
    {
      failure: 401,
      message:
        "The gateway check could not finish. Check your key and try again.",
    },
    {
      failure: 403,
      message:
        "The gateway check could not finish. Check your key and try again.",
    },
    {
      failure: 500,
      message: "The gateway check failed (HTTP 500). Try again.",
    },
    {
      failure: "network",
      // No HTTP answer: the cause is unknown, so no layer is named.
      message: "Check failed. Try again.",
    },
  ])("HA-05 classifies $failure connection failures", async ({ failure, message }) => {
    const connectionTest = vi.fn(() => {
      if (typeof failure !== "number") {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      return Response.json(
        { error: { code: "connection_failed", message: "Check failed" } },
        { status: failure },
      );
    });
    mockHomeMedia([], (path) => {
      if (path === "/me/api/home") {
        return Response.json({
          ...home,
          apps: [
            {
              id: "test-app",
              name: "Test app",
              can_mint_keys: true,
              credentials: [
                {
                  id: "test-key",
                  display_name: "Test key",
                  revoked_at: null,
                  expires_at: null,
                },
              ],
            },
          ],
        });
      }
      if (path === "/me/api/session") {
        return Response.json({ csrf_token: "t" });
      }
      if (path === "/me/api/connection-test") return connectionTest();
    });
    renderHome();

    await userEvent.click(
      await screen.findByRole("button", { name: "Open WebUI" }),
    );
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: () => {},
    });
    screen.getByRole("combobox", { name: "API key" }).focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.click(
      await screen.findByRole("option", { name: "Test app · Test key" }),
    );
    await userEvent.type(screen.getByLabelText(/^Paste your /), "test-secret");
    await userEvent.click(
      screen.getByRole("button", { name: "Check connection" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(connectionTest).toHaveBeenCalledOnce();
    const request = vi.mocked(fetch).mock.calls.find(
      ([input]) => String(input) === "/me/api/connection-test",
    );
    expect(request?.[1]?.method).toBe("POST");
    expect(JSON.parse(request?.[1]?.body as string)).toEqual({
      api_key: "test-secret",
      credential_id: "test-key",
    });
    expect(
      screen.getByRole("button", { name: "Check connection" }),
    ).toBeEnabled();
  });
});

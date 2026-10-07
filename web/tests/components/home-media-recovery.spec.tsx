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
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Playback blocked. Try again.",
    );
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

    expect(await screen.findByText(prompt)).toHaveClass("break-words");
    expect(
      await screen.findByText("Recent results may be incomplete."),
    ).toBeVisible();
  });
});

/* eslint-disable @next/next/no-img-element */
import { Profiler } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import LibraryPage from "./page";
import { productApi } from "@/lib/product-api";


const navigation = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
}));

// The signed-in person and their own home payload; owner tests change both.
const session = vi.hoisted(() => ({
  viewer: { role: "member", name: "Alex" } as { role: string; name: string; id?: string },
  home: { receipts: [] } as Record<string, unknown>,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => navigation.searchParams,
}));

vi.mock("next/image", () => ({
  default: (props: {
    alt: string;
    src: string;
    unoptimized?: boolean;
    [key: string]: unknown;
  }) => {
    const { alt, src, ...imageProps } = props;
    delete imageProps.unoptimized;
    return <img alt={alt} src={src} {...imageProps} />;
  },
}));

vi.mock("@/components/shell/session", () => ({
  useViewer: () => session.viewer,
}));

vi.mock("@/lib/product-api", () => ({
  productApi: {
    home: async () => session.home,
    models: async () => ({
      aliases: [],
      models: [
        { id: "first-music", title: "First music model", workflow: "audio.generate.first-music" },
        { id: "yue2-song", title: "YuE2 Song", workflow: "audio.generate.yue2-song" },
        { id: "image", title: "Image model", display_name: "Image model" },
        {
          id: "audio.generate.test-model",
          title: "Sound model",
          display_name: "Sound model",
        },
      ],
    }),
  },
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const assetWithReceipt = {
  asset_id: "asset_image_1",
  request_id: "req_image_123",
  kind: "image",
  prompt: "",
  model: "image",
  created_at: 1_760_000_000,
  download_url:
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E",
  width: 64,
  height: 64,
};

const assetWithoutReceipt = {
  ...assetWithReceipt,
  asset_id: "asset_no_receipt",
  request_id: null,
  prompt: "A saved image with no request receipt",
};

const audioAsset = {
  asset_id: "asset_audio_1",
  request_id: "req_audio_123",
  kind: "audio",
  prompt:
    "A warm synth loop with brushed percussion and a soft rising melody for a loading screen",
  model: "audio.generate.test-model",
  created_at: 1_760_000_000,
  download_url: "data:audio/wav;base64,UklGRg==",
  duration_s: 125,
};

function renderLibrary(onCommit: () => void = () => {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <Profiler id="library" onRender={onCommit}>
          <LibraryPage />
        </Profiler>
      </QueryClientProvider>,
    ),
  };
}

describe("LibraryPage batch J provenance", () => {
  beforeEach(() => {
    navigation.searchParams = new URLSearchParams();
    session.viewer = { role: "member", name: "Alex" };
    session.home = { receipts: [] };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          assets: [assetWithReceipt],
          models: ["image"],
          next_cursor: null,
        }),
      ),
    );
  });

  afterEach(() => {
    cleanup();
  });


  test("only the newest tile media plays across video and audio", async () => {
    vi.mocked(fetch).mockResolvedValue(Response.json({ assets: [
      { ...assetWithReceipt, asset_id: "video-a", kind: "video", prompt: "Video A" },
      { ...assetWithReceipt, asset_id: "video-b", kind: "video", prompt: "Video B" },
      { ...audioAsset, prompt: "Audio C" },
    ], models: [], next_cursor: null }));
    const paused: HTMLMediaElement[] = [];
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function (this: HTMLMediaElement) {
      paused.push(this);
      this.dispatchEvent(new Event("pause"));
    });
    try {
      const { container } = renderLibrary();
      await screen.findByRole("button", { name: "Play Video A" });
      const [a, b] = Array.from(container.querySelectorAll("video"));
      const c = container.querySelector("audio")!;
      fireEvent.play(a);
      paused.length = 0;
      fireEvent.play(b);
      expect(paused).toContain(a);
      expect(paused).not.toContain(b);
      expect(screen.getByRole("button", { name: "Play Video A" })).toBeVisible();
      paused.length = 0;
      fireEvent.play(c);
      expect(paused).toContain(b);
      expect(paused).not.toContain(c);
      expect(screen.getByRole("button", { name: "Play Video B" })).toBeVisible();
    } finally { pause.mockRestore(); }
  });

  test.each(["image", "audio", "video"])("opening the %s viewer pauses grid media", async (kind) => {
    const target = { ...assetWithReceipt, asset_id: "target", kind, prompt: "Viewer target" };
    vi.mocked(fetch).mockImplementation(async (input) => String(input).startsWith("/me/api/media?")
      ? Response.json({ assets: [{ ...assetWithReceipt, kind: "video", prompt: "Grid video" }, target], models: [], next_cursor: null })
      : Response.json({ download_url: "/fresh-target" }));
    const paused: HTMLMediaElement[] = [];
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function (this: HTMLMediaElement) {
      paused.push(this);
    });
    try {
      const { container } = renderLibrary();
      await screen.findByRole("button", { name: (name) => name.startsWith("Open Viewer target, ") });
      const gridVideo = container.querySelector("video")!;
      fireEvent.play(gridVideo);
      paused.length = 0;
      fireEvent.click(screen.getByRole("button", { name: (name) => name.startsWith("Open Viewer target, ") }));
      await screen.findByRole("dialog");
      expect(paused).toContain(gridVideo);
    } finally { pause.mockRestore(); }
  });

  test("audio retry renews its signed link once and keeps it for subsequent playback", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => String(input).startsWith("/me/api/media?")
      ? Response.json({ assets: [{ ...audioAsset, prompt: "Renew audio", download_url: "/expired-audio" }], models: [], next_cursor: null })
      : Response.json({ download_url: "/fresh-audio" }));
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    const load = vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    try {
      const { container } = renderLibrary();
      const button = await screen.findByRole("button", { name: "Play Renew audio" });
      const audio = container.querySelector("audio")!;
      fireEvent.click(button);
      await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
      fireEvent.error(audio);
      fireEvent.click(screen.getByRole("button", { name: "Play Renew audio" }));
      await waitFor(() => expect(audio).toHaveAttribute("src", "/fresh-audio"));
      expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url) === "/me/api/media/asset_audio_1/download?scope=me")).toHaveLength(1);
      expect(play).toHaveBeenCalledTimes(2);
      fireEvent.pause(audio);
      fireEvent.click(screen.getByRole("button", { name: "Play Renew audio" }));
      await waitFor(() => expect(play).toHaveBeenCalledTimes(3));
      expect(audio).toHaveAttribute("src", "/fresh-audio");
      expect(screen.queryByRole("dialog")).toBeNull();
    } finally { play.mockRestore(); pause.mockRestore(); load.mockRestore(); }
  });

  test("Load more focuses the first new creation and persistently announces its count", async () => {
    const first = { ...assetWithReceipt, prompt: "First creation" };
    const second = { ...assetWithReceipt, asset_id: "second", prompt: "Second creation" };
    const third = { ...assetWithReceipt, asset_id: "third", prompt: "Third creation" };
    vi.mocked(fetch).mockImplementation(async (input) => Response.json(String(input).includes("cursor=next")
      ? { assets: [second, third], models: [], next_cursor: null }
      : { assets: [first], models: [], next_cursor: "next" }));
    const { queryClient } = renderLibrary();
    const more = await screen.findByRole("button", { name: "Load more" });
    const status = screen.getByRole("status");
    more.focus();
    fireEvent.click(more);
    const next = await screen.findByRole("button", { name: (name) => name.startsWith("Open Second creation, ") });
    await waitFor(() => expect(next).toHaveFocus());
    expect(status).toHaveTextContent("2 more creations loaded");
    expect(status).toBeInTheDocument();
    const firstButton = screen.getByRole("button", { name: (name) => name.startsWith("Open First creation, ") });
    firstButton.focus();
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ["library"] }); });
    expect(firstButton).toHaveFocus();
    expect(status).toHaveTextContent("2 more creations loaded");
  });
  test("resets failed media on the first source-change commit", async () => {
    const commits: Array<string | null> = [];
    let capture = false;
    const { queryClient } = renderLibrary(() => {
      if (capture) {
        commits.push(screen.queryByRole("img", { name: /^Picture/ })?.getAttribute("src") ?? null);
      }
    });
    const image = await screen.findByRole("img", { name: /^Picture/ });
    fireEvent.error(image);
    expect(screen.queryByRole("img", { name: /^Picture/ })).toBeNull();
    capture = true;
    act(() => {
      queryClient.setQueriesData(
        { predicate: (query) => query.queryKey[0] === "library" && typeof query.queryKey[1] === "object" },
        { pages: [{ assets: [{ ...assetWithReceipt, download_url: "/new-picture.png" }], models: ["image"], next_cursor: null }], pageParams: [null] },
      );
    });
    await waitFor(() => expect(commits.length).toBeGreaterThan(0));
    expect(commits[0]).toBe("/new-picture.png");
  });

  test.each(["playing", "error"] as const)(
    "resets audio %s and measured duration on the first source-change commit",
    async (state) => {
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ assets: [{ ...audioAsset, prompt: "Reset audio" }], models: [], next_cursor: null }),
      );
      const commits: Array<{ label: string | null; text: string; error: boolean }> = [];
      let capture = false;
      const { queryClient, container } = renderLibrary(() => {
        if (capture) {
          const button = document.querySelector("[data-tile-cue='playback']");
          commits.push({
            label: button?.getAttribute("aria-label") ?? null,
            text: button?.textContent ?? "",
            error: screen.queryByText("Audio unavailable") !== null,
          });
        }
      });
      await screen.findByRole("button", { name: "Play Reset audio" });
      const audio = container.querySelector("audio")!;
      Object.defineProperty(audio, "duration", { configurable: true, value: 333 });
      fireEvent.loadedMetadata(audio);
      if (state === "playing") {
        fireEvent.play(audio);
        expect(screen.getByRole("button", { name: "Pause Reset audio" })).toBeVisible();
      } else {
        fireEvent.error(audio);
        expect(screen.getByText("Audio unavailable")).toBeVisible();
      }
      capture = true;
      act(() => {
        queryClient.setQueriesData(
          { predicate: (query) => query.queryKey[0] === "library" && typeof query.queryKey[1] === "object" },
          { pages: [{ assets: [{ ...audioAsset, prompt: "Reset audio", download_url: "/new-audio.wav", duration_s: 45 }], models: [], next_cursor: null }], pageParams: [null] },
        );
      });
      await waitFor(() => expect(commits.length).toBeGreaterThan(0));
      expect(commits[0]).toMatchObject({ label: "Play Reset audio", error: false });
      expect(commits[0].text).toContain("0:45");
      expect(commits[0].text).not.toContain("5:33");
    },
  );

  test("keeps a playing audio tile mounted when a refetch re-signs its URL", async () => {
    const signed = (sig: string) => ({
      ...audioAsset,
      prompt: "Signed audio",
      download_url: `/media/audio_1.wav?sig=${sig}`,
    });
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({ assets: [signed("one")], models: [], next_cursor: null }),
    );
    const { queryClient, container } = renderLibrary();
    await screen.findByRole("button", { name: "Play Signed audio" });
    const audio = container.querySelector("audio")!;
    fireEvent.play(audio);
    expect(screen.getByRole("button", { name: "Pause Signed audio" })).toBeVisible();
    act(() => {
      queryClient.setQueriesData(
        { predicate: (query) => query.queryKey[0] === "library" && typeof query.queryKey[1] === "object" },
        { pages: [{ assets: [signed("two")], models: [], next_cursor: null }], pageParams: [null] },
      );
    });
    await waitFor(() =>
      expect(container.querySelector("audio")).toBe(audio),
    );
    expect(audio.isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Pause Signed audio" })).toBeVisible();
  });

  test("links the request receipt and omits an absent prompt", async () => {
    renderLibrary();

    (
      await screen.findByRole("button", {
        name: /^Open Picture, /,
      })
    ).click();
    fireEvent.click(await screen.findByText("Details"));

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "View receipt req_image_123" }),
      ).toHaveAttribute("href", "/usage/?receipt=req_image_123"),
    );
    expect(screen.queryByText("Prompt not recorded.")).toBeNull();
  });

  test("opens a deep-linked asset through the exact media detail endpoint", async () => {
    navigation.searchParams = new URLSearchParams("asset=asset_deep_1");
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        Response.json({
          assets: [],
          models: [],
          next_cursor: null,
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          asset: {
            ...assetWithReceipt,
            asset_id: "asset_deep_1",
            prompt: "A deep linked image",
          },
        }),
      );

    renderLibrary();

    expect(
      await screen.findByRole("heading", { name: "A deep linked image" }),
    ).toBeVisible();
    expect(fetch).toHaveBeenCalledWith("/me/api/media/asset_deep_1", {
      credentials: "same-origin",
    });
  });

  test("encodes receipt ids", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [{ ...assetWithReceipt, request_id: "req/image 123" }],
        models: ["image"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    (
      await screen.findByRole("button", {
        name: /^Open Picture, /,
      })
    ).click();
    fireEvent.click(await screen.findByText("Details"));

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "View receipt req/image 123" }),
      ).toHaveAttribute("href", "/usage/?receipt=req%2Fimage%20123"),
    );
  });

  test("omits the receipt link when no request id is available", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [assetWithoutReceipt],
        models: ["image"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    (
      await screen.findByRole("button", {
        name: (name) => name.startsWith("Open A saved image with no request receipt, "),
      })
    ).click();
    fireEvent.click(await screen.findByText("Details"));

    await waitFor(() =>
      expect(
        screen.getAllByText("Request receipt").at(-1)?.nextSibling,
      ).toHaveTextContent("Not recorded"),
    );
    expect(
      screen.queryByRole("link", { name: /View receipt/ }),
    ).not.toBeInTheDocument();
  });

  test("prepares downloads through the media download endpoint", async () => {
    // The viewer also loads the creation's receipt, so answer by URL rather
    // than by call order.
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes("/download?")
        ? Response.json({ download_url: "https://signed.example/media.png" })
        : String(input).startsWith("/me/api/receipts/")
          ? Response.json({ request_id: "req_image_123" })
          : Response.json({
              assets: [assetWithReceipt],
              models: ["image"],
              next_cursor: null,
            }),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    renderLibrary();

    (
      await screen.findByRole("button", {
        name: /^Open Picture, /,
      })
    ).click();
    fireEvent.click(await screen.findByRole("button", { name: "Download" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/me/api/media/asset_image_1/download?scope=me",
        { credentials: "same-origin" },
      ),
    );
    await waitFor(() => expect(click).toHaveBeenCalled());
    click.mockRestore();
  });

  test("does not create its own main landmark inside the app shell", async () => {
    renderLibrary();

    await waitFor(() => {
      expect(
        screen.queryByRole("status", { name: "Loading library" }),
      ).toBeNull();
    });
    expect(document.querySelectorAll("main")).toHaveLength(0);
  });

  test("gives the audio tile a visible short title and names the model only in the viewer", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [audioAsset],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    // The audio tile is quiet: the prompt is the tile's name and lives in
    // the viewer, not as text on the face.
    const open = await screen.findByRole("button", { name: (name) => name.startsWith(`Open ${audioAsset.prompt}, `) });
    expect(screen.queryByText(/^Warm synth loop with brushed percussion/)).toBeNull();
    expect(open.closest("article")!.querySelectorAll("p.line-clamp-2")).toHaveLength(0);
    // The tile carries no model name or id; the viewer's facts do.
    expect(screen.queryByText("Sound model")).toBeNull();
    expect(screen.queryByText("audio.generate.test-model")).toBeNull();
    expect(screen.queryByText("Untitled generation")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: (name) => name.startsWith(`Open ${audioAsset.prompt}, `) }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Sound model")).toBeVisible();
  });

  test("omits the caption when the prompt is absent", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [{ ...audioAsset, prompt: "" }],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    expect(
      await screen.findByRole("button", {
        name: /^Open Audio, /,
      }),
    ).toBeVisible();
    expect(screen.queryByText("Prompt not recorded")).toBeNull();
    expect(screen.queryByText("Sound model")).toBeNull();
  });

  test("falls back when the API returns a null prompt", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [{ ...audioAsset, prompt: null }],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    expect(
      await screen.findByRole("button", {
        name: /^Open Audio, /,
      }),
    ).toBeVisible();
    expect(screen.queryByText("Prompt not recorded")).toBeNull();
  });

  test("renders an accessible mini audio player with measured duration and retryable playback errors", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [audioAsset],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockRejectedValueOnce(new Error("blocked"));
    renderLibrary();

    const playButton = await screen.findByRole("button", {
      name: `Play ${audioAsset.prompt}`,
    });
    // Play and the type/length badge share one persistent control.
    expect(playButton).toHaveTextContent("2:05");
    expect(
      screen.queryByLabelText(`Play ${audioAsset.prompt}`, {
        selector: "audio",
      }),
    ).not.toBeInTheDocument();

    fireEvent.click(playButton);

    await waitFor(() => expect(play).toHaveBeenCalled());
    expect(
      await screen.findByText("Playback didn't start. Try again."),
    ).toBeVisible();
    expect(playButton).toBeEnabled();
  });

  test("reports actual media load errors honestly", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [audioAsset],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    await screen.findByRole("button", {
      name: `Play ${audioAsset.prompt}`,
    });
    const media = document.querySelector("article audio");
    expect(media).toBeTruthy();

    fireEvent.error(media!);

    expect(await screen.findByText("Audio unavailable")).toBeVisible();
  });

  test("distinguishes an empty filtered result from an empty library", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        Response.json({
          assets: [],
          models: ["image"],
          next_cursor: null,
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          assets: [],
          models: ["image"],
          next_cursor: null,
        }),
      );
    renderLibrary();

    expect(await screen.findByText("No creations yet")).toBeVisible();
    expect(screen.queryByText("No matching creations")).toBeNull();

    fireEvent.click(
      within(screen.getByRole("group", { name: "Kind" })).getByRole("button", {
        name: "Images",
      }),
    );

    expect(await screen.findByText("No matching creations")).toBeVisible();
    expect(screen.queryByText("No creations yet")).toBeNull();
    expect(
      screen.getAllByRole("button", { name: "Clear filters" }).length,
    ).toBeGreaterThan(0);
  });

  test("keeps mini audio player state in sync with media play and pause events", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [audioAsset],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    const audio = await screen.findByRole("button", {
      name: `Play ${audioAsset.prompt}`,
    });
    const media = document.querySelector("article audio");
    expect(media).toBeTruthy();

    fireEvent.play(media!);
    expect(
      screen.getByRole("button", { name: `Pause ${audioAsset.prompt}` }),
    ).toBeVisible();

    fireEvent.pause(media!);
    expect(audio).toBeVisible();
  });
});

/* ------------------------------------------------------------------ */
/* Honesty, access and grouping                                        */
/* ------------------------------------------------------------------ */

type Route = (path: string) => Response | Promise<Response> | undefined;

/** Answer each request by URL; anything unrouted gets an empty list. */
function routeFetch(route: Route) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      return (
        (await route(path)) ??
        (path.includes("/download?") ? Response.json({ download_url: "/renewed-media" }) : undefined) ??
        Response.json({ assets: [], models: [], next_cursor: null })
      );
    }),
  );
}

function list(assets: unknown[], extra: Record<string, unknown> = {}) {
  return Response.json({ assets, models: [], next_cursor: null, ...extra });
}

function factValue(dialog: HTMLElement, label: string) {
  return Array.from(dialog.querySelectorAll("dt")).find(
    (term) => term.textContent === label,
  )?.nextElementSibling;
}

const picture = (id: string, prompt: string, extra: object = {}) => ({
  asset_id: id,
  request_id: null,
  kind: "image",
  prompt,
  model: "image",
  created_at: Math.floor(Date.now() / 1000),
  download_url: `/signed/${id}.png`,
  ...extra,
});

async function choosePerson(name: string) {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
  screen.getByRole("combobox", { name: "Person" }).focus();
  await userEvent.keyboard("{Enter}");
  await userEvent.click(await screen.findByRole("option", { name }));
}

describe("Library honesty and access", () => {
  beforeEach(() => {
    navigation.searchParams = new URLSearchParams();
    session.viewer = { role: "member", name: "Alex" };
    session.home = { receipts: [] };
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  test.each(["owner", "member"])("empty Library has a purpose and a reversible primary action for %s", async (role) => {
    session.viewer = { role, name: "Alex" };
    routeFetch((path) => path.startsWith("/me/api/media?") ? list([]) : undefined);
    renderLibrary();
    const empty = await screen.findByRole("region", { name: "No creations yet" });
    expect(empty).toHaveAccessibleDescription("Images, videos and audio you create appear here.");
    expect(within(empty).getAllByRole("link")).toHaveLength(1);
    expect(within(empty).getByRole("link", { name: "Make an image" })).toHaveAttribute("href", "/try?capability=image");

    fireEvent.click(within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: "Images" }));
    const filtered = await screen.findByRole("region", { name: "No matching creations" });
    expect(filtered).toHaveAccessibleDescription("No creations match your search or filters. Clear the filters to see more.");
    expect(within(filtered).getAllByRole("button")).toHaveLength(1);
    expect(within(filtered).queryByRole("link")).toBeNull();
    await userEvent.setup().click(within(filtered).getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("region", { name: "No creations yet" })).toBeVisible();
    if (role === "member") expect(screen.queryByRole("combobox", { name: "Person" })).toBeNull();
  });

  test("two workflows sharing an intent keep their own name in the viewer", async () => {
    session.home = { receipts: [], capabilities: [{ use_name: "audio.generate.first-music", use_names: ["audio.generate.first-music", "audio.generate.yue2-song"], recommended_model: "First music model" }] };
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("song", "My song", { kind: "audio", model: "audio.generate.yue2-song", request_id: "song-receipt" })])
      : path.includes("/receipts/") ? Response.json({ resolved_model: "yue2-song", resolved_workflow: "audio.generate.yue2-song" }) : undefined);
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open My song, ") }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(factValue(dialog, "Model")).toHaveTextContent("YuE2 Song"));
    expect(dialog).not.toHaveTextContent("First music model");
    fireEvent.click(within(dialog).getByText("Details"));
    expect(factValue(dialog, "Model name")).toHaveTextContent("yue2-song");
  });

  test("renewal failure offers a retry and hides expiry copy after recovery", async () => {
    let attempts = 0;
    routeFetch((path) => path.startsWith("/me/api/media?") ? list([picture("retry", "Retry picture")])
      : path.includes("/download?") ? ++attempts === 1 ? new Response("failed", { status: 503 }) : Response.json({ download_url: "/fresh?expires=2000000000" }) : undefined);
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Retry picture, ") }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(await within(dialog).findByRole("button", { name: "Try again" }));
    expect(await within(dialog).findByRole("img", { name: "Retry picture" })).toHaveAttribute("src", "/fresh?expires=2000000000");
    expect(dialog).not.toHaveTextContent("Playback link expires");
    expect(attempts).toBe(2);
  });

  test("opening from Library records the creation for this person's session", async () => {
    session.viewer = { role: "member", name: "Alex", id: "alex" };
    routeFetch((path) => path.startsWith("/me/api/media?") ? list([picture("opened", "Opened picture")]) : undefined);
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Opened picture, ") }));
    await screen.findByRole("dialog");
    await waitFor(() => expect(JSON.parse(sessionStorage.getItem("azizam:opened-creations:v1:alex") ?? "[]")).toContain("opened"));
    sessionStorage.clear();
  });

  test("9a offers Delete in Everyone view only for the viewer's own creations", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    session.home = {
      receipts: [],
      user: { id: "alex" },
      apps: [{ id: "app-alex" }],
    };
    const sams = picture("sam1", "Sam's lighthouse", {
      owner_name: "Sam",
      source: { id: "app:app-sam", kind: "app", label: "Sam's phone", app_id: "app-sam" },
    });
    const alexs = picture("alex1", "Alex's harbour", {
      source: { id: "app:app-alex", kind: "app", label: "Alex's laptop", app_id: "app-alex" },
    });
    const unknown = picture("unknown1", "Nobody's field");
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([sams, alexs, unknown], {
            sources: [
              { id: "app:app-sam", label: "Sam's phone", app_id: "app-sam", owner_user_id: "sam" },
            ],
          })
        : undefined,
    );
    renderLibrary();

    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Sam's lighthouse, ") }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Sam")).toBeVisible();
    expect(within(dialog).queryByText("Alex")).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /^Actions for/ })).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: (name) => name.startsWith("Open Nobody's field, ") }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("button", { name: /^Actions for/ })).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: (name) => name.startsWith("Open Alex's harbour, ") }));
    dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("button", { name: "Actions for Alex's harbour" }),
    ).toBeVisible();
  });

  test("9 asks for everyone's work explicitly in the owner's default view", async () => {
    // The API starts every role on their own work (PF-183).
    for (const [role, expected] of [["owner", "scope=everyone"], ["member", null]] as const) {
      session.viewer = { role, name: "Alex" };
      session.home = { receipts: [], user: { id: "alex" }, apps: [] };
      const paths: string[] = [];
      routeFetch((path) => {
        if (path.startsWith("/me/api/media?")) paths.push(path);
        return undefined;
      });
      const view = renderLibrary();
      await waitFor(() => expect(paths.length).toBeGreaterThan(0));
      if (expected) expect(paths[0]).toContain(expected);
      else expect(paths[0]).not.toContain("scope=");
      view.unmount();
    }
  });

  test("9a never leaves another person's results openable after the person filter changes", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    session.home = { receipts: [], user: { id: "alex" }, apps: [] };
    let finish!: () => void;
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media?")) return undefined;
      if (path.includes("scope=me"))
        return new Promise<Response>((resolve) => {
          finish = () => resolve(list([picture("mine1", "My own kite")]));
        });
      return list([picture("sam1", "Sam's lighthouse", { owner_name: "Sam" })]);
    });
    renderLibrary();
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Sam's lighthouse, ") });

    await choosePerson("Alex (you)");
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    // Sam's result belongs to the previous filter: it is not shown or openable.
    expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Sam's lighthouse, ") })).toBeNull();
    expect(screen.getByRole("status", { name: "Loading library" })).toBeVisible();

    finish();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open My own kite, ") }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Alex")).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Actions for My own kite" }),
    ).toBeVisible();
  });

  test("9b says 'no longer stored' only for a confirmed reason and names only what remains", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([
            picture("lost1", "A lost fox", {
              request_id: "req_fox",
              available: false,
              unavailable_reason: "source_unavailable",
            }),
            picture("lost2", "A lost owl", {
              available: false,
              unavailable_reason: "missing_or_expired",
            }),
            picture("lost3", "", { available: false, unavailable_reason: null }),
          ])
        : path.includes("/download?") ? Response.json({}, { status: 404 })
        : path.startsWith("/me/api/receipts/")
          ? Response.json({})
          : undefined,
    );
    renderLibrary();

    // Not every reason is confirmed, so the summary does not claim deletion.
    const summary = await screen.findByText(/^3 files are unavailable\./);
    expect(summary).toBeVisible();
    expect(summary).not.toHaveTextContent(/no longer stored/);
    fireEvent.click(screen.getByText("Show them"));
    expect(screen.getAllByText("This file is unavailable.")).toHaveLength(2);
    expect(screen.getAllByText("File no longer stored")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: (name) => name.startsWith("Open A lost fox, ") }));
    let dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("This file is unavailable. The prompt and receipt are below."),
    ).toBeVisible();
    expect(within(dialog).queryByText(/file is no longer stored/)).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: (name) => name.startsWith("Open A lost owl, ") }));
    dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("This file is no longer stored. The prompt is below."),
    ).toBeVisible();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: /^Open Picture, / }));
    dialog = await screen.findByRole("dialog");
    // With neither a prompt nor a receipt, the note promises nothing below.
    expect(dialog.querySelector("p.bg-muted")).toHaveTextContent(
      /^This file is unavailable\.$/,
    );
    expect(within(dialog).queryByText(/below/)).toBeNull();
  });

  test("9c shows the charge only from a loaded receipt", async () => {
    let answer!: (response: Response) => void;
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([
            picture("paid", "A paid picture", { request_id: "req_paid" }),
            picture("broken", "A broken receipt", { request_id: "req_broken" }),
            picture("plain", "No request recorded"),
          ])
        : path.startsWith("/me/api/receipts/req_paid")
          ? new Promise<Response>((resolve) => {
              answer = resolve;
            })
          : path.startsWith("/me/api/receipts/req_broken")
            ? Response.json({ error: { message: "nope" } }, { status: 500 })
            : undefined,
    );
    renderLibrary();

    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open A paid picture, ") }));
    let dialog = await screen.findByRole("dialog");
    expect(factValue(dialog, "Charge")).toHaveTextContent("Checking…");
    expect(within(dialog).queryByText("Cost not recorded")).toBeNull();
    answer(Response.json({ remote: true, cost: null }));
    await waitFor(() =>
      expect(factValue(dialog, "Charge")).toHaveTextContent("Cost not recorded"),
    );
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: (name) => name.startsWith("Open A broken receipt, ") }));
    dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(factValue(dialog, "Charge")).toHaveTextContent("Couldn’t load the receipt"),
    );
    expect(within(dialog).queryByText("Cost not recorded")).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: (name) => name.startsWith("Open No request recorded, ") }));
    dialog = await screen.findByRole("dialog");
    expect(factValue(dialog, "Charge")).toBeUndefined();
    expect(within(dialog).queryByText("Cost not recorded")).toBeNull();
  });

  test("10a arrow keys seek in the player instead of changing creations", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([
            { ...audioAsset, asset_id: "a1", prompt: "First clip", duration_s: 30 },
            { ...audioAsset, asset_id: "a2", prompt: "Second clip", duration_s: 30 },
            {
              ...picture("v1", "Third clip"),
              kind: "video",
              download_url: "/signed/v1.mp4",
            },
          ])
        : undefined,
    );
    renderLibrary();

    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open First clip, ") }));
    const dialog = await screen.findByRole("dialog");
    const position = await within(dialog).findByRole("slider", { name: "Position" });
    fireEvent.keyDown(position, { key: "ArrowRight" });
    fireEvent.keyDown(within(dialog).getByRole("button", { name: "Play First clip" }), {
      key: "ArrowRight",
    });
    expect(within(dialog).getByRole("heading", { name: "First clip" })).toBeVisible();

    // Outside the controls the arrows still move between creations.
    fireEvent.keyDown(within(dialog).getByRole("heading", { name: "First clip" }), {
      key: "ArrowRight",
    });
    expect(
      await within(dialog).findByRole("heading", { name: "Second clip" }),
    ).toBeVisible();
    fireEvent.keyDown(within(dialog).getByRole("heading", { name: "Second clip" }), {
      key: "ArrowRight",
    });
    expect(
      await within(dialog).findByRole("heading", { name: "Third clip" }),
    ).toBeVisible();

    // The video viewer draws its own controls; its Position seeks too.
    await waitFor(() => expect(dialog.querySelector("video")).not.toBeNull());
    const video = dialog.querySelector("video")!;
    expect(video).not.toHaveAttribute("controls");
    expect(video).toHaveAccessibleName("Third clip");
    expect(within(dialog).getByRole("button", { name: "Play Third clip" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Mute" })).toBeVisible();
    fireEvent.keyDown(within(dialog).getByRole("slider", { name: "Position" }), {
      key: "ArrowLeft",
    });
    expect(within(dialog).getByRole("heading", { name: "Third clip" })).toBeVisible();
  });

  test("10a Space on the focused video stage toggles playback", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([{ ...picture("v1", "A short film"), kind: "video", download_url: "/v1.mp4" }])
        : undefined,
    );
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open A short film, ") }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.querySelector("video")).not.toBeNull());
    const video = dialog.querySelector("video")!;
    video.focus();
    expect(video).toHaveFocus();
    fireEvent.keyDown(video, { key: " " });
    await waitFor(() => expect(play).toHaveBeenCalledOnce());
    play.mockRestore();
  });

  test("opening a cached creation renews an expired playback URL", async () => {
    let renew!: (response: Response) => void;
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("expired", "An old picture", { download_url: "/expired?expires=1" })])
      : path.includes("/download?") ? new Promise<Response>((resolve) => { renew = resolve; }) : undefined);
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open An old picture, ") }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.querySelector('img[src="/expired?expires=1"]')).toBeNull();
    await waitFor(() => expect(renew).toBeTypeOf("function"));
    renew(Response.json({ download_url: "/renewed?expires=2000000000" }));
    expect(await within(dialog).findByRole("img", { name: "An old picture" })).toHaveAttribute("src", "/renewed?expires=2000000000");
    expect(within(dialog).queryByText(/Playback link expires|Reopen to renew/)).toBeNull();
  });

  test("10b returns focus to the tile of the creation shown last", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([picture("p1", "First picture"), picture("p2", "Second picture")])
        : undefined,
    );
    renderLibrary();
    const first = await screen.findByRole("button", { name: (name) => name.startsWith("Open First picture, ") });
    first.focus();
    fireEvent.click(first);
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(within(dialog).getByRole("heading", { name: "First picture" }), {
      key: "ArrowRight",
    });
    await within(dialog).findByRole("heading", { name: "Second picture" });
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: (name) => name.startsWith("Open Second picture, ") })).toHaveFocus(),
    );
  });

  test("10d shares the file itself where the browser has a share sheet", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([{ ...picture("p1", "A birthday cake"), media_type: "image/png" }])
        : path.includes("/download?")
          ? Response.json({ download_url: "https://signed.example/p1.png" })
          : path === "https://signed.example/p1.png"
            ? new Response("png", { headers: { "content-type": "image/png" } })
            : undefined,
    );
    const share = vi.fn(async () => undefined);
    Object.assign(navigator, { share, canShare: () => true });
    try {
      renderLibrary();
      fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open A birthday cake, ") }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(await within(dialog).findByRole("button", { name: "Share" }));
      await waitFor(() => expect(share).toHaveBeenCalledOnce());
      const shared = (share.mock.calls[0] as unknown as [{ files: File[] }])[0];
      expect(shared.files[0].name).toBe("A birthday cake.png");
      // Download stays, as the secondary action.
      expect(within(dialog).getByRole("button", { name: "Download" })).toBeVisible();
    } finally {
      Reflect.deleteProperty(navigator, "share");
      Reflect.deleteProperty(navigator, "canShare");
    }
  });

  test("10c the download control is a real button, disabled while it prepares", async () => {
    let ready!: () => void;
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([picture("p1", "A picture to keep")])
        : path.includes("/download?")
          ? new Promise<Response>((resolve) => {
              ready = () => resolve(Response.json({ download_url: "https://signed.example/p1.png" }));
            })
          : undefined,
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open A picture to keep, ") }));
    const dialog = await screen.findByRole("dialog");
    const download = within(dialog).getByRole("button", { name: "Download" });
    expect(download.tagName).toBe("BUTTON");
    expect(within(dialog).queryByRole("link", { name: "Download" })).toBeNull();
    fireEvent.click(download);
    await waitFor(() => expect(ready).toBeTypeOf("function"));
    expect(fetch).toHaveBeenCalledWith(
      "/me/api/media/p1/download?scope=me",
      { credentials: "same-origin" },
    );
    const preparing = within(dialog).getByRole("button", { name: "Preparing…" });
    expect(preparing).toBeDisabled();
    fireEvent.click(preparing);
    expect(
      vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("/download?")),
    ).toHaveLength(2); // Opening renews playback; Download renews independently.
    ready();
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled();
    click.mockRestore();
  });

  test("2 dates each creation in its caption and marks the first tile of each day", async () => {
    // Captions compare the creation's local day with the day at render time.
    // Freeze only Date at local noon so a midnight between fixture and render
    // cannot turn "Today" into "Yesterday".
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 9, 12, 12, 0, 0));
    const day = 86_400;
    const nowS = Math.floor(Date.now() / 1000);
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([
            picture("now", "Made today", { created_at: nowS }),
            picture("old", "Made long ago", { created_at: nowS - 60 * day }),
          ])
        : undefined,
    );
    renderLibrary();
    const today = (
      await screen.findByRole("button", { name: (name) => name.startsWith("Open Made today, ") })
    ).closest("article")!;
    // The first tile of a day shows its day once, on the chip; the hover
    // caption does not repeat it (PF-073).
    expect(today).toHaveTextContent(/^Today\s*Made today$/);
    expect(within(today).getAllByText("Today")).toHaveLength(1);
    const old = screen
      .getByRole("button", { name: (name) => name.startsWith("Open Made long ago, ") })
      .closest("article")!;
    expect(old).toHaveTextContent(/Made long ago$/);
    // D1.6: one continuous grid (no date headers); the day rides on the
    // first tile of each day.
    expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
    expect(today).toHaveTextContent(/^Today/);
    expect(old).toHaveTextContent(/^[A-Z][a-z]{2} \d{1,2}(, \d{4})?/);
  });

  test("drops a video's poster from the grid, the count and viewer navigation", async () => {
    routeFetch((path) =>
      path.includes("/poster1/download")
        ? Response.json({ download_url: "/signed/poster1.png" })
        : path.startsWith("/me/api/media?")
          ? list([
              {
                ...picture("vid1", "A swimming fish"),
                kind: "video",
                download_url: "/signed/vid1.mp4",
                poster_asset_id: "poster1",
              },
              { ...picture("poster1", ""), prompt: null, model: null },
            ])
          : undefined,
    );
    renderLibrary();
    const tile = await screen.findByRole("button", { name: (name) => name.startsWith("Open A swimming fish, ") });
    expect(screen.queryByRole("button", { name: /^Open Picture, / })).toBeNull();
    expect(document.querySelectorAll("article")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Library1$/);
    fireEvent.click(tile);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("button", { name: "Next creation" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /Older/ })).toBeNull();
  });

  test("7 shows the viewer's running media work as calm, unclickable placeholders", async () => {
    session.home = {
      receipts: [
        { request_id: "run1", status: "running", intent: "image.generate", accepted_at_ms: Date.now() - 60_000 },
        { request_id: "run2", status: "queued", path: "/openai/v1/videos" },
        { request_id: "done", status: "succeeded", intent: "image.generate" },
      ],
    };
    routeFetch(() => undefined);
    renderLibrary();
    expect(await screen.findByText("Making your picture")).toBeVisible();
    expect(screen.getByText("Making your video")).toBeVisible();
    expect(screen.getByText(/^Started/)).toBeVisible();
    expect(screen.queryByText("No creations yet")).toBeNull();
    expect(screen.queryByRole("button", { name: /Making/ })).toBeNull();
    expect(screen.getByText("Making your picture").closest("button")).toBeNull();

    fireEvent.click(
      within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: "Video" }),
    );
    await waitFor(() => expect(screen.queryByText("Making your picture")).toBeNull());
    expect(screen.getByText("Making your video")).toBeVisible();
  });

  test("8 does not warn that retained pictures will expire", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    const nowS = Math.floor(Date.now() / 1000);
    routeFetch((path) =>
      path.includes("/poster1/download")
        ? Response.json({ download_url: "/signed/poster1.png" })
        : path.startsWith("/me/api/media?")
          ? list([
              picture("i1", "Retained picture", { created_at: nowS - 40 * 86_400 }),
              { ...audioAsset, asset_id: "a1", created_at: nowS - 86_400 },
              {
                ...picture("v1", "A video"),
                kind: "video",
                poster_asset_id: "poster1",
              },
              { ...picture("poster1", ""), prompt: null, created_at: nowS - 3600 },
              picture("gone", "A gone one", {
                available: false,
                unavailable_reason: "missing_or_expired",
              }),
            ])
          : undefined,
    );
    renderLibrary();
    expect(await screen.findByRole("button", { name: /Retained picture/ })).toBeVisible();
    expect(screen.queryByText(/will be removed within 3 days/)).toBeNull();
    // Gone files get one explanation for owners and members (red team
    // round 2), counted beside the title.
    expect(screen.getByText("Receipts remain.")).toBeVisible();
    expect(screen.getByRole("button", { name: "1 no longer stored" })).toBeVisible();
    expect(screen.queryByText(/keeps files/)).toBeNull();
  });

  test("8 explains unavailable media without querying expiry status", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([{ ...audioAsset, available: false, unavailable_reason: "missing_or_expired" }])
        : undefined,
    );
    renderLibrary();
    expect(await screen.findByText(/^1 file is no longer stored\./)).toBeVisible();
    expect(vi.mocked(fetch).mock.calls.some(([input]) => input === "/admin/api/status")).toBe(false);
  });

  test("11 uses the plain copy for search, filters and errors", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? Response.json({ error: { message: "boom" } }, { status: 503 })
        : undefined,
    );
    renderLibrary();
    expect(await screen.findByText("Couldn’t load your library")).toBeVisible();
    expect(screen.queryByText(/could not be loaded/)).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.getByPlaceholderText("Search your creations")).toBeVisible();
    // The filter button names itself at every width.
    const filters = screen.getByRole("button", { name: "Filters" });
    expect(within(filters).getByText("Filters")).not.toHaveClass("sr-only");
  });
});

describe("Library media identity and tile playback", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    navigation.searchParams = new URLSearchParams();
    session.viewer = { role: "member", name: "Alex" };
    session.home = { receipts: [] };
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  test("names every tile maker and explains the mark on hover and keyboard focus", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([
          picture("maker-image", "Maker picture", { owner_name: "Sam" }),
          { ...audioAsset, prompt: "Maker audio", owner_name: "Sam" },
          { ...picture("maker-video", "Maker video"), kind: "video", owner_name: "Alex" },
        ])
      : undefined);
    renderLibrary();
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Maker picture, ") });
    for (const [title, person] of [["Maker picture", "Sam"], ["Maker audio", "Sam"], ["Maker video", "Alex"]]) {
      const open = screen.getByRole("button", { name: (name) => name.startsWith(`Open ${title}, `) });
      const tile = open.closest("article")!;
      expect(within(tile).getByRole("img", { name: person })).toHaveAttribute("data-slot", "person-mark");
      expect(within(tile).queryByRole("tooltip")).toBeNull();
      await userEvent.hover(tile);
      expect(within(tile).getByRole("tooltip")).toHaveTextContent(`Made by ${person}`);
      await userEvent.unhover(tile);
      expect(within(tile).queryByRole("tooltip")).toBeNull();
      open.focus();
      await waitFor(() => expect(within(tile).getByRole("tooltip")).toHaveTextContent(`Made by ${person}`));
      await userEvent.keyboard("{Escape}");
      expect(within(tile).queryByRole("tooltip")).toBeNull();
      expect(screen.queryByRole("dialog")).toBeNull();
      open.blur();
    }
  });

  test("shows a one-line key for distinct visible makers and hides it when a filter leaves one", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    const assets = [
      picture("sam-image", "Sam image", { owner_name: "Sam" }),
      picture("sam-image-2", "Another Sam image", { owner_name: "Sam" }),
      { ...audioAsset, prompt: "Alex audio", owner_name: "Alex" },
    ];
    routeFetch((path) => path.startsWith("/me/api/media?") ? list(assets) : undefined);
    renderLibrary();
    const key = await screen.findByRole("group", { name: "Creation makers" });
    expect(within(key).getAllByRole("img")).toHaveLength(2);
    for (const name of ["Sam", "Alex"]) {
      expect(within(key).getByRole("img", { name })).toHaveAttribute("data-slot", "person-mark");
      expect(within(key).getByText(name)).toBeVisible();
    }
    expect(key).toHaveClass("whitespace-nowrap");
    expect(key.closest("section")).toHaveAccessibleName("Library filters");
    fireEvent.click(within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: "Images" }));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Creation makers" })).toBeNull());
    expect(screen.getByRole("button", { name: (name) => name.startsWith("Open Sam image, ") })).toBeVisible();
    fireEvent.click(within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: "All" }));
    expect(await screen.findByRole("group", { name: "Creation makers" })).toBeVisible();
    await choosePerson("Alex (you)");
    await waitFor(() => expect(screen.queryByRole("group", { name: "Creation makers" })).toBeNull());
  });

  test("counts unavailable makers in the key only while their creations are expanded", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([
          picture("stored", "Stored picture", { owner_name: "Sam" }),
          picture("gone", "Gone picture", { owner_name: "Alex", available: false }),
        ])
      : undefined);
    renderLibrary();
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Stored picture, ") });
    expect(screen.queryByRole("group", { name: "Creation makers" })).toBeNull();
    fireEvent.click(screen.getByText("Show them"));
    expect(await screen.findByRole("group", { name: "Creation makers" })).toBeVisible();
    fireEvent.click(screen.getByText("Show them"));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Creation makers" })).toBeNull());
  });

  test("keeps the audio tile quiet: length and play, no prompt text, no invented bars", async () => {
    const assets = [
      { ...audioAsset, asset_id: "cover-a", prompt: "Morning birds", duration_s: 12 },
      { ...audioAsset, asset_id: "cover-b", prompt: "Evening rain", duration_s: 73 },
    ];
    routeFetch((path) => path.startsWith("/me/api/media?") ? list(assets) : undefined);
    renderLibrary();
    const first = (await screen.findByRole("button", { name: (name) => name.startsWith("Open Morning birds, ") })).closest("article")!;
    const second = screen.getByRole("button", { name: (name) => name.startsWith("Open Evening rain, ") }).closest("article")!;
    expect(within(first).queryByText("Morning birds")).toBeNull();
    expect(within(second).queryByText("Evening rain")).toBeNull();
    expect(within(first).getByRole("button", { name: "Play Morning birds" })).toHaveTextContent("0:12");
    expect(within(second).getByRole("button", { name: "Play Evening rain" })).toHaveTextContent("1:13");
    // jsdom cannot measure audio, so a drawn waveform here would be invented.
    expect(first.querySelectorAll("svg rect")).toHaveLength(0);
    expect(second.querySelectorAll("svg rect")).toHaveLength(0);
  });

  test("keeps play and type-duration visible at rest within the two-cue budget including day", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    const assets = [
      { ...audioAsset, prompt: "Morning birds", owner_name: "Sam" },
      { ...picture("film", "Short film"), kind: "video", download_url: "/film.mp4", duration_s: 9, owner_name: "Sam", created_at: 1_750_000_000 },
      picture("still", "Still picture", { owner_name: "Sam", created_at: 1_740_000_000 }),
    ];
    routeFetch((path) => path.startsWith("/me/api/media?") ? list(assets) : undefined);
    renderLibrary();
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Morning birds, ") });
    for (const [title, duration, icon] of [["Morning birds", "2:05", "lucide-audio-lines"], ["Short film", "0:09", "lucide-video"]] as const) {
      const button = screen.getByRole("button", { name: `Play ${title}` });
      expect(button).toBeVisible();
      expect(button).not.toHaveClass("opacity-0", "max-sm:hidden");
      // Anchored to the tile corner; a stray "relative" would drop it below the face.
      expect(button).toHaveClass("absolute", "bottom-2", "left-2");
      expect(button).not.toHaveClass("relative");
      expect(button.querySelector(".lucide-play")).toBeTruthy();
      expect(button.querySelector(`.${icon}`)).toBeTruthy();
      expect(button).toHaveTextContent(duration);
      const tile = button.closest("article")!;
      expect(tile.querySelectorAll("[data-tile-cue]")).toHaveLength(2);
      expect(tile.querySelector('[data-tile-cue="day"]')).toBeTruthy();
      expect(tile.querySelector('[data-tile-cue="playback"]')).toBe(button);
      expect(within(tile).getByLabelText("Sam")).toHaveClass("opacity-0", "group-focus-within:opacity-100");
    }
    const still = screen.getByRole("button", { name: (name) => name.startsWith("Open Still picture, ") }).closest("article")!;
    expect(within(still).queryByRole("button", { name: /^Play / })).toBeNull();
    expect(still.querySelector(".lucide-play")).toBeNull();
    expect(still.querySelectorAll("[data-tile-cue]")).toHaveLength(1);
  });

  test.each(["audio", "video", "video with poster"])("keyboard plays and pauses %s without opening or navigating", async (kind) => {
    const title = "Keyboard clip";
    const asset = kind === "audio"
      ? { ...audioAsset, prompt: title }
      : { ...picture("video-keyboard", title), kind: "video", download_url: "/clip.mp4", duration_s: 9, poster_asset_id: kind === "video with poster" ? "poster-keyboard" : null };
    routeFetch((path) => path.includes("/poster-keyboard/download")
      ? Response.json({ download_url: "/poster.png" })
      : path.startsWith("/me/api/media?") ? list([asset]) : undefined);
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    renderLibrary();
    const open = await screen.findByRole("button", { name: (name) => name.startsWith(`Open ${title}, `) });
    const button = await screen.findByRole("button", { name: `Play ${title}` });
    const href = window.location.href;
    open.focus();
    await userEvent.tab();
    expect(button).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(play).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: `Pause ${title}` })).toHaveFocus();
    await userEvent.keyboard(" ");
    expect(pause).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: `Play ${title}` })).toHaveFocus();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(window.location.href).toBe(href);
    fireEvent.click(open);
    expect(await screen.findByRole("dialog")).toBeVisible();
  });

  test("poster video tile sets its source once, so a reload cannot abort the first play", async () => {
    routeFetch((path) => path.includes("/poster-once/download")
      ? Response.json({ download_url: "/poster.png" })
      : path.startsWith("/me/api/media?")
        ? list([{ ...picture("video-once", "Once film"), kind: "video", download_url: "/once.mp4", duration_s: 9, poster_asset_id: "poster-once" }])
        : undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    renderLibrary();
    const button = await screen.findByRole("button", { name: "Play Once film" });
    const video = button.closest("article")!.querySelector("video")!;
    expect(video).not.toHaveAttribute("src");
    // Setting src again, even to the same value, restarts the media load and aborts play().
    let writes = 0;
    const observer = new MutationObserver((records) => {
      writes += records.filter((record) => record.attributeName === "src").length;
    });
    observer.observe(video, { attributes: true });
    await userEvent.click(button);
    await screen.findByRole("button", { name: "Pause Once film" });
    observer.takeRecords().forEach((record) => {
      if (record.attributeName === "src") writes += 1;
    });
    observer.disconnect();
    expect(video).toHaveAttribute("src", expect.stringContaining("/once.mp4"));
    expect(writes).toBe(1);
  });

  test("video tile retries rejected playback and updates its measured length", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([{ ...picture("retry-video", "Retry film"), kind: "video", download_url: "/retry.mp4" }])
      : undefined);
    const play = vi.spyOn(HTMLMediaElement.prototype, "play")
      .mockRejectedValueOnce(new Error("blocked"))
      .mockResolvedValue(undefined);
    renderLibrary();
    const button = await screen.findByRole("button", { name: "Play Retry film" });
    const video = button.closest("article")!.querySelector("video")!;
    Object.defineProperty(video, "duration", { configurable: true, value: 64 });
    fireEvent.loadedMetadata(video);
    expect(button).toHaveTextContent("1:04");
    await userEvent.click(button);
    expect(await screen.findByText("Playback didn't start. Try again.")).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(button);
    await waitFor(() => expect(play).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Playback didn't start. Try again.")).toBeNull();
    fireEvent.ended(video);
    expect(screen.getByRole("button", { name: "Play Retry film" })).toBeVisible();
  });
});


describe("Library response and media boundaries", () => {
  const originalShare = Object.getOwnPropertyDescriptor(navigator, "share");
  const originalCanShare = Object.getOwnPropertyDescriptor(navigator, "canShare");
  beforeEach(() => {
    navigation.searchParams = new URLSearchParams();
    session.viewer = { role: "member", name: "Alex" };
    session.home = { receipts: [] };
    window.history.replaceState(null, "", "/");
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    if (originalShare) Object.defineProperty(navigator, "share", originalShare);
    else Reflect.deleteProperty(navigator, "share");
    if (originalCanShare) Object.defineProperty(navigator, "canShare", originalCanShare);
    else Reflect.deleteProperty(navigator, "canShare");
    vi.unstubAllGlobals();
  });

  test.each(["invalid JSON", "null", "wrong shape"])("rejects %s library data as a typed unreadable response", async (kind) => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? kind === "invalid JSON" ? new Response("{")
        : Response.json(kind === "null" ? null : { assets: [{}] })
      : undefined);
    const { queryClient } = renderLibrary();
    expect(await screen.findByText("We couldn't read the response. Try again.")).toBeVisible();
    const query = queryClient.getQueryCache().findAll().find((query) => query.queryKey[0] === "library" && typeof query.queryKey[1] === "object");
    expect(query?.state.error).toMatchObject({ stage: "library", reason: "unreadable" });
    queryClient.clear();
  });

  test("a deep link that cannot open explains itself, and Try again opens it", async () => {
    navigation.searchParams = new URLSearchParams("asset=flaky");
    let attempts = 0;
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media/flaky")) return undefined;
      attempts += 1;
      return attempts === 1
        ? Response.json({}, { status: 404 })
        : Response.json({ asset: picture("flaky", "Second try works") });
    });
    const { queryClient } = renderLibrary();
    expect(await screen.findByText("That creation is unavailable")).toBeVisible();
    expect(screen.getByText("We couldn’t open this creation. Try again or return to your library.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("dialog", { name: "Second try works" })).toBeVisible();
    expect(screen.queryByText("That creation is unavailable")).toBeNull();
    queryClient.clear();
  });

  test("a library load failure explains itself and retries the list", async () => {
    let attempts = 0;
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? ++attempts === 1
        ? Response.json({ error: { message: "boom" } }, { status: 503 })
        : list([picture("recovered-list", "Recovered list")])
      : undefined);
    renderLibrary();
    expect(await screen.findByText("Couldn’t load your library")).toBeVisible();
    expect(screen.getByText("We couldn't load your library. Try again.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Recovered list, ") })).not.toBeNull(), { onTimeout: (error) => error });
    expect(screen.queryByText("Couldn’t load your library")).toBeNull();
    expect(attempts).toBe(2);
  });

  test("appends a long second page using its opaque cursor and stops at the end", async () => {
    const requests: URLSearchParams[] = [];
    const first = Array.from({ length: 60 }, (_, i) => picture(`first-${i}`, `First ${i}`));
    const second = Array.from({ length: 60 }, (_, i) => picture(`second-${i}`, `Second ${i}`));
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media?")) return undefined;
      const params = new URL(path, "https://test.invalid").searchParams;
      requests.push(params);
      return params.has("cursor") ? list(second) : list(first, { next_cursor: "next/+ &=" });
    });
    renderLibrary();
    await screen.findByRole("button", { name: (name) => name.startsWith("Open First 59, ") });
    expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Second 0, ") })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Second 59, ") });
    const grid = screen.getByRole("group", { name: "Creations" });
    expect(within(grid).getAllByRole("button", { name: /^Open / })).toHaveLength(120);
    expect(within(grid).getByRole("button", { name: (name) => name.startsWith("Open First 0, ") })).toBeVisible();
    expect(requests.map((params) => params.get("cursor"))).toEqual([null, "next/+ &="]);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  test("changing kind after paging starts fresh and replaces the old pages", async () => {
    const requests: URLSearchParams[] = [];
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media?")) return undefined;
      const params = new URL(path, "https://test.invalid").searchParams;
      requests.push(params);
      if (params.get("kind") === "image") return list([picture("filtered", "Filtered picture")]);
      return params.has("cursor")
        ? list([picture("old-second", "Old second page")])
        : list([picture("old-first", "Old first page")], { next_cursor: "old-cursor" });
    });
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Old second page, ") });
    fireEvent.click(within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: "Images" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Filtered picture, ") })).not.toBeNull(), { onTimeout: (error) => error });
    expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Old first page, ") })).toBeNull();
    expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Old second page, ") })).toBeNull();
    expect(requests.map((params) => [params.get("kind"), params.get("cursor")])).toEqual([
      [null, null], [null, "old-cursor"], ["image", null],
    ]);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  test("a creation dialog is named by its prompt and shows a text workflow in Details", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([picture("lamp", "A teal desk lamp on a walnut table", { workflow: "workflow.image.lamp", width: 1024, height: 768, duration_s: 125, generation_ms: 1500 })])
        : undefined,
    );
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open A teal desk lamp on a walnut table, ") }));
    const dialog = await screen.findByRole("dialog", { name: "A teal desk lamp on a walnut table" });
    fireEvent.click(within(dialog).getByText("Details"));
    expect(within(dialog).getByText("workflow.image.lamp")).toBeVisible();
    expect(factValue(dialog, "Dimensions")).toHaveTextContent("1024 × 768 px");
    expect(factValue(dialog, "Duration")).toHaveTextContent("125 seconds");
    expect(factValue(dialog, "Generation time")).toHaveTextContent("1.5 seconds");
  });

  test("deep link opens, becomes unavailable, and can return to the library", async () => {
    navigation.searchParams = new URLSearchParams("asset=missing");
    let finish!: (response: Response) => void;
    routeFetch((path) => path.startsWith("/me/api/media/missing")
      ? new Promise<Response>((resolve) => { finish = resolve; })
      : undefined);
    const { queryClient } = renderLibrary();
    expect(await screen.findByText("Opening that creation…")).toBeVisible();
    await act(async () => finish(Response.json({}, { status: 404 })));
    expect(await screen.findByText("That creation is unavailable")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Back to library" }));
    await waitFor(() => expect(screen.queryByText("That creation is unavailable")).toBeNull());
    queryClient.clear();
  });

  test.each([0, "0", null, ""])("keeps recorded cost %j distinct from missing", async (usd) => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("zero", "Zero charge", { request_id: "zero-receipt" })])
      : path.includes("/receipts/") ? Response.json({ cost: { usd }, remote: true }) : undefined);
    const { queryClient } = renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Zero charge, ") }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(factValue(dialog, "Charge")).toHaveTextContent(usd === 0 || usd === "0" ? "USD 0.00" : "Cost not recorded"));
    queryClient.clear();
  });

  test("successful renewal clears the selected unavailable warning", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("recovered", "Recovered image", { available: false, unavailable_reason: "missing_or_expired" })])
      : undefined);
    const { queryClient } = renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Recovered image, ") }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("img", { name: "Recovered image" })).toBeVisible();
    expect(within(dialog).queryByText(/file is no longer stored/)).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled();
    queryClient.clear();
  });

  test("shares a generic binary response as the gateway's declared media type", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([{ ...picture("p2", "Octet cake"), media_type: "image/png" }])
        : path.includes("/download?")
          ? Response.json({ download_url: "https://signed.example/p2" })
          : path === "https://signed.example/p2"
            ? new Response("png", { headers: { "content-type": "application/octet-stream" } })
            : undefined,
    );
    const share = vi.fn(async () => undefined);
    Object.assign(navigator, { share, canShare: () => true });
    try {
      renderLibrary();
      fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Octet cake, ") }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(await within(dialog).findByRole("button", { name: "Share" }));
      await waitFor(() => expect(share).toHaveBeenCalledOnce());
      const shared = (share.mock.calls[0] as unknown as [{ files: File[] }])[0];
      expect(shared.files[0].type).toBe("image/png");
      expect(shared.files[0].name).toBe("Octet cake.png");
    } finally {
      Reflect.deleteProperty(navigator, "share");
      Reflect.deleteProperty(navigator, "canShare");
    }
  });

  test.each([
    [500, "image/png"],
    [200, "text/html"],
    [200, "application/json"],
    [200, "audio/mpeg"],
  ])("refuses share response %i %s and locks equivalent actions", async (status, type) => {
    const share = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "share", { configurable: true, value: share });
    Object.defineProperty(navigator, "canShare", { configurable: true, value: vi.fn(() => true) });
    let finish!: (response: Response) => void;
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("share", "Share image", { workflow_download_url: "/workflow" })])
      : path === "/renewed-media" ? new Promise<Response>((resolve) => { finish = resolve; }) : undefined);
    const { queryClient } = renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Share image, ") }));
    const dialog = await screen.findByRole("dialog");
    const button = await within(dialog).findByRole("button", { name: "Share" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    expect(within(dialog).getAllByRole("button", { name: "Preparing…" })).toHaveLength(2);
    for (const action of within(dialog).getAllByRole("button", { name: "Preparing…" })) expect(action).toBeDisabled();
    expect(vi.mocked(fetch).mock.calls.filter(([path]) => path === "/renewed-media")).toHaveLength(1);
    await act(async () => finish(new Response("not a file", { status, headers: { "content-type": type } })));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Share" })).toBeEnabled());
    expect(share).not.toHaveBeenCalled();
    queryClient.clear();
  });


  test("receipt retry replaces its failure with recorded zero", async () => {
    let attempts = 0;
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("receipt-retry", "Receipt retry", { request_id: "retry" })])
      : path.includes("/receipts/") ? ++attempts === 1
        ? Response.json({ error: { message: "internal exception" } }, { status: 500 })
        : Response.json({ cost: { usd: 0 } }) : undefined);
    const { queryClient } = renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Receipt retry, ") }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(await within(dialog).findByRole("button", { name: "Retry receipt" }));
    await waitFor(() => expect(factValue(dialog, "Charge")).toHaveTextContent("USD 0.00"));
    expect(within(dialog).queryByText("internal exception")).toBeNull();
    queryClient.clear();
  });

  test.each(["models", "home"] as const)("shows a quiet wait, then failure and retry for %s", async (method) => {
    const label = method === "models" ? "model names" : "running work";
    let reject!: (error: Error) => void;
    vi.spyOn(productApi, method).mockImplementationOnce(() => new Promise<never>((_resolve, fail) => { reject = fail; }));
    routeFetch(() => undefined);
    const { queryClient } = renderLibrary();
    // A supporting query's wait stays quiet (P1); only its failure shows.
    await screen.findByRole("heading", { name: "Library" });
    expect(screen.queryByText(new RegExp(`couldn’t load ${label}`))).toBeNull();
    await act(async () => reject(new Error("network")));
    fireEvent.click(await screen.findByRole("button", { name: `Retry ${label}` }));
    await waitFor(() => expect(screen.queryByRole("button", { name: `Retry ${label}` })).toBeNull());
    queryClient.clear();
  });

  test("audio buffering ends when media can play", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?") ? list([{ ...audioAsset, prompt: "Buffer test" }]) : undefined);
    const { queryClient, container } = renderLibrary();
    await screen.findByRole("button", { name: "Play Buffer test" });
    const audio = container.querySelector("audio")!;
    fireEvent.waiting(audio);
    expect(screen.getByText("Loading audio…")).toBeVisible();
    fireEvent.canPlay(audio);
    expect(screen.queryByText("Loading audio…")).toBeNull();
    queryClient.clear();
  });
});

describe("Library accessibility", () => {
  const originalShare = Object.getOwnPropertyDescriptor(navigator, "share");
  beforeEach(() => {
    navigation.searchParams = new URLSearchParams();
    session.viewer = { role: "member", name: "Alex" };
    session.home = { receipts: [] };
    window.history.replaceState(null, "", "/library/");
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    if (originalShare) Object.defineProperty(navigator, "share", originalShare);
    else Reflect.deleteProperty(navigator, "share");
  });

  test.each(["image", "audio", "video"])("names %s tiles with their title, kind and creation date", async (kind) => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([{ ...assetWithReceipt, kind, prompt: "  Named creation  " }]) : undefined);
    renderLibrary();
    const date = new Date(assetWithReceipt.created_at * 1000).toLocaleDateString("en-US", {
      year: "numeric", month: "long", day: "numeric",
    });
    const label = `Open Named creation, ${kind[0].toUpperCase() + kind.slice(1)}, ${date}`;
    expect(await screen.findByRole("button", { name: label })).toHaveAccessibleName(label);
  });

  test.each([null, "invalid-date"])("names tiles honestly when their creation date is %s", async (created_at) => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([{ ...assetWithReceipt, prompt: "Undated creation", created_at }]) : undefined);
    renderLibrary();
    expect(await screen.findByRole("button", {
      name: "Open Undated creation, Image, Date unavailable",
    })).toBeEnabled();
  });

  test("names images and open buttons from the permitted prompt, never the file path", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([
          picture("opaque-file-name.png", "  A red kite over the sea  "),
          picture("hidden-prompt-file.png", "", { prompt: null }),
        ])
      : undefined);
    renderLibrary();
    expect(await screen.findByRole("img", { name: "A red kite over the sea" })).toBeVisible();
    expect(screen.getByRole("button", { name: (name) => name.startsWith("Open A red kite over the sea, ") })).toBeEnabled();
    expect(screen.getByRole("img", { name: /^Picture, / })).toBeVisible();
    expect(screen.getByRole("button", { name: /^Open Picture, / })).toBeEnabled();
    expect(screen.queryByRole("img", { name: /opaque-file-name|hidden-prompt/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /opaque-file-name|hidden-prompt/ })).toBeNull();
  });

  test("moves through responsive tile rows with arrows and Home/End, without opening", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list(Array.from({ length: 7 }, (_, i) => picture(`p${i}`, `Picture ${i}`)))
      : undefined);
    renderLibrary();
    const grid = await screen.findByRole("group", { name: "Creations" });
    const tiles = within(grid).getAllByRole("button", { name: /^Open / });
    tiles.forEach((tile, i) => {
      const left = (i % 3) * 110;
      const top = Math.floor(i / 3) * 110;
      vi.spyOn(tile, "getBoundingClientRect").mockReturnValue({
        left, top, right: left + 100, bottom: top + 100, width: 100, height: 100,
        x: left, y: top, toJSON: () => ({}),
      });
    });
    tiles[0].focus();
    await userEvent.keyboard("{ArrowRight}{ArrowDown}");
    expect(tiles[4]).toHaveFocus();
    await userEvent.keyboard("{ArrowUp}{ArrowLeft}");
    expect(tiles[0]).toHaveFocus();
    await userEvent.keyboard("{End}");
    expect(tiles[6]).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}{ArrowRight}");
    expect(tiles[6]).toHaveFocus();
    await userEvent.keyboard("{Home}");
    expect(tiles[0]).toHaveFocus();
    await userEvent.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expect(tiles[0]).toHaveFocus();
    grid.style.direction = "rtl";
    await userEvent.keyboard("{ArrowLeft}");
    expect(tiles[1]).toHaveFocus();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(tiles[0]).toHaveClass("focus-visible:outline-2", "focus-visible:outline-offset-2", "focus-visible:outline-ring", "min-h-6", "min-w-6");
  });

  test("keeps playback controls in the Tab order and does not steal their arrow keys", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([{ ...audioAsset, prompt: "Keyboard audio" }, picture("next", "Next picture")])
      : undefined);
    renderLibrary();
    const open = await screen.findByRole("button", { name: (name) => name.startsWith("Open Keyboard audio, ") });
    const play = screen.getByRole("button", { name: "Play Keyboard audio" });
    open.focus();
    await userEvent.tab();
    expect(play).toHaveFocus();
    const event = new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true });
    expect(play.dispatchEvent(event)).toBe(true);
    expect(play).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: (name) => name.startsWith("Open Next picture, ") })).toHaveFocus();
  });

  test("also navigates the expanded unavailable group", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("gone1", "Gone one", { available: false }), picture("gone2", "Gone two", { available: false })])
      : undefined);
    renderLibrary();
    fireEvent.click(await screen.findByText("Show them"));
    const grid = screen.getByRole("group", { name: "Unavailable creations" });
    const tiles = within(grid).getAllByRole("button", { name: /^Open / });
    tiles[0].focus();
    await userEvent.keyboard("{End}");
    expect(tiles[1]).toHaveFocus();
    await userEvent.keyboard("{Home}");
    expect(tiles[0]).toHaveFocus();
    expect(screen.getByRole("button", { name: "Refresh library" })).toHaveClass("min-h-6", "min-w-6");
    expect(screen.getByRole("button", { name: "2 unavailable" })).toHaveClass("min-h-6", "min-w-6");
  });

  test("focuses the named dialog, traps Tab in both directions and restores focus on repeated opens", async () => {
    Object.defineProperty(navigator, "share", { configurable: true, value: vi.fn() });
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("focus", "Focus picture")]) : undefined);
    renderLibrary();
    const open = await screen.findByRole("button", { name: (name) => name.startsWith("Open Focus picture, ") });
    for (let i = 0; i < 2; i += 1) {
      await userEvent.click(open);
      const dialog = await screen.findByRole("dialog", { name: "Focus picture" });
      expect(dialog).toHaveAttribute("aria-modal", "true");
      expect(within(dialog).getByRole("heading", { name: "Focus picture" })).toHaveFocus();
      expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled();
      expect(within(dialog).getByRole("button", { name: "Share" })).toBeEnabled();
      const first = within(dialog).getAllByRole("button", { name: "Close" })[0];
      const last = within(dialog).getByText("Details");
      last.focus();
      await userEvent.tab();
      expect(first).toHaveFocus();
      await userEvent.tab({ shift: true });
      expect(last).toHaveFocus();
      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(open).toHaveFocus());
      expect(screen.queryByRole("dialog")).toBeNull();
    }
  });

  test("a portalled action menu keeps arrow navigation instead of changing the selected creation", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("menu1", "Menu one"), picture("menu2", "Menu two")]) : undefined);
    renderLibrary();
    await userEvent.click(await screen.findByRole("button", { name: (name) => name.startsWith("Open Menu one, ") }));
    const dialog = await screen.findByRole("dialog", { name: "Menu one" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Actions for Menu one" }));
    const remove = await screen.findByRole("menuitem", { name: "Delete" });
    remove.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(within(dialog).getByRole("heading", { name: "Menu one", hidden: true })).toBeInTheDocument();
    expect(within(dialog).queryByRole("heading", { name: "Menu two", hidden: true })).toBeNull();
  });

  test("a deep-linked collapsed unavailable tile returns focus to a visible tile", async () => {
    navigation.searchParams = new URLSearchParams("asset=gone-focus");
    routeFetch((path) => path.startsWith("/me/api/media?")
      ? list([picture("visible", "Visible picture"), picture("gone-focus", "Hidden picture", { available: false })])
      : path.startsWith("/me/api/media/gone-focus") && !path.includes("/download?")
        ? Response.json({ asset: picture("gone-focus", "Hidden picture", { available: false }) })
        : undefined);
    renderLibrary();
    const dialog = await screen.findByRole("dialog", { name: "Hidden picture" });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: (name) => name.startsWith("Open Visible picture, ") })).toHaveFocus());
    expect(dialog).not.toBeInTheDocument();
  });

  test("provides reduced-motion overrides on the viewer and hover captions", async () => {
    routeFetch((path) => path.startsWith("/me/api/media?") ? list([picture("motion", "Still picture")]) : undefined);
    renderLibrary();
    const open = await screen.findByRole("button", { name: (name) => name.startsWith("Open Still picture, ") });
    expect(open.closest("article")!.querySelector(".transition-opacity")).toHaveClass("motion-reduce:transition-none");
    fireEvent.click(open);
    const dialog = await screen.findByRole("dialog", { name: "Still picture" });
    expect(dialog).toHaveClass("motion-reduce:animate-none");
    expect(document.querySelector(".bg-overlay")).toHaveClass("motion-reduce:animate-none");
  });
});

describe("Library paging regressions", () => {
  beforeEach(() => {
    navigation.searchParams = new URLSearchParams();
    session.viewer = { role: "member", name: "Alex" };
    session.home = { receipts: [] };
    window.history.replaceState(null, "", "/library/");
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  test("paging retention keeps earlier tiles while pending and after append", async () => {
    let finish!: (response: Response) => void;
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media?")) return undefined;
      const cursor = new URL(path, "https://test.invalid").searchParams.get("cursor");
      return cursor
        ? new Promise<Response>((resolve) => { finish = resolve; })
        : list([picture("kept", "Kept first page")], { next_cursor: "page/+ two" });
    });
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    expect(screen.getByRole("button", { name: (name) => name.startsWith("Open Kept first page, ") })).toBeVisible();
    expect(screen.getByRole("button", { name: "Loading more…" })).toBeDisabled();
    await act(async () => finish(list([picture("added", "Added second page")])));
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Added second page, ") });
    expect(screen.getByRole("button", { name: (name) => name.startsWith("Open Kept first page, ") })).toBeVisible();
    expect(within(screen.getByRole("group", { name: "Creations" })).getAllByRole("button", { name: /^Open / })).toHaveLength(2);
  });

  test("paging failure retains tiles and exposes Retry loading more in an alert", async () => {
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media?")) return undefined;
      return new URL(path, "https://test.invalid").searchParams.has("cursor")
        ? Response.json({}, { status: 503 })
        : list([picture("kept-error", "Kept after error")], { next_cursor: "failed-page" });
    });
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("We couldn’t load more library items. Try again.");
    expect(within(alert).getByRole("button", { name: "Retry loading more" })).toBeEnabled();
    expect(screen.getByRole("button", { name: (name) => name.startsWith("Open Kept after error, ") })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  test("paging retry requests the failed cursor and appends each tile exactly once", async () => {
    const cursors: (string | null)[] = [];
    let nextAttempts = 0;
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media?")) return undefined;
      const cursor = new URL(path, "https://test.invalid").searchParams.get("cursor");
      cursors.push(cursor);
      if (!cursor) return list([picture("retry-first", "Retry first")], { next_cursor: "retry/+ &=cursor" });
      return ++nextAttempts === 1
        ? Response.json({}, { status: 503 })
        : list([picture("retry-second", "Retry second")]);
    });
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry loading more" }));
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Retry second, ") });
    const grid = screen.getByRole("group", { name: "Creations" });
    expect(within(grid).getAllByRole("button", { name: (name) => name.startsWith("Open Retry first, ") })).toHaveLength(1);
    expect(within(grid).getAllByRole("button", { name: (name) => name.startsWith("Open Retry second, ") })).toHaveLength(1);
    expect(within(grid).getAllByRole("button", { name: /^Open / })).toHaveLength(2);
    expect(cursors).toEqual([null, "retry/+ &=cursor", "retry/+ &=cursor"]);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  test("paging filter reset starts at page one then uses only the new cursor", async () => {
    const requests: (string | null)[][] = [];
    routeFetch((path) => {
      if (!path.startsWith("/me/api/media?")) return undefined;
      const params = new URL(path, "https://test.invalid").searchParams;
      const kind = params.get("kind");
      const cursor = params.get("cursor");
      requests.push([kind, cursor]);
      if (kind === "image") return cursor
        ? list([picture("new-two", "New two")])
        : list([picture("new-one", "New one")], { next_cursor: "new-cursor" });
      return cursor
        ? list([picture("old-two", "Old two")])
        : list([picture("old-one", "Old one")], { next_cursor: "old-cursor" });
    });
    renderLibrary();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByRole("button", { name: (name) => name.startsWith("Open Old two, ") });
    fireEvent.click(within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: "Images" }));
    await screen.findByRole("button", { name: (name) => name.startsWith("Open New one, ") });
    expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Old one, ") })).toBeNull();
    expect(screen.queryByRole("button", { name: (name) => name.startsWith("Open Old two, ") })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByRole("button", { name: (name) => name.startsWith("Open New two, ") });
    expect(screen.getByRole("button", { name: (name) => name.startsWith("Open New one, ") })).toBeVisible();
    expect(requests).toEqual([[null, null], [null, "old-cursor"], ["image", null], ["image", "new-cursor"]]);
    expect(within(screen.getByRole("group", { name: "Creations" })).getAllByRole("button", { name: /^Open / })).toHaveLength(2);
  });
});

/* eslint-disable @next/next/no-img-element */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
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

const navigation = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
}));

// The signed-in person and their own home payload; owner tests change both.
const session = vi.hoisted(() => ({
  viewer: { role: "member", name: "Alex" } as { role: string; name: string },
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

function renderLibrary() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <LibraryPage />
    </QueryClientProvider>,
  );
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
      ).toHaveAttribute("href", "/r/req_image_123"),
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
      ).toHaveAttribute("href", "/r/req%2Fimage%20123"),
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
        name: "Open A saved image with no request receipt",
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

  test("gives the audio tile the same short hover title as a picture and names the model only in the viewer", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [audioAsset],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    // D1.5: no words on the audio face; the hover title is the short
    // whole-word title every tile has.
    expect(
      await screen.findByText(/^Warm synth loop with brushed percussion .*…$/),
    ).toHaveClass("line-clamp-2");
    // The tile carries no model name or id; the viewer's facts do.
    expect(screen.queryByText("Sound model")).toBeNull();
    expect(screen.queryByText("audio.generate.test-model")).toBeNull();
    expect(screen.queryByText("Untitled generation")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: `Open ${audioAsset.prompt}` }),
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
    // D1.6: the length is in the type badge bottom left; the play disc is
    // the button.
    expect(
      playButton.closest("article")!.querySelector("span.pointer-events-none.flex.h-6"),
    ).toHaveTextContent("2:05");
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

    fireEvent.click(await screen.findByRole("button", { name: "Open Sam's lighthouse" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Sam")).toBeVisible();
    expect(within(dialog).queryByText("Alex")).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /^Actions for/ })).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Open Nobody's field" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("button", { name: /^Actions for/ })).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Open Alex's harbour" }));
    dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("button", { name: "Actions for Alex's harbour" }),
    ).toBeVisible();
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
    await screen.findByRole("button", { name: "Open Sam's lighthouse" });

    await choosePerson("Alex (you)");
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    // Sam's result belongs to the previous filter: it is not shown or openable.
    expect(screen.queryByRole("button", { name: "Open Sam's lighthouse" })).toBeNull();
    expect(screen.getByRole("status", { name: "Loading library" })).toBeVisible();

    finish();
    fireEvent.click(await screen.findByRole("button", { name: "Open My own kite" }));
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

    fireEvent.click(screen.getByRole("button", { name: "Open A lost fox" }));
    let dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("This file is unavailable. The prompt and receipt are below."),
    ).toBeVisible();
    expect(within(dialog).queryByText(/no longer stores/)).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Open A lost owl" }));
    dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("The gateway no longer stores this file. The prompt is below."),
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

    fireEvent.click(await screen.findByRole("button", { name: "Open A paid picture" }));
    let dialog = await screen.findByRole("dialog");
    expect(factValue(dialog, "Charge")).toHaveTextContent("Checking…");
    expect(within(dialog).queryByText("Cost not recorded")).toBeNull();
    answer(Response.json({ remote: true, cost: null }));
    await waitFor(() =>
      expect(factValue(dialog, "Charge")).toHaveTextContent("Cost not recorded"),
    );
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Open A broken receipt" }));
    dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(factValue(dialog, "Charge")).toHaveTextContent("Couldn’t load the receipt"),
    );
    expect(within(dialog).queryByText("Cost not recorded")).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Open No request recorded" }));
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

    fireEvent.click(await screen.findByRole("button", { name: "Open First clip" }));
    const dialog = await screen.findByRole("dialog");
    const position = within(dialog).getByRole("slider", { name: "Position" });
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
    fireEvent.click(await screen.findByRole("button", { name: "Open A short film" }));
    const dialog = await screen.findByRole("dialog");
    const video = dialog.querySelector("video")!;
    video.focus();
    expect(video).toHaveFocus();
    fireEvent.keyDown(video, { key: " " });
    await waitFor(() => expect(play).toHaveBeenCalledOnce());
    play.mockRestore();
  });

  test("10b returns focus to the tile of the creation shown last", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([picture("p1", "First picture"), picture("p2", "Second picture")])
        : undefined,
    );
    renderLibrary();
    const first = await screen.findByRole("button", { name: "Open First picture" });
    first.focus();
    fireEvent.click(first);
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(within(dialog).getByRole("heading", { name: "First picture" }), {
      key: "ArrowRight",
    });
    await within(dialog).findByRole("heading", { name: "Second picture" });
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Open Second picture" })).toHaveFocus(),
    );
  });

  test("10d shares the file itself where the browser has a share sheet", async () => {
    routeFetch((path) =>
      path.startsWith("/me/api/media?")
        ? list([{ ...picture("p1", "A birthday cake"), media_type: "image/png" }])
        : path.includes("/download?")
          ? Response.json({ download_url: "https://signed.example/p1.png" })
          : path === "https://signed.example/p1.png"
            ? new Response(new Blob(["png"], { type: "image/png" }))
            : undefined,
    );
    const share = vi.fn(async () => undefined);
    Object.assign(navigator, { share, canShare: () => true });
    try {
      renderLibrary();
      fireEvent.click(await screen.findByRole("button", { name: "Open A birthday cake" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "Open A picture to keep" }));
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
    ).toHaveLength(1);
    ready();
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled();
    click.mockRestore();
  });

  test("2 dates each creation in its caption and marks the first tile of each day", async () => {
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
      await screen.findByRole("button", { name: "Open Made today" })
    ).closest("article")!;
    expect(today).toHaveTextContent(/Made today\s*Today$/);
    const old = screen
      .getByRole("button", { name: "Open Made long ago" })
      .closest("article")!;
    expect(old).toHaveTextContent(/Made long ago\s*[A-Z][a-z]{2} \d{1,2}(, \d{4})?$/);
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
    const tile = await screen.findByRole("button", { name: "Open A swimming fish" });
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

  test("8 warns the owner before pictures expire and names pictures, not files", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    const hour = 3600;
    const nowS = Math.floor(Date.now() / 1000);
    routeFetch((path) =>
      path === "/admin/api/status"
        ? Response.json({ status: { budgets: { images: { cache_ttl: 86_400 } } } })
        : path.includes("/poster1/download")
          ? Response.json({ download_url: "/signed/poster1.png" })
          : path.startsWith("/me/api/media?")
            ? list([
                picture("i1", "Fresh one", { created_at: nowS - hour }),
                picture("i2", "Fresh two", { created_at: nowS - 2 * hour }),
                picture("i3", "Older than the cache", { created_at: nowS - 40 * hour }),
                { ...audioAsset, asset_id: "a1", created_at: nowS - hour },
                {
                  ...picture("v1", "A video"),
                  kind: "video",
                  poster_asset_id: "poster1",
                },
                { ...picture("poster1", ""), prompt: null, created_at: nowS - hour },
                picture("gone", "A gone one", {
                  available: false,
                  unavailable_reason: "missing_or_expired",
                }),
              ])
            : undefined,
    );
    renderLibrary();
    expect(
      await screen.findByText(
        "2 pictures will be removed within 3 days. Download the ones to keep.",
      ),
    ).toBeVisible();
    // Gone files get one explanation for owners and members (red team
    // round 2), counted beside the title.
    expect(screen.getByText("Their prompts and receipts remain.")).toBeVisible();
    expect(screen.getByRole("button", { name: "1 no longer stored" })).toBeVisible();
    expect(screen.queryByText(/keeps files/)).toBeNull();
  });

  test("8 does not claim picture retention when only audio is unavailable", async () => {
    session.viewer = { role: "owner", name: "Alex" };
    routeFetch((path) =>
      path === "/admin/api/status"
        ? Response.json({ status: { budgets: { images: { cache_ttl: 86_400 } } } })
        : path.startsWith("/me/api/media?")
          ? list([{ ...audioAsset, available: false, unavailable_reason: "missing_or_expired" }])
          : undefined,
    );
    renderLibrary();
    expect(await screen.findByText(/^1 file is no longer stored\./)).toBeVisible();
    await waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.some(([input]) => input === "/admin/api/status")).toBe(true),
    );
    expect(screen.queryByText(/keeps pictures/)).toBeNull();
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

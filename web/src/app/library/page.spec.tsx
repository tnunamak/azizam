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
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import LibraryPage from "./page";

const navigation = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
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
  useViewer: () => ({ role: "member" }),
}));

vi.mock("@/lib/product-api", () => ({
  productApi: {
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
        name: "Open Image",
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
        name: "Open Image",
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
        name: "Open Image",
      })
    ).click();
    fireEvent.click(await screen.findByRole("link", { name: "Download" }));

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

  test("uses the prompt as the card title and keeps the model as secondary text", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [audioAsset],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    expect(await screen.findByText(audioAsset.prompt)).toBeVisible();
    expect(screen.getByText("Sound model")).toBeVisible();
    expect(screen.queryByText("Untitled generation")).not.toBeInTheDocument();
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
        name: "Open Audio",
      }),
    ).toBeVisible();
    expect(screen.queryByText("Prompt not recorded")).toBeNull();
    expect(screen.getByText("Sound model")).toBeVisible();
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
        name: "Open Audio",
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
    expect(screen.getByText("2:05")).toBeVisible();
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

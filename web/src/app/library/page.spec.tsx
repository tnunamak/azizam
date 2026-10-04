/* eslint-disable @next/next/no-img-element */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import LibraryPage from "./page";

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

  test("links the request receipt and explains absent prompt provenance honestly", async () => {
    renderLibrary();

    (
      await screen.findByRole("button", { name: "Open Image generation" })
    ).click();

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "View receipt req_image_123" }),
      ).toHaveAttribute("href", "/r/req_image_123"),
    );
    expect(
      screen.getByText(
        "Not recorded. The gateway does not report whether the prompt was omitted by policy or unavailable from the backend.",
      ),
    ).toBeVisible();
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
      await screen.findByRole("button", { name: "Open Image generation" })
    ).click();

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

    await waitFor(() =>
      expect(
        screen.getAllByText("Request receipt").at(-1)?.nextSibling,
      ).toHaveTextContent("Not recorded"),
    );
    expect(
      screen.queryByRole("link", { name: /View receipt/ }),
    ).not.toBeInTheDocument();
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
    expect(screen.getByText("audio.generate.test-model")).toBeVisible();
    expect(screen.queryByText("Untitled generation")).not.toBeInTheDocument();
  });

  test("falls back to a kind-specific title when the prompt is not recorded", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      Response.json({
        assets: [{ ...audioAsset, prompt: "" }],
        models: ["audio.generate.test-model"],
        next_cursor: null,
      }),
    );
    renderLibrary();

    expect(
      await screen.findByRole("button", { name: "Open Audio generation" }),
    ).toBeVisible();
    expect(screen.getByText("Audio generation")).toBeVisible();
    expect(screen.getByText("audio.generate.test-model")).toBeVisible();
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
      await screen.findByRole("button", { name: "Open Audio generation" }),
    ).toBeVisible();
    expect(screen.getByText("Audio generation")).toBeVisible();
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
      await screen.findByText("Playback blocked. Try again."),
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

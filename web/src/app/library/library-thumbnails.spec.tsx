/* eslint-disable @next/next/no-img-element */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import LibraryPage from "./page";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("next/image", () => ({
  default: (props: { alt: string; src: string; unoptimized?: boolean; [key: string]: unknown }) => {
    const { alt, src, ...imageProps } = props;
    delete imageProps.unoptimized;
    return <img alt={alt} src={src} {...imageProps} />;
  },
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: "member", name: "Alex", id: "alex" }),
}));
vi.mock("@/lib/product-api", () => ({
  productApi: {
    home: async () => ({ receipts: [], user: { id: "alex" }, apps: [] }),
    models: async () => ({ aliases: [], models: [] }),
  },
}));
vi.mock("@/lib/toast", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

const now = Math.floor(Date.now() / 1000);
const item = (id: string, kind: string, extra: object = {}) => ({
  asset_id: id, request_id: null, kind, prompt: `Prompt ${id}`, model: "image", created_at: now,
  width: 1536, height: 1024, download_url: `/signed/${id}/original`, ...extra,
});

function renderLibrary(assets: unknown[]) {
  const requested: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    requested.push(path);
    if (path.startsWith("/me/api/media?")) return Response.json({ assets, models: [], next_cursor: null });
    return Response.json({});
  }));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><LibraryPage /></QueryClientProvider>);
  return requested;
}

describe("Library tiles load small previews", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/library/");
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test("an image tile shows the thumbnail, lazily and with its size, not the original", async () => {
    renderLibrary([item("pic", "image", { thumbnail_url: "/signed/pic/thumb" })]);
    const image = await screen.findByAltText("Prompt pic");
    expect(image).toHaveAttribute("src", "/signed/pic/thumb");
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("decoding", "async");
    expect(image).toHaveAttribute("width", "1536");
    expect(image).toHaveAttribute("height", "1024");
  });

  test("a thumbnail that fails to load falls back to the original", async () => {
    renderLibrary([item("pic", "image", { thumbnail_url: "/signed/pic/thumb" })]);
    const image = await screen.findByAltText("Prompt pic");
    fireEvent.error(image);
    await waitFor(() => expect(screen.getByAltText("Prompt pic")).toHaveAttribute("src", "/signed/pic/original"));
  });

  test("an image without a thumbnail still shows the original", async () => {
    renderLibrary([item("pic", "image")]);
    expect(await screen.findByAltText("Prompt pic")).toHaveAttribute("src", "/signed/pic/original");
  });

  test("a video tile shows a poster, loads no video bytes and asks for no poster link", async () => {
    const requested = renderLibrary([
      item("clip", "video", { thumbnail_url: "/signed/clip/thumb", poster_asset_id: "poster", duration_s: 4 }),
    ]);
    const poster = await screen.findByAltText("Prompt clip");
    expect(poster).toHaveAttribute("src", "/signed/clip/thumb");
    expect(poster).toHaveAttribute("loading", "lazy");
    const video = document.querySelector("video");
    expect(video).not.toBeNull();
    expect(video).toHaveAttribute("preload", "none");
    expect(video).not.toHaveAttribute("src");
    expect(requested.filter((path) => path.includes("/download?"))).toEqual([]);
  });

  test("a video whose preview fails to load falls back to the source frame at 0.1 s", async () => {
    renderLibrary([item("clip", "video", { thumbnail_url: "/signed/clip/thumb", duration_s: 4 })]);
    fireEvent.error(await screen.findByAltText("Prompt clip"));
    await waitFor(() => expect(document.querySelector("video")).toHaveAttribute("src", "/signed/clip/original#t=0.1"));
    expect(document.querySelector("video")).toHaveAttribute("preload", "metadata");
  });
});

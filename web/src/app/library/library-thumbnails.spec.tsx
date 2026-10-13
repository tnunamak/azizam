/* eslint-disable @next/next/no-img-element */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import LibraryPage from "./page";

const imageRenders = vi.hoisted(() => [] as string[]);

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("next/image", () => ({
  default: (props: { alt: string; src: string; unoptimized?: boolean; [key: string]: unknown }) => {
    const { alt, src, ...imageProps } = props;
    imageRenders.push(src);
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
    if (path.includes("/download?")) {
      const id = decodeURIComponent(path.split("/")[4]);
      return Response.json({ download_url: `/signed/${id}/original` });
    }
    return Response.json({});
  }));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><LibraryPage /></QueryClientProvider>);
  return requested;
}

describe("Library tiles load small previews", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/library/");
    imageRenders.length = 0;
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
    expect(image).toHaveAttribute("width", "480");
    expect(image).toHaveAttribute("height", "320");
  });

  test("a large grid retains every tile and skips image rerenders and list fetches for unrelated state", async () => {
    const assets = Array.from({ length: 240 }, (_, index) =>
      item(`pic-${index}`, "image", { thumbnail_url: `/signed/pic-${index}/thumb` }),
    );
    const requested = renderLibrary(assets);
    const last = await screen.findByAltText("Prompt pic-239");
    expect(document.querySelectorAll("[data-asset-open]")).toHaveLength(240);
    expect(last.parentElement?.style.contentVisibility).toBe("auto");
    const renders = imageRenders.length;
    const nodes = Array.from(document.querySelectorAll("[data-asset-open]"));
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(imageRenders).toHaveLength(renders);
    expect(Array.from(document.querySelectorAll("[data-asset-open]"))).toEqual(nodes);
    expect(requested.filter((path) => path.startsWith("/me/api/media?"))).toHaveLength(1);
  });

  test("opening a tile keeps other tile previews unchanged and uses the original in the viewer", async () => {
    const requested = renderLibrary([
      item("pic", "image", { thumbnail_url: "/signed/pic/thumb" }),
      item("other", "image", { thumbnail_url: "/signed/other/thumb" }),
    ]);
    await screen.findByAltText("Prompt other");
    const otherRenders = imageRenders.filter((src) => src === "/signed/other/thumb").length;
    fireEvent.click(screen.getByRole("button", { name: "Open Prompt pic" }));
    await waitFor(() => {
      const original = document.querySelector('img[src="/signed/pic/original"]');
      expect(original).toHaveAttribute("width", "1536");
      expect(original).toHaveAttribute("height", "1024");
    });
    expect(imageRenders.filter((src) => src === "/signed/other/thumb")).toHaveLength(otherRenders);
    expect(requested.filter((path) => path.startsWith("/me/api/media?"))).toHaveLength(1);
  });

  test("a thumbnail that fails to load falls back to the original", async () => {
    renderLibrary([item("pic", "image", { thumbnail_url: "/signed/pic/thumb" })]);
    const image = await screen.findByAltText("Prompt pic");
    fireEvent.error(image);
    await waitFor(() => expect(screen.getByAltText("Prompt pic")).toHaveAttribute("src", "/signed/pic/original"));
    expect(screen.getByAltText("Prompt pic")).toHaveAttribute("width", "1536");
    expect(screen.getByAltText("Prompt pic")).toHaveAttribute("height", "1024");
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
    expect(poster).toHaveAttribute("decoding", "async");
    expect(poster).toHaveAttribute("width", "480");
    expect(poster).toHaveAttribute("height", "320");
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

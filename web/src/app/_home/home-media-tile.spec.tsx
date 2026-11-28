import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MediaTile } from "@/app/_home/home-media-tile";
import type { HomeCreation } from "@/app/_home/home-api";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function tile(creation: HomeCreation) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ul>
        <MediaTile creation={creation} />
      </ul>
    </QueryClientProvider>,
  );
}

describe("Recent media tiles", () => {
  it("opens a picture in Library's viewer, named by its prompt", () => {
    tile({ asset_id: "img-1", kind: "image", prompt: "Raised bed, late afternoon", download_url: "/pic.png", created_at: 1_700_000_000 });
    const link = screen.getByRole("link", { name: /Raised bed, late afternoon/ });
    expect(link.getAttribute("href")).toMatch(/^\/library\/?\?asset=img-1$/);
    expect(screen.getByRole("img", { name: "Raised bed, late afternoon" })).toBeVisible();
  });

  it("falls back to a labelled placeholder when a picture will not load", () => {
    tile({ asset_id: "img-2", kind: "image", prompt: "Lost", download_url: "/gone.png", kind_info: { label: "Image", untitled: "Picture", recent: true } });
    fireEvent.error(screen.getByRole("img", { name: "Lost" }));
    expect(screen.getByRole("img", { name: "Image preview unavailable" })).toBeVisible();
    expect(screen.getByText("Preview unavailable")).toBeVisible();
  });

  it("gives an audio clip its own play button, apart from the link to Library", () => {
    tile({ asset_id: "aud-1", kind: "audio", prompt: "Garden reminder", download_url: "/clip.wav", size_bytes: 1000, kind_info: { label: "Audio", untitled: "Audio", recent: true } });
    expect(screen.getByRole("button", { name: "Play Garden reminder" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Audio in Library: Garden reminder" })).toBeVisible();
  });
});

describe("Recent kind words and generic previews", () => {
  it("uses the server label and untitled name for a model file", () => {
    const { container } = tile({
      asset_id: "ast-model",
      kind: "model",
      download_url: "/mesh.glb",
      kind_info: { label: "Sculpture", untitled: "Untitled sculpture", recent: true },
    });
    expect(screen.getByText("Sculpture")).toBeVisible();
    expect(screen.getByText("Untitled sculpture")).toBeVisible();
    expect(screen.getByRole("img", { name: "Sculpture preview unavailable" })).toBeVisible();
    expect(screen.getByRole("link").getAttribute("href")).toMatch(/^\/library\/?\?asset=ast-model$/);
    expect(container.querySelector("img,video,audio")).toBeNull();
  });

  it("does not invent a renderer for a new kind or its poster", () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const { container } = tile({
      asset_id: "future",
      kind: "smell",
      download_url: "/scent.bin",
      poster_asset_id: "poster",
      prompt: "Spring",
      kind_info: { label: "Scent", untitled: "Untitled scent", recent: true },
    });
    expect(screen.getByText("Spring")).toBeVisible();
    expect(screen.getByRole("img", { name: "Scent preview unavailable" })).toBeVisible();
    expect(container.querySelector("img,video,audio")).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("uses neutral File for an older row, never Image", () => {
    tile({ asset_id: "old", kind: "model" });
    expect(screen.getByRole("img", { name: "File preview unavailable" })).toBeVisible();
    expect(screen.queryByText("Image")).toBeNull();
  });

  it("uses the declared label even when a known renderer fails", () => {
    tile({
      asset_id: "photo",
      kind: "image",
      prompt: "Garden",
      download_url: "/gone.png",
      kind_info: { label: "Photograph", untitled: "Untitled photograph", recent: true },
    });
    fireEvent.error(screen.getByRole("img", { name: "Garden" }));
    expect(screen.getByRole("img", { name: "Photograph preview unavailable" })).toBeVisible();
    expect(screen.getByText("Photograph")).toBeVisible();
  });

  it("renders server words as text, not markup", () => {
    const label = "<b>Untrusted label</b>";
    const { container } = tile({
      asset_id: "safe",
      kind: "smell",
      prompt: "Garden",
      kind_info: { label, untitled: "Unknown file", recent: true },
    });
    expect(screen.getByText(label)).toBeVisible();
    expect(container.querySelector("b")).toBeNull();
  });
});

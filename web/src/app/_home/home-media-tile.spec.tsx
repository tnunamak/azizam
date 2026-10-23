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
    const link = screen.getByRole("link", { name: "Open Image in Library: Raised bed, late afternoon" });
    expect(link.getAttribute("href")).toMatch(/^\/library\/?\?asset=img-1$/);
    expect(screen.getByRole("img", { name: "Raised bed, late afternoon" })).toBeVisible();
  });

  it("falls back to a labelled placeholder when a picture will not load", () => {
    tile({ asset_id: "img-2", kind: "image", prompt: "Lost", download_url: "/gone.png" });
    fireEvent.error(screen.getByRole("img", { name: "Lost" }));
    expect(screen.getByRole("img", { name: "Image preview unavailable" })).toBeVisible();
    expect(screen.getByText("Preview unavailable")).toBeVisible();
  });

  it("gives an audio clip its own play button, apart from the link to Library", () => {
    tile({ asset_id: "aud-1", kind: "audio", prompt: "Garden reminder", download_url: "/clip.wav", size_bytes: 1000 });
    expect(screen.getByRole("button", { name: "Play Garden reminder" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Audio in Library: Garden reminder" })).toBeVisible();
  });
});

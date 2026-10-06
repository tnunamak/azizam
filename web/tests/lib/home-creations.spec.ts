import { afterEach, describe, expect, it, vi } from "vitest";
import { hasHomeCreationContent, homeApi } from "@/app/home/home-api";

afterEach(() => vi.unstubAllGlobals());

describe("Home recent creations", () => {
  it("requires visible content for each media kind", () => {
    expect(
      hasHomeCreationContent({ asset_id: "image", kind: "image", download_url: "/image" }),
    ).toBe(true);
    expect(
      hasHomeCreationContent({ asset_id: "video", kind: "video", poster_asset_id: "poster" }),
    ).toBe(true);
    expect(
      hasHomeCreationContent({ asset_id: "audio", kind: "audio", download_url: "/audio", prompt: "Read this" }),
    ).toBe(true);
    expect(
      hasHomeCreationContent({ asset_id: "empty", kind: "image" }),
    ).toBe(false);
    expect(
      hasHomeCreationContent({ asset_id: "empty-audio", kind: "audio", download_url: "/audio" }),
    ).toBe(false);
  });

  it("continues past newer items without content to find recent creations", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            assets: [{ asset_id: "expired", kind: "image" }],
            has_more: true,
            next_cursor: "older-page",
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            assets: [
              { asset_id: "older-image", kind: "image", download_url: "/image" },
              { asset_id: "older-audio", kind: "audio", download_url: "/audio", prompt: "Recorded words" },
            ],
            has_more: false,
          }),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const result = await homeApi.creations();

    expect(result.assets.map((asset) => asset.asset_id)).toEqual([
      "older-image",
      "older-audio",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1][0])).toContain("cursor=older-page");
  });
});

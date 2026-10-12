import { afterEach, describe, expect, it, vi } from "vitest";
import { hasHomeCreationContent, homeApi } from "@/app/_home/home-api";

afterEach(() => vi.unstubAllGlobals());

describe("Home recent creations", () => {
  it("keeps neutral icon fallbacks for each media kind", () => {
    expect(
      hasHomeCreationContent({
        asset_id: "image",
        kind: "image",
        download_url: "/image",
      }),
    ).toBe(true);
    expect(
      hasHomeCreationContent({
        asset_id: "video",
        kind: "video",
        poster_asset_id: "poster",
      }),
    ).toBe(true);
    expect(
      hasHomeCreationContent({
        asset_id: "video-file",
        kind: "video",
        download_url: "/video",
      }),
    ).toBe(true);
    expect(
      hasHomeCreationContent({
        asset_id: "audio",
        kind: "audio",
        download_url: "/audio",
        prompt: "Read this",
      }),
    ).toBe(true);
    expect(hasHomeCreationContent({ asset_id: "empty", kind: "image" })).toBe(
      true,
    );
    expect(
      hasHomeCreationContent({
        asset_id: "empty-audio",
        kind: "audio",
        download_url: "/audio",
      }),
    ).toBe(true);
    expect(
      hasHomeCreationContent({ asset_id: "empty-video", kind: "video" }),
    ).toBe(true);
  });

  it("prefers previewable creations over newer icon-only fallbacks", async () => {
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
              {
                asset_id: "older-image",
                kind: "image",
                download_url: "/image",
              },
              {
                asset_id: "older-audio",
                kind: "audio",
                download_url: "/audio",
                prompt: "Recorded words",
              },
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
      "expired",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1][0])).toContain("cursor=older-page");
  });

  it("bounds Recent pagination and returns accumulated fallbacks", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const page = new URL(
        String(input),
        "http://azizam.test",
      ).searchParams.get("cursor");
      return new Response(
        JSON.stringify({
          assets: [{ asset_id: `fallback-${page ?? "first"}`, kind: "image" }],
          has_more: true,
          next_cursor:
            page === "page-2"
              ? "page-3"
              : page === "page-1"
                ? "page-2"
                : "page-1",
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await homeApi.creations();

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.assets.map((asset) => asset.asset_id)).toEqual([
      "fallback-first",
      "fallback-page-1",
      "fallback-page-2",
    ]);
  });

  it("keeps accumulated Recent items when a later page fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            assets: [{ asset_id: "first", kind: "image" }],
            has_more: true,
            next_cursor: "older",
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { message: "Failed" } }), {
          status: 503,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const result = await homeApi.creations();

    expect(result.assets.map((asset) => asset.asset_id)).toEqual(["first"]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.partial).toBe(true);
  });

  it("HL-07 stops when the server repeats a cursor", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({
        assets: [{ asset_id: "fallback", kind: "image" }],
        has_more: true,
        next_cursor: "same-cursor",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await homeApi.creations();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.partial).toBe(true);
  });
});

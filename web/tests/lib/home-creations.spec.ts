import { afterEach, describe, expect, it, vi } from "vitest";
import { hasHomeCreationContent, homeApi } from "@/app/_home/home-api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Home recent media", () => {
  it("counts only pictures, videos and audio as creations", () => {
    expect(hasHomeCreationContent({ asset_id: "a", kind: "image" })).toBe(true);
    expect(hasHomeCreationContent({ asset_id: "v", kind: "video" })).toBe(true);
    expect(hasHomeCreationContent({ asset_id: "x", kind: "text" as never })).toBe(false);
  });

  it("asks for one page of eight and keeps only creation content", async () => {
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requested.push(String(input));
        return Response.json({
          assets: [
            { asset_id: "pic", kind: "image" },
            { asset_id: "note", kind: "text" },
          ],
          has_more: true,
          next_cursor: "more",
        });
      }),
    );
    const result = await homeApi.creations();
    expect(requested).toEqual(["/me/api/media?limit=8"]);
    expect(result.assets.map((asset) => asset.asset_id)).toEqual(["pic"]);
  });
});

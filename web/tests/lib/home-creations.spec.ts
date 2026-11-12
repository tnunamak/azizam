import { afterEach, describe, expect, it, vi } from "vitest";
import { hasHomeCreationContent, homeApi, type HomeCreation } from "@/app/_home/home-api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function asset(id: string, kind: string, recent?: boolean): HomeCreation {
  return {
    asset_id: id,
    kind,
    ...(recent === undefined ? {} : { kind_info: { label: "Server kind", untitled: "Server item", recent } }),
  };
}

function kindRow(kind: string, recent?: boolean, label = `${kind} word`) {
  return { kind, label, plural: `${label}s`, untitled: `Untitled ${label}`, input: kind, ...(recent === undefined ? {} : { recent }) };
}

describe("Home recent media is declared by data", () => {
  it("keeps model and unknown kinds without a preview renderer", () => {
    expect(hasHomeCreationContent(asset("m", "model", true))).toBe(true);
    expect(hasHomeCreationContent(asset("s", "smell", true))).toBe(true);
  });

  it("excludes by declaration, never by a hard-coded kind", () => {
    expect(hasHomeCreationContent(asset("t", "text", false))).toBe(false);
    expect(hasHomeCreationContent(asset("i", "image", false))).toBe(false);
    expect(hasHomeCreationContent(asset("t2", "text", true))).toBe(true);
  });

  it("keeps older and unknown rows unless explicitly excluded", () => {
    expect(hasHomeCreationContent(asset("old", "model"))).toBe(true);
    expect(hasHomeCreationContent(asset("new", "smell"))).toBe(true);
  });

  it("filters one page by the page's kind words and keeps its continuation and partial state", async () => {
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requested.push(String(input));
        return Response.json({
          assets: [asset("pic", "image"), asset("mesh", "model"), asset("new", "smell"), asset("note", "text")],
          kinds: [kindRow("image", true), kindRow("model", true, "3D model"), kindRow("text", false), "junk", { kind: 3 }],
          has_more: true,
          next_cursor: "more",
          partial: true,
        });
      }),
    );
    const result = await homeApi.creations();
    expect(requested).toEqual(["/me/api/media?limit=8"]);
    expect(result.assets.map((row) => row.asset_id)).toEqual(["pic", "mesh", "new"]);
    expect(result.assets[1].kind_info).toEqual({ label: "3D model", untitled: "Untitled 3D model", recent: true });
    expect(result.assets[2].kind_info).toBeUndefined();
    expect(result.has_more).toBe(true);
    expect(result.next_cursor).toBe("more");
    expect(result.partial).toBe(true);
  });

  it("shows a kind whose row sets recent true, even text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ assets: [asset("note", "text")], kinds: [kindRow("text", true)] })),
    );
    expect((await homeApi.creations()).assets.map((row) => row.asset_id)).toEqual(["note"]);
  });
});

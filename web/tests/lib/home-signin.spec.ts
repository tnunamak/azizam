import { afterEach, describe, expect, it, vi } from "vitest";
import { homeApi } from "@/app/home/home-api";

afterEach(() => vi.unstubAllGlobals());
describe("Home session boundary", () => {
  it("offers sign-in after the Home API reports an expired or absent session", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ error: { code: "authentication_required" } }),
            { status: 401 },
          ),
        ),
    );
    await expect(homeApi.home()).resolves.toEqual({ authenticated: false });
  });
  it("keeps operational failures visible instead of treating them as signed out", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({
              error: { code: "unavailable", message: "Try again" },
            }),
            { status: 503 },
          ),
        ),
    );
    await expect(homeApi.home()).rejects.toMatchObject({
      status: 503,
      message: "Try again",
    });
  });
});

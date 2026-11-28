import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { homeApi } from "@/app/_home/home-api";
import { navigate } from "@/lib/navigate";
import * as sessionEnd from "@/lib/session-end";

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

beforeEach(() => {
  vi.spyOn(navigate, "replace").mockImplementation(() => {});
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/logout") return json({ next: "/sign-in/" });
      if (String(input) === "/me/api/session") return json({ authenticated: true, csrf_token: "csrf" });
      return json({ download_url: "/me/api/media/asset-1/file" });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  sessionEnd.resetSessionStateForTests();
});

// Home's requests are session-bound: the guard in networkFetch must refuse them once the session has ended.
it("a Home request made after the session ended is refused and never sent", async () => {
  await sessionEnd.endSession("sign-out");
  vi.mocked(fetch).mockClear();
  await expect(homeApi.mediaDownload("asset-1")).rejects.toMatchObject({ code: "session_ended" });
  expect(fetch).not.toHaveBeenCalled();
});

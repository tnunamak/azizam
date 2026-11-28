import { afterEach, describe, expect, it, vi } from "vitest";
import { accountApi } from "@/lib/account-api";

const response = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});
afterEach(() => vi.unstubAllGlobals());
describe("member account writes", () => {
  it("uses the member session CSRF token without requiring admin privileges", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response({ authenticated: true, csrf_token: "test-csrf", apps: [] }),
      )
      .mockResolvedValueOnce(response({ app: { id: "owned-app" } }, 201));
    vi.stubGlobal("fetch", fetch);
    expect(await accountApi.createApp("Laptop")).toEqual({ id: "owned-app" });
    expect(fetch.mock.calls[0][0]).toBe("/me/api/session");
    const [path, init] = fetch.mock.calls[1];
    expect(path).toBe("/me/api/apps");
    expect(init.headers.get("x-azizam-user-action")).toBe("1");
    expect(init.headers.get("x-azizam-csrf")).toBe("test-csrf");
    expect(init.credentials).toBe("same-origin");
  });
  it("refreshes a rolled session once and uses the replacement token", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ csrf_token: "old" }))
      .mockResolvedValueOnce(
        response(
          { error: { code: "user_csrf_required", message: "Session changed" } },
          403,
        ),
      )
      .mockResolvedValueOnce(response({ csrf_token: "new" }))
      .mockResolvedValueOnce(response({ credential: { id: "new-key" } }, 201));
    vi.stubGlobal("fetch", fetch);
    await accountApi.mintKey("owned/app", "Laptop");
    expect(fetch.mock.calls[3][0]).toBe("/me/api/apps/owned%2Fapp/keys");
    expect(fetch.mock.calls[3][1].headers.get("x-azizam-csrf")).toBe("new");
  });
  it("preserves a forbidden ownership response and does not retry it", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ csrf_token: "test-csrf" }))
      .mockResolvedValueOnce(
        response(
          {
            error: {
              code: "credential_not_found",
              message: "API key not found for this account",
            },
          },
          404,
        ),
      );
    vi.stubGlobal("fetch", fetch);
    await expect(
      accountApi.revokeKey("another-user-key"),
    ).rejects.toMatchObject({ code: "credential_not_found", status: 404 });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

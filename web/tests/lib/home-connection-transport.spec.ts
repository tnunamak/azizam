import { afterEach, describe, expect, it, vi } from "vitest";
import { accountApi } from "@/lib/account-api";
import { homeApi } from "@/app/_home/home-api";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

const session = (csrf_token: string) =>
  ({ authenticated: true, csrf_token } as Awaited<ReturnType<typeof accountApi.session>>);

const csrfFailure = () => json({
  error: { code: "user_csrf_required", message: "Fixture session changed" },
}, 403);

const result = {
  ok: true,
  latency_ms: 1,
  reason: "Fixture gateway answered",
  path: "/v1/models",
  model: null,
  request_id: null,
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Home connection transport", () => {
  it("refreshes CSRF once and replays the exact selected credential payload", async () => {
    const sessions = vi.spyOn(accountApi, "session")
      .mockResolvedValueOnce(session("fixture-old"))
      .mockResolvedValueOnce(session("fixture-new"));
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(csrfFailure())
      .mockResolvedValueOnce(json(result));
    vi.stubGlobal("fetch", fetchMock);

    await expect(homeApi.test("fixture-secret", "fixture-credential"))
      .resolves.toEqual(result);
    expect(sessions).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [index, call] of fetchMock.mock.calls.entries()) {
      const [path, init] = call;
      expect(path).toBe("/me/api/connection-test");
      expect(init?.method).toBe("POST");
      expect(init?.credentials).toBe("same-origin");
      const headers = new Headers(init?.headers);
      expect(headers.get("content-type")).toBe("application/json");
      expect(headers.get("x-azizam-user-action")).toBe("1");
      expect(headers.get("x-azizam-csrf"))
        .toBe(index === 0 ? "fixture-old" : "fixture-new");
      expect(JSON.parse(init?.body as string)).toEqual({
        api_key: "fixture-secret",
        credential_id: "fixture-credential",
      });
    }
    expect(fetchMock.mock.calls[0][1]?.body)
      .toBe(fetchMock.mock.calls[1][1]?.body);
  });

  it("stops after a second CSRF rejection instead of retrying indefinitely", async () => {
    const sessions = vi.spyOn(accountApi, "session")
      .mockResolvedValue(session("fixture-csrf"));
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(csrfFailure())
      .mockResolvedValueOnce(csrfFailure())
      .mockImplementation(async () => json({
        error: { code: "unexpected_third_attempt", message: "Fixture sentinel" },
      }, 500));
    vi.stubGlobal("fetch", fetchMock);

    await expect(homeApi.test("fixture-secret", "fixture-credential"))
      .rejects.toMatchObject({ code: "user_csrf_required", status: 403 });
    expect(sessions).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    { status: 403, code: "access_denied" },
    { status: 401, code: "authentication_required" },
    { status: 503, code: "unavailable" },
  ])("does not replay $status / $code", async ({ status, code }) => {
    const sessions = vi.spyOn(accountApi, "session")
      .mockResolvedValue(session("fixture-csrf"));
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () =>
      json({ error: { code, message: "Fixture rejection" } }, status),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(homeApi.test("fixture-secret", "fixture-credential"))
      .rejects.toMatchObject({ code, status });
    expect(sessions).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not replay a connection check after a network failure", async () => {
    vi.spyOn(accountApi, "session").mockResolvedValue(session("fixture-csrf"));
    const offline = new TypeError("Fixture network failure");
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(offline);
    vi.stubGlobal("fetch", fetchMock);

    await expect(homeApi.test("fixture-secret", "fixture-credential"))
      .rejects.toBe(offline);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

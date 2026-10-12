import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { fetchViewer } from "@/components/shell/session";

beforeEach(() => {
  vi.spyOn(api, "session").mockRejectedValue(
    new Error("Unexpected admin-session fallback"),
  );
  vi.stubGlobal("fetch", vi.fn());
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function memberSnapshot(body: unknown, status = 200) {
  vi.mocked(fetch).mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

describe("viewer fallback contract", () => {
  it.each([
    ["member", false],
    ["owner", true],
    ["admin", true],
    ["superadmin", true],
    ["unknown-role", false],
  ] as const)("keeps member snapshot role %s authoritative", async (role, admin) => {
    memberSnapshot({
      authenticated: true,
      user: {
        id: "person-fixture",
        name: "River",
        email: "river@example.test",
        role,
      },
    });
    expect(await fetchViewer()).toEqual({
      authenticated: true,
      id: "person-fixture",
      name: "River",
      email: "river@example.test",
      role,
      admin,
    });
    expect(api.session).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/me/api/session", {
      credentials: "same-origin",
      cache: "no-store",
    });
  });

  it.each([401, 403])("treats only fallback HTTP %s as signed out", async (status) => {
    memberSnapshot({ authenticated: false });
    vi.mocked(api.session).mockRejectedValue(
      new ApiError("denied", "fixture diagnostic", status),
    );
    expect(await fetchViewer()).toEqual({
      authenticated: false,
      name: "Your account",
      email: null,
      role: "member",
      admin: false,
    });
    expect(api.session).toHaveBeenCalledOnce();
  });

  it.each([
    new ApiError("unavailable", "fixture 500", 500),
    new ApiError("unavailable", "fixture 503", 503),
    new TypeError("fixture network failure"),
    new Error("fixture unexpected failure"),
  ])("preserves unexpected fallback failure: %s", async (error) => {
    memberSnapshot({ authenticated: false });
    vi.mocked(api.session).mockRejectedValue(error);
    await expect(fetchViewer()).rejects.toBe(error);
  });

  it("does not switch identities when the member endpoint fails", async () => {
    memberSnapshot({}, 503);
    await expect(fetchViewer()).rejects.toThrow(
      "Your account could not be loaded.",
    );
    expect(api.session).not.toHaveBeenCalled();
  });

  it("does not switch identities when the member JSON is invalid", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("{invalid"));
    await expect(fetchViewer()).rejects.toBeInstanceOf(SyntaxError);
    expect(api.session).not.toHaveBeenCalled();
  });

  it.each([
    ["service_account", true, "Fixture automation"],
    ["user", false, "Gateway owner"],
    [undefined, false, "Gateway owner"],
  ] as const)("distinguishes fallback actor kind %s", async (kind, serviceAdmin, name) => {
    memberSnapshot({ authenticated: false });
    vi.mocked(api.session).mockResolvedValue({
      actor: {
        subject: "fixture-actor",
        kind,
        label: "Fixture automation",
        email: null,
        roles: ["member", "admin"],
        csrf_token: "fixture-csrf",
      },
    } as Awaited<ReturnType<typeof api.session>>);
    expect(await fetchViewer()).toEqual({
      authenticated: true,
      serviceAdmin,
      name,
      email: null,
      role: "member",
      admin: true,
    });
  });
});

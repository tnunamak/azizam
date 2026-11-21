import { afterEach, describe, expect, test, vi } from "vitest";

import { accountApi, type AccountSnapshot } from "./account-api";
import { ADAPTER_UPLOAD_PATH, sendAdapterUpload } from "./adapter-upload-send";

afterEach(() => vi.restoreAllMocks());

describe("sendAdapterUpload", () => {
  test("posts the form to the member route with the user-action and CSRF headers", async () => {
    vi.spyOn(accountApi, "session").mockResolvedValue({ csrf_token: "csrf-1" } as AccountSnapshot);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    const body = new FormData();
    body.append("file", new File(["x"], "a.safetensors"));

    await sendAdapterUpload({ method: "POST", body });

    expect(ADAPTER_UPLOAD_PATH).toBe("/me/api/adapters");
    const [path, init] = fetchSpy.mock.calls[0];
    expect(path).toBe("/me/api/adapters");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(body);
    expect(init?.credentials).toBe("same-origin");
    expect(init?.headers).toEqual({ "x-azizam-user-action": "1", "x-azizam-csrf": "csrf-1" });
  });

  test("sets no content type, so the browser writes the multipart boundary", async () => {
    vi.spyOn(accountApi, "session").mockResolvedValue({ csrf_token: "c" } as AccountSnapshot);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
    await sendAdapterUpload({ method: "POST", body: new FormData() });
    const headers = fetchSpy.mock.calls[0][1]?.headers as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain("content-type");
  });

  test("sends once more after a stale CSRF token, with a fresh session read", async () => {
    const session = vi
      .spyOn(accountApi, "session")
      .mockResolvedValueOnce({ csrf_token: "old" } as AccountSnapshot)
      .mockResolvedValueOnce({ csrf_token: "new" } as AccountSnapshot);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(Response.json({ error: { code: "user_csrf_required", message: "x" } }, { status: 403 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    const response = await sendAdapterUpload({ method: "POST", body: new FormData() });
    expect(response.status).toBe(200);
    expect(session).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls[1][1]?.headers).toEqual({ "x-azizam-user-action": "1", "x-azizam-csrf": "new" });
  });

  test("sends only once more, and not at all for another refusal", async () => {
    vi.spyOn(accountApi, "session").mockResolvedValue({ csrf_token: "c" } as AccountSnapshot);
    const stale = () => Response.json({ error: { code: "user_csrf_required", message: "x" } }, { status: 403 });
    const twice = vi.spyOn(globalThis, "fetch").mockImplementation(async () => stale());
    expect((await sendAdapterUpload({ method: "POST", body: new FormData() })).status).toBe(403);
    expect(twice).toHaveBeenCalledTimes(2);
    twice.mockReset();
    twice.mockImplementation(async () => Response.json({ error: { code: "adapter_uploads_off", message: "x" } }, { status: 403 }));
    expect((await sendAdapterUpload({ method: "POST", body: new FormData() })).status).toBe(403);
    expect(twice).toHaveBeenCalledTimes(1);
  });
});

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
});

import { describe, expect, it } from "vitest";
import { describeAdapterUploaded, uploadAdapter, type AdapterUploaded } from "./adapter-upload";
import { NETWORK_FAILURE_MESSAGE, NetworkUnavailableError } from "./network-error";

const offered: AdapterUploaded = {
  id: "ast_example",
  object: "gateway.asset",
  kind: "adapter",
  media_type: "adapter/safetensors",
  size_bytes: 3,
  architectures: ["example"],
  offered: true,
};
const unreadable = {
  ok: false,
  code: "unreadable_reply",
  message: "The gateway sent a reply this page cannot read. Try again.",
};

function picked() {
  return new File(["xyz"], "my style.safetensors");
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("uploadAdapter", () => {
  it("sends exactly one named file part and returns the offered adapter", async () => {
    const result = await uploadAdapter(picked(), async ({ method, body }) => {
      expect(method).toBe("POST");
      const entries = Array.from(body.entries());
      expect(entries.map(([name]) => name)).toEqual(["file"]);
      expect(entries[0]?.[1]).toMatchObject({ name: "my style.safetensors", size: 3 });
      return json(offered);
    });
    expect(result).toEqual({ ok: true, adapter: offered });
    expect(describeAdapterUploaded(offered)).toBe("Added to your Library and ready to pick.");
  });

  it("returns an unoffered adapter and describes the gateway note or the default", async () => {
    const notOffered = { ...offered, offered: false, architectures: [] };
    const noted = { ...notOffered, note: "Stored, but no matching workflow yet." };
    expect(await uploadAdapter(picked(), async () => json(noted))).toEqual({
      ok: true,
      adapter: noted,
    });
    expect(describeAdapterUploaded(noted)).toBe(noted.note);
    expect(describeAdapterUploaded(notOffered)).toBe(
      "Added to your Library, but no workflow offers it yet.",
    );
  });

  it.each([
    [401, "unauthorized", "Sign in required"],
    [413, "adapter_too_large", "The file is larger than the 42-byte limit for adapters."],
    [400, "missing_file", "Send the adapter as one multipart/form-data file part named 'file'."],
    [507, "adapter_store_failed", "The adapter could not be stored. Try again, and ask the owner if it continues."],
    [403, "adapter_uploads_off", "Adapter uploads are off. The owner can turn them on."],
  ])("preserves the gateway refusal for %i %s", async (status, code, message) => {
    expect(await uploadAdapter(picked(), async () => json({ error: { code, message } }, status)))
      .toEqual({ ok: false, code, message });
  });

  it("rejects primitive and array roots, even if a nonstandard JSON decoder attaches an id", async () => {
    expect(await uploadAdapter(picked(), async () => json("not an object"))).toEqual(unreadable);
    for (const root of [Object.assign([], { id: offered.id }), Object.assign(() => {}, { id: offered.id })]) {
      const response = json(null);
      response.json = async () => root;
      expect(await uploadAdapter(picked(), async () => response)).toEqual(unreadable);
    }
  });

  it("rejects a null JSON body or a null error", async () => {
    expect(await uploadAdapter(picked(), async () => json(null))).toEqual(unreadable);
    expect(await uploadAdapter(picked(), async () => json({ error: null }, 400))).toEqual(unreadable);
  });

  it("does not accept an adapter id when the HTTP reply is unsuccessful", async () => {
    expect(await uploadAdapter(picked(), async () => json(offered, 400))).toEqual(unreadable);
  });

  it("does not pass through a non-string refusal code", async () => {
    expect(await uploadAdapter(picked(), async () => json({ error: { code: 42, message: "Refused" } }, 400)))
      .toEqual(unreadable);
  });

  it("rethrows a sender error that is not a network failure", async () => {
    const interruption = new Error("Session ended");
    await expect(uploadAdapter(picked(), async () => {
      throw interruption;
    })).rejects.toBe(interruption);
  });

  it("uses shared network copy for NetworkUnavailableError", async () => {
    expect(await uploadAdapter(picked(), async () => {
      throw new NetworkUnavailableError();
    })).toEqual({ ok: false, code: "network", message: NETWORK_FAILURE_MESSAGE });
  });

  it("handles a non-JSON body", async () => {
    expect(await uploadAdapter(picked(), async () => new Response("not JSON"))).toEqual(unreadable);
  });

  it("handles JSON missing its success id or refusal fields", async () => {
    const withoutId = { ...offered, id: undefined };
    expect(await uploadAdapter(picked(), async () => json(withoutId))).toEqual(unreadable);
    expect(await uploadAdapter(picked(), async () => json({ error: { code: "unknown" } }, 400)))
      .toEqual(unreadable);
  });

  it("uses the shared network copy when sending rejects with a fetch TypeError", async () => {
    expect(await uploadAdapter(picked(), async () => {
      throw new TypeError("Failed to fetch");
    })).toEqual({ ok: false, code: "network", message: NETWORK_FAILURE_MESSAGE });
  });
});

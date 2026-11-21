import { isNetworkFailure, NETWORK_FAILURE_MESSAGE } from "./network-error";

export type AdapterUploaded = {
  id: string;
  object: "gateway.asset";
  kind: "adapter";
  media_type: "adapter/safetensors";
  size_bytes: number;
  architectures: string[];
  offered: boolean;
  note?: string;
};

export type AdapterUploadResult =
  | { ok: true; adapter: AdapterUploaded }
  | { ok: false; code: string; message: string };

function unreadableReply(): AdapterUploadResult {
  return {
    ok: false,
    code: "unreadable_reply",
    message: "The gateway sent a reply this page cannot read. Try again.",
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function uploadAdapter(
  file: File,
  send: (init: { method: "POST"; body: FormData }) => Promise<Response>,
): Promise<AdapterUploadResult> {
  const body = new FormData();
  body.append("file", file, file.name);

  let response: Response;
  try {
    response = await send({ method: "POST", body });
  } catch (error) {
    if (isNetworkFailure(error) || error instanceof TypeError) {
      return { ok: false, code: "network", message: NETWORK_FAILURE_MESSAGE };
    }
    // A caller-owned session interruption is not a network failure.
    throw error;
  }

  let reply: unknown;
  try {
    reply = await response.json();
  } catch {
    return unreadableReply();
  }
  if (!isRecord(reply)) return unreadableReply();
  if (response.ok) {
    if (
      typeof reply.id !== "string" ||
      typeof reply.size_bytes !== "number" ||
      typeof reply.offered !== "boolean" ||
      !Array.isArray(reply.architectures) ||
      !reply.architectures.every(architecture => typeof architecture === "string")
    ) return unreadableReply();
    return { ok: true, adapter: reply as AdapterUploaded };
  }
  if (isRecord(reply.error)) {
    const { code, message } = reply.error;
    if (typeof code === "string" && typeof message === "string") {
      return { ok: false, code, message: message.trim() ? message : "The upload did not go through. Try again." };
    }
  }
  return unreadableReply();
}

export function describeAdapterUploaded(adapter: AdapterUploaded): string {
  if (adapter.offered) return "Added to your Library and ready to pick.";
  return adapter.note ?? "Added to your Library, but no workflow offers it yet.";
}

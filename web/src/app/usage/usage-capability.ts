import type { Capability } from "@/components/product";

export type UsageCapability =
  | Capability
  | "audio"
  | "embeddings"
  | "rerank"
  | "other";

/**
 * Which task a usage key or receipt capability belongs to, so each row and
 * chart band takes the task name from lib/terms. Text read aloud ("speech",
 * "voice", "tts") is "Read text aloud"; other audio is music or sounds.
 */
export function normalizeCapability(value: unknown): UsageCapability {
  const text = String(value ?? "").toLowerCase();
  if (text === "default") return "chat";
  if (text === "stt" || text.includes("transcri")) return "transcribe";
  if (text.includes("image")) return "image";
  if (text.includes("video")) return "video";
  if (text.includes("rerank")) return "rerank";
  if (text.includes("embedding") || text.includes("embed")) return "embeddings";
  if (text.includes("speech") || text.includes("voice") || text === "tts")
    return "voice";
  if (text.includes("audio")) return "audio";
  if (text.includes("decision") || text.includes("decide")) return "decide";
  if (
    text.includes("chat") ||
    text.includes("completion") ||
    text.includes("openai")
  )
    return "chat";
  return "other";
}

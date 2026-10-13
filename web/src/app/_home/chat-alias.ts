import type { HomeAlias } from "./home-api";

const chatOperations = ["chat.generate", "chat", "chat.completions"];

/**
 * The chat model name a setup guide suggests for a key: the gateway's
 * recommendation, else a local model (so a guide does not quietly spend on a
 * provider when a free one is allowed), else any available chat model.
 */
export function chatAlias(
  aliases: HomeAlias[],
  recommended?: string | null,
): string | null {
  const chat = aliases.filter(
    (alias) => alias.available === true && chatOperations.includes(alias.operation),
  );
  return (
    chat.find((alias) => alias.id === recommended)?.id ??
    chat.find((alias) => alias.local === true)?.id ??
    chat[0]?.id ??
    null
  );
}

/** The transcription model name for a guide that needs one (OpenWhispr). */
export function transcriptionAlias(aliases: HomeAlias[]): string | null {
  const rows = aliases.filter(
    (alias) => alias.available === true && /transcri/.test(alias.operation),
  );
  return (
    rows.find((alias) => alias.local === true)?.id ?? rows[0]?.id ?? null
  );
}

/**
 * Which chat model a person starts with. It is read from the catalog's own
 * data, never from a model name: a model that takes pictures (a vision
 * model) and an embedding model are not ordinary chat models, so the default
 * never lands on one of them.
 */

export type ChatCandidate = {
  id: string;
  /** The catalog's own recommendation for chat. */
  recommended?: boolean;
  /** Position in the catalog's own order (aliases, then models). */
  order: number;
  /** Takes pictures as input. */
  pictures: boolean;
  /** This option is an embedding model, not a chat choice. */
  embedding: boolean;
};

function earliest<T extends ChatCandidate>(candidates: readonly T[]) {
  return candidates.reduce<T | undefined>(
    (first, candidate) => (!first || candidate.order < first.order ? candidate : first),
    undefined,
  );
}

/**
 * The default chat choice: the catalog's recommendation when it is an
 * ordinary chat model, else the first ordinary one in catalog order. An
 * embedding option is never the default. A row that both chats and embeds
 * offers a chat option, and that option is an ordinary chat choice. When
 * every chat model takes pictures, the first of those still stands, so Run
 * stays possible and the picker shows a choice. With no chat model at all,
 * there is no default.
 */
export function chatDefaultChoice<T extends ChatCandidate>(
  candidates: readonly T[],
): T | undefined {
  const chatModels = candidates.filter((candidate) => !candidate.embedding);
  const ordinary = chatModels.filter((candidate) => !candidate.pictures);
  if (!ordinary.length) return earliest(chatModels);
  return ordinary.find((candidate) => candidate.recommended) ?? earliest(ordinary);
}

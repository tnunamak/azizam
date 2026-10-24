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
  /** Makes embeddings rather than chat replies. */
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
 * ordinary chat model, else the first ordinary one in catalog order. When no
 * chat model is ordinary, the first in catalog order still stands, so Run
 * stays possible and the picker shows a choice.
 */
export function chatDefaultChoice<T extends ChatCandidate>(
  candidates: readonly T[],
): T | undefined {
  const ordinary = candidates.filter((candidate) => !candidate.pictures && !candidate.embedding);
  if (!ordinary.length) return earliest(candidates);
  return ordinary.find((candidate) => candidate.recommended) ?? earliest(ordinary);
}

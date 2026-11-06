/**
 * Is a workflow catalog input a picture (or other media file), or text?
 *
 * One rule for every screen that reads the workflow catalog: the Workflows
 * summaries ("Needs: a face photo and text") and Try's picture picker. It
 * reads only what the catalog declares (the asset flag, the media types, the
 * input type, the gateway's "Media input:" description), never the input's
 * name, so a text field called "image" stays text.
 */

export type MediaKind = "image" | "video" | "audio";

/** The catalog fields this rule reads; both screens' input types fit it. */
export interface CatalogInputLike {
  type: string;
  asset?: boolean;
  media_type?: string;
  accepted_media_types?: string[];
  description?: string;
}

/** The gateway's own description for every asset input; jargon to a member. */
export const MEDIA_DESCRIPTION = /^media input:/i;

/** Input types that are a file rather than a JSON value. */
const MEDIA_TYPES = new Set([
  "asset",
  "file",
  "image",
  "audio",
  "video",
  "binary",
  "upload",
  "blob",
]);

const KINDS: readonly MediaKind[] = ["image", "video", "audio"];

/** The noun for one kind of media, as people read it. */
export const MEDIA_NOUN: Record<MediaKind, string> = {
  image: "picture",
  video: "video",
  audio: "audio clip",
};

/** "picture" -> "a picture", "audio clip" -> "an audio clip". */
export function withArticle(noun: string): string {
  return /^[aeiou]/i.test(noun) ? `an ${noun}` : `a ${noun}`;
}

/** "image/png" -> "image". */
export function mediaFamily(value: string): string {
  return value.trim().toLowerCase().split("/")[0];
}

function isKind(value: string): value is MediaKind {
  return (KINDS as readonly string[]).includes(value);
}

/** The media types the catalog declares for an input, in its order. */
export function declaredMediaTypes(input: CatalogInputLike): string[] {
  return [input.media_type, ...(input.accepted_media_types ?? [])].filter(
    (value): value is string => typeof value === "string" && !!value.trim(),
  );
}

/** The kinds of media an input takes, from its declared media types, else its type. */
export function mediaKindsOf(input: CatalogInputLike): MediaKind[] {
  const declared = declaredMediaTypes(input).map(mediaFamily).filter(isKind);
  if (declared.length) return [...new Set(declared)];
  const type = input.type.toLowerCase();
  return isKind(type) ? [type] : [];
}

/** True when the input takes a picture, video, audio clip or other file, not text. */
export function isMediaInput(input: CatalogInputLike): boolean {
  return (
    input.asset === true ||
    MEDIA_TYPES.has(input.type.toLowerCase()) ||
    MEDIA_DESCRIPTION.test(input.description ?? "") ||
    mediaKindsOf(input).length > 0
  );
}

/** The one kind of media an input takes, or null when the catalog does not say. */
export function mediaKindOf(input: CatalogInputLike): MediaKind | null {
  return isMediaInput(input) ? (mediaKindsOf(input)[0] ?? null) : null;
}

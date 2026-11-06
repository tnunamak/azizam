import { MEDIA_NOUN, withArticle } from "./workflow-media-input";
import { kindLabel } from "./media-kind";

/**
 * The rules every media input shares: what it accepts, how big a file may be, and
 * the words it uses. No screen has a copy of these; `useMediaPick` and the Library
 * picker read them.
 */

/** Largest file a media input takes: it is read into the browser, then sent. */
export const MAX_MEDIA_BYTES = 10 * 1024 * 1024;
export const MAX_MEDIA_LABEL = "10 MB";

/** A file's size in the words a person reads under its name ("12 KB", "1.4 MB"). */
export function sizeWords(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return megabytes(bytes);
}

export function megabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

/**
 * The media types an input takes. Each is a full type ("image/png") or a family
 * ("image/*"). `undefined` or an empty list means any file.
 */
export type MediaTypes = readonly string[] | undefined;

/** The `accept` attribute for a file input, or undefined for any file. */
export function acceptAttribute(types: MediaTypes): string | undefined {
  return types?.length ? types.join(",") : undefined;
}

/** True when the file's type is one of the types, or the input takes any file. */
export function fileFitsTypes(file: { type: string }, types: MediaTypes): boolean {
  if (!types?.length) return true;
  const actual = file.type.toLowerCase();
  return types.some((type) => {
    const wanted = type.toLowerCase();
    return wanted.endsWith("/*") ? actual.startsWith(wanted.slice(0, -1)) : actual === wanted;
  });
}

/** The one family ("image") every type shares, or null when they differ or any file is taken. */
export function kindOfTypes(types: MediaTypes): string | null {
  const families = new Set((types ?? []).map((type) => type.trim().toLowerCase().split("/")[0]));
  return families.size === 1 ? [...families][0] || null : null;
}

/** The noun for one kind, as a person reads it: "picture", "video", "audio clip", "file". */
export function mediaNoun(kind: string | null): string {
  if (!kind) return "file";
  return (MEDIA_NOUN as Record<string, string>)[kind] ?? kindLabel(kind).toLowerCase();
}

/** "picture" -> "a picture"; re-exported so a screen needs one import for its words. */
export { withArticle };

export function readAsDataUri(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("Read failed"));
    reader.readAsDataURL(file);
  });
}

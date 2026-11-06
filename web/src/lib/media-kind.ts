import { AudioLines, Box, File, FileText, ImageIcon, Video, type LucideIcon } from "lucide-react";

/**
 * An asset's kind is the top-level part of its IANA media type ("image", "video", "audio", "model", "text"...), or
 * "file" when the gateway cannot tell. No kind is special to this code except for its icon; a kind with no icon still
 * lists, filters and downloads, under the name the gateway gives it and a plain file icon.
 */
export type MediaKind = string;

export const UNKNOWN_KIND = "file";

/** The words for one kind, as the gateway sends them in `GET /me/api/media` (gateway/asset_types.py). */
export interface KindNames {
  kind: string;
  /** One item: "Image". Also the word in a name such as "Image, Oct 20 at 7:45 PM". */
  label: string;
  /** The filter button: "Images". */
  plural: string;
  /** The name of an item with no prompt: "Picture". */
  untitled: string;
}

/**
 * Icons only, one per IANA top-level type this UI has a drawing for. The words for a kind live in the gateway, which
 * is the one table of them; a kind with no icon here gets the file icon.
 */
const KIND_ICONS: Record<string, LucideIcon> = {
  image: ImageIcon,
  video: Video,
  audio: AudioLines,
  model: Box,
  text: FileText,
  file: File,
};

/**
 * How the Library picker lays a kind out. A kind with a picture to show (an image, a video's first frame) is a grid
 * of thumbnails. Any other kind has nothing to look at, so its items differ only by name, length and date: they are
 * a list of rows, each with room for a full title. A kind not named here is a list.
 */
const KIND_LAYOUTS: Record<string, "grid" | "list"> = {
  image: "grid",
  video: "grid",
};

export function kindLayout(kind: MediaKind): "grid" | "list" {
  return KIND_LAYOUTS[kind] ?? "list";
}

/** The kinds the Library lists first, in this order; any other kind follows by name. */
const KIND_ORDER = ["image", "video", "audio"];

// The words the gateway last sent. A kind it never named reads as its own type, capitalised.
const knownNames = new Map<string, KindNames>();

/** Keeps the words the gateway sent for the kinds in the Library. Rows that are not well-formed are ignored. */
export function rememberKindNames(rows: readonly unknown[] | null | undefined): void {
  for (const row of rows ?? []) {
    if (!row || typeof row !== "object") continue;
    const { kind, label, plural, untitled } = row as Partial<KindNames>;
    if (typeof kind === "string" && kind && typeof label === "string" && label)
      knownNames.set(kind, { kind, label, plural: plural || label, untitled: untitled || label });
  }
}

function capitalized(kind: string): string {
  return kind ? kind[0].toUpperCase() + kind.slice(1) : "File";
}

/** The kind of a media type such as "model/gltf-binary": its top-level part, or "file" when unknown. */
export function kindFromMediaType(mediaType: string | null | undefined): MediaKind {
  const [top, rest] = String(mediaType ?? "").split(";")[0].trim().toLowerCase().split("/");
  if (!top || !rest || (top === "application" && rest === "octet-stream")) return UNKNOWN_KIND;
  return top;
}

export function kindIcon(kind: MediaKind): LucideIcon {
  return KIND_ICONS[kind] ?? KIND_ICONS[UNKNOWN_KIND];
}

/**
 * Whether the Library can hold items of this kind: the kinds it has a drawing for, or any kind the gateway named
 * for the member's Library. A media input offers "Choose from Library" only for a kind this says yes to; "file"
 * (a type nobody could name) and an input that takes any file are not a kind.
 */
export function libraryHoldsKind(kind: MediaKind | null | undefined): kind is MediaKind {
  return Boolean(kind) && kind !== UNKNOWN_KIND && (kind! in KIND_ICONS || knownNames.has(kind!));
}

export function kindLabel(kind: MediaKind): string {
  return knownNames.get(kind)?.label ?? capitalized(kind);
}

export function kindPluralLabel(kind: MediaKind): string {
  return knownNames.get(kind)?.plural ?? capitalized(kind);
}

export function kindUntitledName(kind: MediaKind): string {
  return knownNames.get(kind)?.untitled ?? capitalized(kind);
}

/** The filter list: "All", then only the kinds that occur, the familiar three first and the rest by name. */
export function kindFilterOptions(occurring: Iterable<MediaKind>): { value: string; label: string; icon?: LucideIcon }[] {
  const kinds = Array.from(new Set(Array.from(occurring).filter(Boolean)));
  kinds.sort((a, b) => {
    const rankA = KIND_ORDER.indexOf(a);
    const rankB = KIND_ORDER.indexOf(b);
    if (rankA !== -1 || rankB !== -1) return (rankA === -1 ? 99 : rankA) - (rankB === -1 ? 99 : rankB);
    return a.localeCompare(b);
  });
  return [
    { value: "all", label: "All" },
    ...kinds.map((kind) => ({ value: kind, label: kindPluralLabel(kind), icon: kindIcon(kind) })),
  ];
}

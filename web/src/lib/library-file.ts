import { SNIFF_BYTES, sniffKinds } from "./media-sniff";
import { networkFetch } from "./network-error";
import {
  MAX_MEDIA_BYTES,
  MAX_MEDIA_LABEL,
  fileFitsTypes,
  megabytes,
  type MediaTypes,
} from "./media-pick";

/** One item of the member's Library, as `GET /me/api/media` lists it. */
export type LibraryAsset = {
  asset_id: string;
  kind?: string;
  media_type?: string | null;
  size_bytes?: number | null;
  prompt?: string | null;
  available?: boolean;
  // Signed links the list carries for showing the item; they are not for sending.
  thumbnail_url?: string | null;
  download_url?: string | null;
};

export type LibraryPick = Pick<LibraryAsset, "asset_id" | "media_type" | "size_bytes">;

const SUBTYPE = /^[a-z]+\/([a-z0-9]+)/i;

/** The item is over the size limit; the message is what the person reads. */
export class TooBigError extends Error {
  constructor(noun: string, bytes?: number | null, maxBytes = MAX_MEDIA_BYTES) {
    const size = bytes ? `is ${megabytes(bytes)}` : `is over ${limitLabel(maxBytes)}`;
    super(`That ${noun} ${size}. Choose one under ${limitLabel(maxBytes)}.`);
  }
}

export function limitLabel(maxBytes: number): string {
  return maxBytes === MAX_MEDIA_BYTES ? MAX_MEDIA_LABEL : megabytes(maxBytes);
}

/**
 * Reads a body without holding more than the media limit: it stops as soon as
 * the bytes passed the limit (a missing or wrong Content-Length cannot get
 * round that) or the leading bytes are not the kind of media the input takes.
 * Only kinds the sniffer knows (picture, video, audio) are checked by their
 * bytes; any other kind rests on its declared type.
 */
async function readMedia(
  response: Response,
  kind: string,
  noun: string,
  knownBytes: number | null,
  maxBytes: number,
  signal: AbortSignal,
): Promise<Uint8Array[]> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Library download is empty");
  const chunks: Uint8Array[] = [];
  let total = 0;
  let sniffed = false;
  const sniffable = ["image", "video", "audio"].includes(kind);
  const check = () => {
    if (!sniffable) {
      sniffed = true;
      return;
    }
    const head = new Uint8Array(SNIFF_BYTES);
    let filled = 0;
    for (const chunk of chunks) {
      const part = chunk.subarray(0, SNIFF_BYTES - filled);
      head.set(part, filled);
      filled += part.length;
      if (filled >= SNIFF_BYTES) break;
    }
    if (!(sniffKinds(head.subarray(0, filled)) as string[]).includes(kind)) {
      throw new Error("Library download is not media");
    }
    sniffed = true;
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error("Library download was replaced");
      if (done) break;
      total += value.length;
      if (total > maxBytes) throw new TooBigError(noun, knownBytes, maxBytes);
      chunks.push(value);
      if (!sniffed && total >= SNIFF_BYTES) check();
    }
    if (!sniffed) check();
  } catch (error) {
    void reader.cancel().catch(() => {});
    throw error;
  }
  return chunks;
}

/**
 * The bytes of one Library item as a file. The download route answers with a
 * short-lived signed link, so this asks for a fresh one and then reads the
 * file from it, as the Library page does. The list's size and the response's
 * Content-Length refuse a too-big item before any of it is read.
 */
export async function libraryFile(
  pick: LibraryPick,
  options: {
    kind: string;
    noun: string;
    types?: MediaTypes;
    maxBytes?: number;
    signal: AbortSignal;
  },
): Promise<File> {
  const { kind, noun, signal } = options;
  const maxBytes = options.maxBytes ?? MAX_MEDIA_BYTES;
  if (pick.size_bytes && pick.size_bytes > maxBytes) {
    throw new TooBigError(noun, pick.size_bytes, maxBytes);
  }
  const link = await networkFetch(
    `/me/api/media/${encodeURIComponent(pick.asset_id)}/download`,
    { credentials: "same-origin", signal },
  );
  if (!link.ok) throw new Error("Library download failed");
  const { download_url: url } = (await link.json()) as {
    download_url?: string | null;
  };
  if (!url) throw new Error("Library download has no link");
  const response = await networkFetch(url, { signal });
  if (!response.ok) throw new Error("Library download failed");
  // An error page must not become a picture: the type is the media's own, or
  // the gateway's declared type when the file comes back as plain bytes. The
  // bytes themselves are checked as they arrive.
  const header = response.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  const declared = pick.media_type?.toLowerCase();
  const type = header?.startsWith(`${kind}/`)
    ? header
    : (!header || header === "application/octet-stream") &&
        declared?.startsWith(`${kind}/`)
      ? declared
      : null;
  if (!type || !fileFitsTypes({ type }, options.types)) {
    throw new Error("Library download is not media");
  }
  const length = Number(response.headers.get("content-length"));
  if (length > maxBytes) {
    void response.body?.cancel().catch(() => {});
    throw new TooBigError(noun, length, maxBytes);
  }
  const chunks = await readMedia(response, kind, noun, length || null, maxBytes, signal);
  const extension = SUBTYPE.exec(type)?.[1] ?? "bin";
  return new File(chunks as BlobPart[], `library-${noun.replace(/\s+/g, "-")}.${extension}`, {
    type,
  });
}

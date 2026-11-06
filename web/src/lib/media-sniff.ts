import type { MediaKind } from "./workflow-media-input";

/** Bytes needed to tell every container below apart. */
export const SNIFF_BYTES = 12;

const ascii = (bytes: Uint8Array, start: number, text: string) =>
  [...text].every((char, index) => bytes[start + index] === char.charCodeAt(0));

const startsWith = (bytes: Uint8Array, prefix: number[]) =>
  prefix.every((byte, index) => bytes[index] === byte);

// ISO base media brands that hold a still picture or sound; any other brand
// (isom, mp42, qt, 3gp…) is a video.
const IMAGE_BRANDS = /^(avif|avis|heic|heix|heim|heis|hevc|hevx|heif|mif1|msf1)$/;
const AUDIO_BRANDS = /^(M4A |M4B |F4A |F4B )$/;

/**
 * Which kinds of media a file's leading bytes can be, by container signature
 * alone. Empty when the bytes are not a known image, video or audio container,
 * so a header that says "image/png" over an error page or another kind of file
 * does not pass. Matroska/WebM and Ogg carry sound or pictures, so they answer
 * for both.
 */
export function sniffKinds(bytes: Uint8Array): MediaKind[] {
  if (startsWith(bytes, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return ["image"];
  if (startsWith(bytes, [0xff, 0xd8, 0xff])) return ["image"];
  if (ascii(bytes, 0, "GIF87a") || ascii(bytes, 0, "GIF89a")) return ["image"];
  if (ascii(bytes, 0, "RIFF")) {
    if (ascii(bytes, 8, "WEBP")) return ["image"];
    if (ascii(bytes, 8, "WAVE")) return ["audio"];
    if (ascii(bytes, 8, "AVI ")) return ["video"];
    return [];
  }
  if (ascii(bytes, 4, "ftyp")) {
    const brand = String.fromCharCode(...bytes.subarray(8, 12));
    if (IMAGE_BRANDS.test(brand)) return ["image"];
    if (AUDIO_BRANDS.test(brand)) return ["audio"];
    return ["video"];
  }
  if (startsWith(bytes, [0x1a, 0x45, 0xdf, 0xa3])) return ["video", "audio"];
  if (ascii(bytes, 0, "OggS")) return ["audio", "video"];
  if (ascii(bytes, 0, "fLaC")) return ["audio"];
  if (ascii(bytes, 0, "ID3")) return ["audio"];
  // An MPEG audio or AAC frame: eleven set sync bits.
  if (bytes[0] === 0xff && (bytes[1] & 0xe0) === 0xe0) return ["audio"];
  return [];
}

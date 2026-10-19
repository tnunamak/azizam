import { expect, test } from "vitest";
import { sniffKinds } from "@/lib/media-sniff";

const text = (value: string) => [...value].map((char) => char.charCodeAt(0));
const pad = (bytes: number[]) => new Uint8Array([...bytes, ...new Array(12).fill(0)].slice(0, Math.max(12, bytes.length)));
const ftyp = (brand: string) => pad([0, 0, 0, 0x18, ...text("ftyp"), ...text(brand)]);
const riff = (form: string) => pad([...text("RIFF"), 1, 2, 3, 4, ...text(form)]);

test.each([
  ["PNG", pad([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), ["image"]],
  ["JPEG", pad([0xff, 0xd8, 0xff, 0xe0]), ["image"]],
  ["GIF87a", pad(text("GIF87a")), ["image"]],
  ["GIF89a", pad(text("GIF89a")), ["image"]],
  ["WebP", riff("WEBP"), ["image"]],
  ["AVIF", ftyp("avif"), ["image"]],
  ["HEIC", ftyp("heic"), ["image"]],
  ["MP4", ftyp("isom"), ["video"]],
  ["MOV", ftyp("qt  "), ["video"]],
  ["M4A", ftyp("M4A "), ["audio"]],
  ["WebM or Matroska", pad([0x1a, 0x45, 0xdf, 0xa3]), ["video", "audio"]],
  ["Ogg", pad(text("OggS")), ["audio", "video"]],
  ["WAV", riff("WAVE"), ["audio"]],
  ["MP3 with ID3", pad(text("ID3")), ["audio"]],
  ["MP3 frame", pad([0xff, 0xfb, 0x90, 0x00]), ["audio"]],
  ["FLAC", pad(text("fLaC")), ["audio"]],
])("%s is told by its leading bytes", (_name, bytes, kinds) => {
  expect(sniffKinds(bytes)).toEqual(kinds);
});

test.each([
  ["an HTML page", pad(text("<!doctype html>"))],
  ["JSON", pad(text('{"asset_id":"x"}'))],
  ["a zip archive", pad([0x50, 0x4b, 0x03, 0x04])],
  ["RIFF of another kind", riff("ABCD")],
  ["no bytes", new Uint8Array()],
])("%s is no media", (_name, bytes) => {
  expect(sniffKinds(bytes)).toEqual([]);
});

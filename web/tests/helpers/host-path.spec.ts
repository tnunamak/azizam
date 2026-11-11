import { describe, expect, it } from "vitest";

import { GGUF, HOST_PATH } from "./host-path";

describe("HOST_PATH", () => {
  it.each([
    GGUF,
    "/home/alice/models/a.gguf",
    "/home/alice2/models/a.gguf",
    "/home/alice-work/models/a.gguf",
    "/Users/alice/models/a.gguf",
    "/mnt/models/a.gguf",
    "loaded from /media/disk/a.gguf",
    "path=/mnt/data/a.gguf",
    "model [/mnt/data/a.gguf",
    "model (/media/disk/a.gguf",
    "`/home/alice/models/a.gguf`",
    "model </home/alice/models/a.gguf>",
    "/home/alice",
    "/Users/alice",
    "/mnt/model.gguf",
    "/media/clip.mp4",
  ])("finds a host path in %s", (text) => {
    expect(text).toMatch(HOST_PATH);
  });

  it.each([
    "Qwen3.8 27B",
    "/me/media/abc123/",
    "https://example.com/media/clip/",
    "https://example.com/redirect?next=/media/clip.mp4",
    "https://example.com/home/alice/models/a.gguf",
    "https://example.com/Users/alice/models/a.gguf",
    "https://example.com/mnt/data/a.gguf",
    "/v1/images/generations",
    "home/alice",
  ])("leaves %s alone", (text) => {
    expect(text).not.toMatch(HOST_PATH);
  });
});

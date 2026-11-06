"use client";

import { useEffect, useState } from "react";
import { networkFetch } from "@/lib/network-error";

/**
 * The real loudness shape of an audio file, for its cover. Peaks are measured
 * from the decoded file, never invented: without Web Audio, or when decoding
 * fails, the result stays null and the cover draws no waveform.
 */
const cache = new Map<string, number[] | null>();

/**
 * A tile measures its clip only when the file is small. Measuring downloads and
 * decodes the whole file, which for a five-minute track is several megabytes per
 * tile; those tiles keep the glyph, and the viewer still measures them. A file
 * of unknown size is measured, as before.
 */
export const TILE_PEAKS_MAX_BYTES = 1_500_000;
let queue: Promise<unknown> = Promise.resolve();

async function measure(src: string, bins: number): Promise<number[] | null> {
  const AudioCtx =
    typeof window === "undefined"
      ? undefined
      : (window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext);
  if (!AudioCtx) return null;
  const response = await networkFetch(src);
  if (!response.ok) return null;
  const buffer = await response.arrayBuffer();
  const context = new AudioCtx();
  try {
    const audio = await context.decodeAudioData(buffer);
    const data = audio.getChannelData(0);
    const size = Math.max(1, Math.floor(data.length / bins));
    const peaks: number[] = [];
    for (let bin = 0; bin < bins; bin += 1) {
      let peak = 0;
      const end = bin === bins - 1 ? data.length : Math.min(data.length, (bin + 1) * size);
      for (let index = bin * size; index < end; index += 1) {
        const value = Math.abs(data[index]);
        if (value > peak) peak = value;
      }
      peaks.push(peak);
    }
    const loudest = Math.max(...peaks);
    return loudest > 0 ? peaks.map((peak) => Math.sqrt(peak / loudest)) : null;
  } finally {
    void context.close();
  }
}

export function useAudioPeaks(
  key: string,
  src: string | null | undefined,
  enabled: boolean,
  bins = 40,
  sizeBytes?: number | null,
): number[] | null {
  const [peaks, setPeaks] = useState<number[] | null>(
    () => cache.get(key) ?? null,
  );
  useEffect(() => {
    if (!enabled || !src) return;
    if (sizeBytes != null && sizeBytes > TILE_PEAKS_MAX_BYTES) return;
    if (typeof window === "undefined" || !("AudioContext" in window)) return;
    if (cache.has(key)) {
      setPeaks(cache.get(key) ?? null);
      return;
    }
    let cancelled = false;
    // One decode at a time keeps a long grid from stalling the page.
    const job = queue.then(() =>
      measure(src, bins).catch(() => null),
    );
    queue = job;
    void job.then((result) => {
      cache.set(key, result);
      if (!cancelled) setPeaks(result);
    });
    return () => {
      cancelled = true;
    };
  }, [key, src, enabled, bins, sizeBytes]);
  return peaks;
}

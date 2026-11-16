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

/** Pure waveform envelope: proportional buckets, with every channel contributing. */
export function peaksFrom(channels: Float32Array[], bins: number): number[] | null {
  const length = channels[0]?.length ?? 0;
  if (!length || !Number.isSafeInteger(bins) || bins <= 0) return null;
  const peaks: number[] = [];
  let loudest = 0;
  for (let bin = 0; bin < bins; bin += 1) {
    const start = Math.floor(bin * length / bins);
    const end = Math.max(start + 1, Math.floor((bin + 1) * length / bins));
    let peak = 0;
    for (const channel of channels) {
      for (let index = start; index < end; index += 1) {
        const value = Math.abs(channel[index]);
        if (value > peak) peak = value;
      }
    }
    peaks.push(peak);
    if (peak > loudest) loudest = peak;
  }
  return loudest > 0 ? peaks.map((peak) => Math.sqrt(peak / loudest)) : null;
}

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
    return peaksFrom(
      Array.from({ length: audio.numberOfChannels }, (_, channel) =>
        audio.getChannelData(channel),
      ),
      bins,
    );
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
  const cacheKey = JSON.stringify([key, bins]);
  const [peaks, setPeaks] = useState<{
    cacheKey: string;
    value: number[] | null;
  }>(() => ({ cacheKey, value: cache.get(cacheKey) ?? null }));
  useEffect(() => {
    setPeaks({ cacheKey, value: cache.get(cacheKey) ?? null });
    if (!enabled || !src) return;
    if (sizeBytes != null && sizeBytes > TILE_PEAKS_MAX_BYTES) return;
    if (typeof window === "undefined" || !("AudioContext" in window)) return;
    if (cache.has(cacheKey)) {
      setPeaks({ cacheKey, value: cache.get(cacheKey) ?? null });
      return;
    }
    let cancelled = false;
    // One decode at a time keeps a long grid from stalling the page.
    const job = queue.then(() => {
      if (cancelled) return undefined;
      return measure(src, bins).catch(() => null);
    });
    queue = job;
    void job.then((result) => {
      if (cancelled || result === undefined) return;
      cache.set(cacheKey, result);
      setPeaks({ cacheKey, value: result });
    });
    return () => {
      cancelled = true;
    };
  }, [cacheKey, src, enabled, bins, sizeBytes]);
  return peaks.cacheKey === cacheKey ? peaks.value : cache.get(cacheKey) ?? null;
}

import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { TILE_PEAKS_MAX_BYTES, useAudioPeaks } from '@/lib/audio-peaks';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubAudio() {
  class FakeAudioContext {
    decodeAudioData = vi.fn(async () => ({ getChannelData: () => new Float32Array([0.5, 0.25, 1, 0]) }));
    close = vi.fn(async () => {});
  }
  vi.stubGlobal('AudioContext', FakeAudioContext);
  const fetchMock = vi.fn(async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(1) }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

it('does not download a file larger than the tile limit to draw its waveform', async () => {
  const fetchMock = stubAudio();
  const { result } = renderHook(() =>
    useAudioPeaks('fixture-large', '/fixture/large.mp3', true, 2, TILE_PEAKS_MAX_BYTES + 1),
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(fetchMock).not.toHaveBeenCalled();
  expect(result.current).toBeNull();
});

it('measures a small file, and a file of unknown size', async () => {
  const fetchMock = stubAudio();
  const small = renderHook(() => useAudioPeaks('fixture-small', '/fixture/small.mp3', true, 2, 1000));
  await waitFor(() => expect(small.result.current).not.toBeNull());
  const unknown = renderHook(() => useAudioPeaks('fixture-unknown', '/fixture/unknown.mp3', true, 2));
  await waitFor(() => expect(unknown.result.current).not.toBeNull());
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

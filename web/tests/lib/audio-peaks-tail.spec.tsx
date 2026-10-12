import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useAudioPeaks } from '@/lib/audio-peaks';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('includes the final samples when audio length is not divisible by bin count', async () => {
  const close = vi.fn(async () => {});
  const decode = vi.fn(async () => ({
    getChannelData: () => new Float32Array([0, 0, 0, 0, 1]),
  }));
  class FakeAudioContext {
    decodeAudioData = decode;
    close = close;
  }
  vi.stubGlobal('AudioContext', FakeAudioContext);
  const fetchMock = vi.fn(async () => ({
    ok: true, arrayBuffer: async () => new ArrayBuffer(1),
  }));
  vi.stubGlobal('fetch', fetchMock);
  const { result } = renderHook(() =>
    useAudioPeaks('fixture-tail-five-samples', '/fixture/tail.wav', true, 2),
  );
  await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(result.current).toEqual([0, 1]));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(decode).toHaveBeenCalledTimes(1);
});


import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useAudioPeaks } from '@/lib/audio-peaks';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('closes the decoder and keeps no invented waveform after decoding fails', async () => {
  const close = vi.fn(async () => {});
  class FailingAudioContext {
    decodeAudioData = vi.fn(async () => { throw new Error('fixture bad media'); });
    close = close;
  }
  vi.stubGlobal('AudioContext', FailingAudioContext);
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, arrayBuffer: async () => new ArrayBuffer(1),
  })));
  const { result } = renderHook(() =>
    useAudioPeaks('fixture-decode-failure', '/fixture/bad.wav', true, 2),
  );
  await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
  expect(result.current).toBeNull();
});

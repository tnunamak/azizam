import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { peaksFrom, useAudioPeaks } from "./audio-peaks";

describe("peaksFrom", () => {
  it("measures a mono impulse", () => {
    const data = new Float32Array(8);
    data[3] = -1;
    expect(peaksFrom([data], 4)).toEqual([0, 1, 0, 0]);
  });

  it("gives identical stereo the same peaks as mono", () => {
    const data = new Float32Array([0, -1, 0.25, 0, 0, 0, 0.5, 0]);
    expect(peaksFrom([data, data.slice()], 4)).toEqual(peaksFrom([data], 4));
  });

  it("measures right-only stereo", () => {
    const right = new Float32Array(8);
    right[3] = -1;
    expect(peaksFrom([new Float32Array(8), right], 4)).toEqual([0, 1, 0, 0]);
  });

  it("places sample 40 of 79 in bar 20 of 40", () => {
    const data = new Float32Array(79);
    data[40] = 1;
    const expected = Array<number>(40).fill(0);
    expected[20] = 1;
    expect(peaksFrom([data], 40)).toEqual(expected);
  });

  it("fills all 40 bars from 10 samples", () => {
    const data = new Float32Array([0, 0.25, 0, 1, 0, 0, 0, 0, 0, 0]);
    expect(peaksFrom([data], 40)).toEqual([
      0, 0, 0, 0, 0.5, 0.5, 0.5, 0.5, 0, 0, 0, 0,
      1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
      0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ]);
  });

  it("returns null for silence", () => {
    expect(peaksFrom([new Float32Array(79), new Float32Array(79)], 40)).toBeNull();
    expect(peaksFrom([], 40)).toBeNull();
    expect(peaksFrom([new Float32Array(0)], 40)).toBeNull();
  });
});

describe("useAudioPeaks", () => {
  it("returns only the current key and bin count on the first render", async () => {
    let resolveA!: (value: AudioBuffer) => void;
    let resolveOther!: (value: AudioBuffer) => void;
    const firstDecode = new Promise<AudioBuffer>((resolve) => { resolveA = resolve; });
    const otherDecode = new Promise<AudioBuffer>((resolve) => { resolveOther = resolve; });
    const samples = new Float32Array([0, -1, 0, 0]);
    const audio = {
      numberOfChannels: 1,
      getChannelData: () => samples,
    } as unknown as AudioBuffer;
    const decode = vi.fn()
      .mockImplementationOnce(() => firstDecode)
      .mockImplementation(() => otherDecode);
    const close = vi.fn().mockResolvedValue(undefined);
    const audioContext = Object.getOwnPropertyDescriptor(window, "AudioContext");
    const fetchDescriptor = Object.getOwnPropertyDescriptor(globalThis, "fetch");
    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: class {
        decodeAudioData = decode;
        close = close;
      },
    });
    Object.defineProperty(globalThis, "fetch", {
      configurable: true,
      value: vi.fn().mockResolvedValue({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(0),
      }),
    });
    const renders: Array<number[] | null> = [];
    let unmount: (() => void) | undefined;
    try {
      const hook = renderHook(({ key, bins }) => {
        const value = useAudioPeaks(key, "/synthetic-wave.wav", true, bins);
        // Capture inside render: reading result.current after rerender would hide
        // an incorrect first value repaired by the passive effect.
        renders.push(value);
        return value;
      }, { initialProps: { key: "first-render-clip-a", bins: 2 } });
      unmount = hook.unmount;
      await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
      expect(hook.result.current).toBeNull();
      await act(async () => { resolveA(audio); await firstDecode; });
      await waitFor(() => expect(hook.result.current).toEqual([1, 0]));
      const a = hook.result.current;

      let first = renders.length;
      hook.rerender({ key: "first-render-clip-b", bins: 2 });
      expect(renders[first]).toBeNull();
      await waitFor(() => expect(decode).toHaveBeenCalledTimes(2));

      first = renders.length;
      hook.rerender({ key: "first-render-clip-a", bins: 2 });
      expect(renders[first]).toEqual([1, 0]);
      expect(renders[first]).toBe(a);

      first = renders.length;
      hook.rerender({ key: "first-render-clip-a", bins: 4 });
      expect(renders[first]).toBeNull();

      first = renders.length;
      hook.rerender({ key: "first-render-clip-a", bins: 2 });
      expect(renders[first]).toBe(a);
    } finally {
      unmount?.();
      await act(async () => {
        resolveA(audio);
        resolveOther(audio);
        await Promise.all([firstDecode, otherDecode]);
      });
      if (audioContext) Object.defineProperty(window, "AudioContext", audioContext);
      else Reflect.deleteProperty(window, "AudioContext");
      if (fetchDescriptor) Object.defineProperty(globalThis, "fetch", fetchDescriptor);
      else Reflect.deleteProperty(globalThis, "fetch");
    }
  });
});

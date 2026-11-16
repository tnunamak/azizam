import { describe, expect, it } from "vitest";
import { peaksFrom } from "./audio-peaks";

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

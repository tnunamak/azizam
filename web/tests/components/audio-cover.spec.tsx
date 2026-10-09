import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  AudioCover,
  audioKind,
  coverWords,
  Waveform,
} from "@/components/product/audio-cover";

describe("audio covers", () => {
  it("name the kind of sound from the model", () => {
    expect(audioKind("tts-1").label).toBe("Voice");
    expect(audioKind("ace-step-xl").label).toBe("Music");
    expect(audioKind("stable-audio-sfx").label).toBe("Sound effect");
    expect(audioKind(null).label).toBe("Audio");
  });
  it("show whole sentences, else whole words, never a cut word", () => {
    const tart =
      "For the apple tart, slice the apples thinly and bake until the pastry is golden. Let it cool before serving.";
    expect(coverWords(tart, 84)).toBe(
      "For the apple tart, slice the apples thinly and bake until the pastry is golden.",
    );
    expect(coverWords("The tomatoes are ready.", 84)).toBe(
      "The tomatoes are ready.",
    );
    expect(coverWords(tart, 40)).toBe("For the apple tart, slice the apples…");
    // Never ends on a joining word.
    expect(
      coverWords(
        "On Saturday we will take the train to the coast, walk by the harbor, and have a picnic.",
        80,
      ),
    ).toBe(
      "On Saturday we will take the train to the coast, walk by the harbor…",
    );
  });
  it("draw no bars without measured peaks", () => {
    const { container } = render(<Waveform peaks={null} />);
    expect(container.querySelector("svg")).toBeNull();
  });
  it("draw one bar per measured peak and mark the played part", () => {
    const { container } = render(
      <Waveform peaks={[0.2, 1, 0.5, 0.1]} progress={0.5} />,
    );
    const bars = container.querySelectorAll("rect");
    expect(bars).toHaveLength(4);
    expect(bars[0].getAttribute("class")).toContain("opacity-90");
    expect(bars[3].getAttribute("class")).toContain("opacity-40");
  });
  it("show the clip's words, kind and length, and no waveform without Web Audio", () => {
    const { getByText, container } = render(
      <AudioCover
        peaksKey="a"
        src="/a.mp3"
        visible
        maker="Robin"
        model="tts-1"
        text="The tomatoes are ready to pick."
        duration="0:04"
        control={<button type="button">Play</button>}
      />,
    );
    expect(getByText("The tomatoes are ready to pick.")).toBeVisible();
    expect(getByText("Voice")).toBeVisible();
    expect(getByText("0:04")).toBeVisible();
    // jsdom has no AudioContext: nothing is invented.
    expect(container.querySelector("svg rect")).toBeNull();
  });
});

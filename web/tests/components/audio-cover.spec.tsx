import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  AudioFace,
  audioKind,
  MediaBadge,
  Waveform,
} from "@/components/product/audio-cover";
import { Play } from "lucide-react";

describe("audio faces", () => {
  it("name the kind of sound from the model", () => {
    expect(audioKind("tts-1").label).toBe("Voice");
    expect(audioKind("ace-step-xl").label).toBe("Music");
    expect(audioKind("stable-audio-sfx").label).toBe("Sound effect");
    expect(audioKind(null).label).toBe("Audio");
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
  it("draw one neutral face in no person's colour, and no waveform without Web Audio", () => {
    const { container } = render(
      <AudioFace peaksKey="a" src="/a.mp3" visible />,
    );
    // D1.5: one neutral family; no words, kind or maker on the face.
    expect(container.innerHTML).not.toContain("person");
    expect(container.textContent).toBe("");
    // jsdom has no AudioContext: nothing is invented.
    expect(container.querySelector("svg rect")).toBeNull();
  });
  it("put the play control in the middle of the face", () => {
    const { getByRole } = render(
      <AudioFace
        peaksKey="b"
        visible={false}
        center={<button type="button">Play clip</button>}
      />,
    );
    expect(getByRole("button", { name: "Play clip" })).toBeVisible();
  });
  it("show the type glyph and length, or the glyph alone", () => {
    const { container, getByText } = render(
      <>
        <MediaBadge icon={Play}>0:04</MediaBadge>
        <MediaBadge icon={Play} />
      </>,
    );
    expect(getByText("0:04")).toBeVisible();
    const glyphOnly = container.children[1];
    expect(glyphOnly.className).toContain("w-6");
    expect(glyphOnly.textContent).toBe("");
  });
});

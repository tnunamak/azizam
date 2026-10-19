import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { LongAudioSection } from "./record-sections";

afterEach(cleanup);

const saved = {
  enabled: true,
  cut_files_longer_than_seconds: 40,
  target_seconds: 30,
  search_seconds: 2,
  max_segment_seconds: 40,
  native_long_audio: false,
  from_bundled_defaults: [] as string[],
  note: "All values are saved on this engine.",
};

describe("Long recordings", () => {
  it("shows nothing for an engine with no cutting settings", () => {
    const { container } = render(<LongAudioSection chunking={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists the numbers in force", () => {
    render(<LongAudioSection chunking={saved} />);
    expect(screen.getByRole("heading", { level: 2, name: "Long recordings" })).toBeVisible();
    expect(screen.getByText("Longest piece sent to the engine").nextElementSibling).toHaveTextContent("40 s");
    expect(screen.getByText("Distance between cuts").nextElementSibling).toHaveTextContent("30 s");
    expect(screen.queryByText(/not saved on this engine/)).toBeNull();
    expect(screen.getByText("All values are saved on this engine.")).toBeVisible();
  });

  it("says which numbers are defaults that this engine does not save", () => {
    render(<LongAudioSection chunking={{ ...saved, from_bundled_defaults: ["max_segment_seconds", "threshold_seconds"], note: "Some come from the bundled defaults." }} />);
    expect(screen.getByText("Longest piece sent to the engine").nextElementSibling).toHaveTextContent("default, not saved on this engine");
    expect(screen.getByText("Cuts recordings longer than").nextElementSibling).toHaveTextContent("default, not saved on this engine");
    expect(screen.getByText("Distance between cuts").nextElementSibling).not.toHaveTextContent("default");
  });

  it("explains the hard maximum when cutting is off, and shows invalid settings", () => {
    const { rerender } = render(<LongAudioSection chunking={{ ...saved, enabled: false }} />);
    expect(screen.getByText(/refuses audio over the hard maximum/)).toBeVisible();
    rerender(<LongAudioSection chunking={{ invalid: "STT target_seconds must be a number" }} />);
    expect(screen.getByText(/These settings are not valid: STT target_seconds must be a number/)).toBeVisible();
    vi.restoreAllMocks();
  });
});

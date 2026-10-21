import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { CapabilityList, type CapabilityRow } from "./capabilities";

afterEach(() => cleanup());

const rows: CapabilityRow[] = [
  { id: "chat", label: "Chat", level: "likely", evidence: "The server lists chat models." },
  { id: "embeddings", label: "Embeddings", level: "possible", evidence: "It answers on the embeddings path.", why: "Nothing proves it yet." },
  { id: "images", label: "Images", level: "none", evidence: "The server did not mention images.", why: "No image models were listed." },
  { id: "speech", label: "Speech to text", level: "possible", evidence: "It answers on the transcription path." },
];

function mount(ticked: string[]) {
  return render(<CapabilityList rows={rows} ticked={new Set(ticked)} onTick={vi.fn()} tests={{}} testNote="note" />);
}

describe("capability checkboxes are described by their evidence", () => {
  it("a Likely row: the level and the evidence", () => {
    mount(["chat"]);
    expect(screen.getByRole("checkbox", { name: "Chat" })).toHaveAccessibleDescription("Likely The server lists chat models.");
  });

  it("a Possible row left unticked: the level, the evidence and why it is unticked", () => {
    mount([]);
    expect(screen.getByRole("checkbox", { name: "Embeddings" })).toHaveAccessibleDescription(
      "Possible It answers on the embeddings path. Why unticked: Nothing proves it yet.",
    );
  });

  it("an unticked row that was not found: the level, the evidence and the reason", () => {
    mount([]);
    expect(screen.getByRole("checkbox", { name: "Images" })).toHaveAccessibleDescription(
      "Not found The server did not mention images. Why unticked: No image models were listed.",
    );
  });

  it("a row ticked by hand: the level, the evidence and the warning, with no unticked reason", () => {
    mount(["speech", "embeddings"]);
    expect(screen.getByRole("checkbox", { name: "Speech to text" })).toHaveAccessibleDescription(
      "Possible It answers on the transcription path. Not proven yet. Run a real test to be sure.",
    );
    expect(screen.getByRole("checkbox", { name: "Embeddings" })).not.toHaveAccessibleDescription(expect.stringContaining("Why unticked"));
  });

  it("ids are unique per row, so two lists on one page do not clash", () => {
    mount(["chat"]);
    const ids = [...document.querySelectorAll("[aria-describedby]")].flatMap((el) => (el.getAttribute("aria-describedby") ?? "").split(" "));
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) expect(document.getElementById(id)).not.toBeNull();
  });
});

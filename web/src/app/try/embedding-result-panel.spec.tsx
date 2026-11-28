import * as React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EmbeddingResultPanel } from "@/app/try/embedding-result-panel";

const numbers = Array.from({ length: 12 }, (_, index) => index / 10);
const payload = { model: "example-model", data: [{ embedding: numbers }, { embedding: numbers }] };
const json = JSON.stringify(numbers);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("EmbeddingResultPanel", () => {
  it("renders the explanation, three facts and eight-number preview with four more", () => {
    const { container } = render(<EmbeddingResultPanel payload={payload} />);
    // The Try result card carries the heading; the panel adds none of its own.
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.getByText("Apps compare these numbers to find similar text or to search documents.")).toBeTruthy();
    expect([...container.querySelectorAll("dt")].map(node => node.textContent)).toEqual(["Model", "Inputs", "Numbers in the first result"]);
    expect([...container.querySelectorAll("dd")].map(node => node.textContent)).toEqual(["example-model", "2", "12"]);
    expect(screen.getByText("Preview, Copy and Download use only the first result.")).toBeTruthy();
    expect(screen.getByText("0.0000, 0.1000, 0.2000, 0.3000, 0.4000, 0.5000, 0.6000, 0.7000 and 4 more")).toBeTruthy();
  });

  it("shows Not reported for a missing model and omits a zero remainder", () => {
    render(<EmbeddingResultPanel payload={{ data: [{ embedding: [1.25] }] }} />);
    expect(screen.getByText("Not reported")).toBeTruthy();
    expect(screen.getByText("1.2500")).toBeTruthy();
    expect(screen.queryByText(/and 0 more/)).toBeNull();
    expect(screen.getByText("Numbers per input")).toBeTruthy();
    expect(screen.queryByText(/use only the first result/)).toBeNull();
  });

  it("renders only the empty sentence for a null summary", () => {
    render(<EmbeddingResultPanel payload={null} />);
    expect(screen.getByText("No numbers came back.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("copies the helper JSON through the shared copy button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const previous = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    try {
      render(<EmbeddingResultPanel payload={payload} />);
      fireEvent.click(screen.getByRole("button", { name: "Copy" }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith(json));
      expect(writeText).toHaveBeenCalledTimes(1);
    } finally {
      if (previous) Object.defineProperty(navigator, "clipboard", previous);
      else Reflect.deleteProperty(navigator, "clipboard");
    }
  });

  it("downloads exact JSON in a Blob and removes the anchor before delayed URL revocation", async () => {
    vi.useFakeTimers();
    const createObjectURL = vi.fn((blob: Blob | MediaSource) => (blob ? "blob:embedding-test" : ""));
    const revokeObjectURL = vi.fn();
    const NativeURL = URL;
    vi.stubGlobal("URL", class extends NativeURL {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = revokeObjectURL;
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.isConnected).toBe(true);
      expect(this.download).toBe("embedding.json");
      expect(this.getAttribute("href")).toBe("blob:embedding-test");
    });
    render(<EmbeddingResultPanel payload={payload} />);
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0]?.[0] as unknown as Blob;
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe("application/json");
    expect(click).toHaveBeenCalledTimes(1);
    const anchor = click.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.isConnected).toBe(false);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    vi.runAllTimers();
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:embedding-test");
    vi.useRealTimers();
    const text = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(blob);
    });
    expect(text).toBe(json);
  });
});

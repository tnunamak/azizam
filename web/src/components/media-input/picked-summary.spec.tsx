import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PickedSummary } from "./picked-summary";

afterEach(cleanup);

const props = {
  name: "picture.png",
  facts: ["PNG", "19 KB"],
  kind: "image",
  previewUrl: "blob:first-picture",
  previewAlt: "Picked picture",
  retryLabel: "Retry picture preview",
  onRemove: vi.fn(),
};

function loadImage(width: number, height: number) {
  const image = screen.getByRole("img", { name: props.previewAlt });
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(image);
}

describe("PickedSummary image dimensions", () => {
  it("shows the pixel size after the type only once the image loads", () => {
    render(<PickedSummary {...props} />);

    expect(screen.getByText("PNG · 19 KB")).toBeTruthy();
    expect(screen.queryByText("PNG · 1024×1024 · 19 KB")).toBeNull();

    loadImage(1024, 1024);

    expect(screen.getByText("PNG · 1024×1024 · 19 KB")).toBeTruthy();
    expect(screen.queryByText("PNG · 19 KB")).toBeNull();
  });

  it("clears the old pixel size when the preview URL changes", () => {
    const { rerender } = render(<PickedSummary {...props} />);
    loadImage(1024, 1024);
    expect(screen.getByText("PNG · 1024×1024 · 19 KB")).toBeTruthy();

    rerender(<PickedSummary {...props} previewUrl="blob:second-picture" />);

    expect(screen.getByText("PNG · 19 KB")).toBeTruthy();
    expect(screen.queryByText("PNG · 1024×1024 · 19 KB")).toBeNull();

    loadImage(640, 480);

    expect(screen.getByText("PNG · 640×480 · 19 KB")).toBeTruthy();
  });

  it("hides the size after an error and until a retry loads", () => {
    render(<PickedSummary {...props} />);
    loadImage(1024, 1024);
    expect(screen.getByText("PNG · 1024×1024 · 19 KB")).toBeTruthy();

    fireEvent.error(screen.getByRole("img", { name: props.previewAlt }));

    expect(screen.getByText("PNG · 19 KB")).toBeTruthy();
    expect(screen.queryByText("PNG · 1024×1024 · 19 KB")).toBeNull();
    expect(screen.getByText("Preview unavailable")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: props.retryLabel }));

    expect(screen.getByText("PNG · 19 KB")).toBeTruthy();
    expect(screen.queryByText("PNG · 1024×1024 · 19 KB")).toBeNull();

    loadImage(1024, 1024);

    expect(screen.getByText("PNG · 1024×1024 · 19 KB")).toBeTruthy();
  });

  it("keeps video and other kinds free of image dimensions", () => {
    const { rerender } = render(<PickedSummary {...props} />);
    loadImage(1024, 1024);
    expect(screen.getByText("PNG · 1024×1024 · 19 KB")).toBeTruthy();

    rerender(<PickedSummary {...props} kind="video" facts={["MP4", "19 KB"]} />);
    expect(screen.getByText("MP4 · 19 KB")).toBeTruthy();
    expect(screen.queryByText(/1024×1024/)).toBeNull();

    rerender(<PickedSummary {...props} kind="audio" facts={["MP3", "19 KB"]} />);
    expect(screen.getByText("MP3 · 19 KB")).toBeTruthy();
    expect(screen.queryByText(/1024×1024/)).toBeNull();
  });
});

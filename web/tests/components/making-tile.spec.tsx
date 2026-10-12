import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MakingTile } from "@/components/product/making-tile";

afterEach(cleanup);

describe("running creation tile", () => {
  it("keeps its words visible on a phone", () => {
    render(<MakingTile kind="video" startedAt={Math.floor(Date.now() / 1000) - 60} />);
    const words = screen.getByText("Making your video");
    expect(words.className).not.toContain("sr-only");
  });
  it("keeps the may-not-finish warning visible on a phone, with the reason for wider screens", () => {
    render(<MakingTile kind="image" risk="Images paused: it may not finish" />);
    const short = screen.getByText("May not finish");
    expect(short.className).not.toContain("sr-only");
    expect(screen.getByText("Images paused: it may not finish")).toBeInTheDocument();
  });
  it("shows no warning without a risk", () => {
    render(<MakingTile kind="audio" />);
    expect(screen.queryByText(/not finish/i)).toBeNull();
  });
});

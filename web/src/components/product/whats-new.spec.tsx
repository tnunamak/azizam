import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { WhatsNew } from "./whats-new";

const entries = [
  { date: "2026-11-23", title: "Newest update", detail: "Newest detail." },
  { date: "2026-11-22", title: "Second update", detail: "Second detail." },
  { date: "2026-11-21", title: "Third update", detail: "Third detail." },
  { date: "2026-11-20", title: "Fourth update", detail: "Fourth detail." },
];

describe("What's new", () => {
  it("shows a named list of the newest three entries with titles, details and calendar dates", () => {
    render(<WhatsNew entries={entries} />);

    expect(screen.getByRole("heading", { level: 2, name: "What's new" })).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "What's new" });
    expect(within(list).getAllByRole("heading").map((heading) => heading.textContent)).toEqual([
      "Newest update",
      "Second update",
      "Third update",
    ]);
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getByText("Newest detail.")).toBeInTheDocument();
    expect(within(list).getByText("Nov 23")).toHaveAttribute("datetime", "2026-11-23");
    expect(screen.queryByText("Fourth update")).not.toBeInTheDocument();
  });

  it("expands and collapses the same list and reflects the state on the button", async () => {
    const user = userEvent.setup();
    render(<WhatsNew entries={entries} />);
    const button = screen.getByRole("button", { name: "Show all (4)" });
    expect(button).toHaveAttribute("aria-expanded", "false");

    await user.click(button);
    expect(screen.getByRole("heading", { name: "Fourth update" })).toBeInTheDocument();
    expect(button).toHaveAccessibleName("Show fewer");
    expect(button).toHaveAttribute("aria-expanded", "true");

    await user.click(button);
    expect(screen.queryByText("Fourth update")).not.toBeInTheDocument();
    expect(button).toHaveAccessibleName("Show all (4)");
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("renders nothing when no entries exist", () => {
    const { container } = render(<WhatsNew entries={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

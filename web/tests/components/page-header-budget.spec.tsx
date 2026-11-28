import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { PageHeader } from "@/components/product/page-header";

afterEach(cleanup);

describe("PageHeader attention budget", () => {
  it("shows a short subtitle under the page heading", () => {
    render(
      <PageHeader title="Models" subtitle="Choose a model for your app." />,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "Models" }),
    ).toBeVisible();
    expect(screen.getByText("Choose a model for your app.")).toBeVisible();
    expect(screen.queryByText("About this page")).toBeNull();
  });

  it("omits a null subtitle", () => {
    render(
      <PageHeader title="Usage" subtitle={null} answer="Legacy guidance." />,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "Usage" }),
    ).toBeVisible();
    expect(screen.queryByText("Legacy guidance.")).toBeNull();
    expect(screen.queryByText("About this page")).toBeNull();
  });

  it("keeps a long legacy subtitle intact in a collapsed disclosure", () => {
    const copy =
      "Review request volume and compare spend with the previous period before changing access plans.";
    render(<PageHeader title="Usage" answer={copy} />);

    const disclosure = screen.getByText("About this page").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    expect(disclosure).toHaveTextContent(copy);
  });

  it("omits a subtitle that repeats the title", () => {
    render(<PageHeader title="Models" subtitle="Models." />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Models" }),
    ).toBeVisible();
    expect(screen.queryByText("Models.")).toBeNull();
    expect(screen.queryByText("About this page")).toBeNull();
  });

  it.each(["Here's how to use your AI.", "Here’s how to use your AI."])(
    "omits a title-restating how-to subtitle: %s",
    (subtitle) => {
      render(<PageHeader title="Home" answer={subtitle} />);

      expect(
        screen.getByRole("heading", { level: 1, name: "Home" }),
      ).toBeVisible();
      expect(screen.queryByText(subtitle)).toBeNull();
      expect(screen.queryByText("About this page")).toBeNull();
    },
  );

  it("preserves actions and their keyboard access beside the semantic heading", async () => {
    const user = userEvent.setup();
    render(
      <PageHeader
        title="Providers"
        primaryAction={<button type="button">Add provider</button>}
      />,
    );

    const action = screen.getByRole("button", { name: "Add provider" });
    expect(
      screen.getByRole("heading", { level: 1, name: "Providers" }),
    ).toBeVisible();
    await user.tab();
    expect(action).toHaveFocus();
  });
});

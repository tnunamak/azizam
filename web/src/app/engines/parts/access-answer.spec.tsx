import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { accessAnswer } from "../__fixtures__/test-utils";
import { AccessAnswer } from "./access-answer";

afterEach(() => cleanup());

describe("AccessAnswer", () => {
  it("you and two plans: the sentence, a link per plan, who holds it, and the Access plans link", () => {
    const access = accessAnswer("you_and_family");
    render(<AccessAnswer access={access} />);
    expect(screen.getByRole("heading", { name: "Who can use these models" })).toBeVisible();
    expect(
      screen.getByText("Usable by: you, and the Family plan (allows all chat models) and the Kids plan (lists qwen3-32b)."),
    ).toBeVisible();
    const plans = screen.getByRole("list", { name: "Plans that include these models" });
    const rows = within(plans).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByRole("link", { name: "Family" })).toHaveAttribute("href", expect.stringMatching(/^\/people\/?\?plan=plan_family$/));
    expect(rows[0]).toHaveTextContent("allows all chat models");
    expect(rows[0]).toHaveTextContent("3 people and 1 app");
    expect(within(rows[1]).getByRole("link", { name: "Kids" })).toHaveAttribute("href", expect.stringMatching(/^\/people\/?\?plan=plan_kids$/));
    expect(rows[1]).toHaveTextContent("lists qwen3-32b");
    expect(rows[1]).toHaveTextContent("1 person");
    expect(rows[1]).not.toHaveTextContent("1 person and");
    expect(within(rows[2]).getByRole("link", { name: "Local only" })).toHaveAttribute("href", expect.stringMatching(/^\/people\/?\?plan=plan_local$/));
    expect(rows[2]).toHaveTextContent("1 person");
    expect(screen.getByRole("link", { name: "Access plans" })).toHaveAttribute("href", expect.stringMatching(/^\/people\/?\?tab=plans$/));
  });

  it("says when nobody holds a plan, and counts one app", () => {
    const access = accessAnswer("you_and_family");
    access.plans = [
      { ...access.plans[0], people: 0, apps: 0 },
      { ...access.plans[1], people: 0, apps: 1 },
    ];
    render(<AccessAnswer access={access} />);
    const rows = within(screen.getByRole("list", { name: "Plans that include these models" })).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Nobody holds this plan yet");
    expect(rows[1]).toHaveTextContent("1 app");
    expect(rows[1]).not.toHaveTextContent("Nobody");
  });

  it("some plans, some models left out: the sentence names them once and the list does not repeat them", () => {
    const access = accessAnswer("listed_only");
    render(<AccessAnswer access={access} />);
    expect(screen.getByText(access.sentence)).toBeVisible();
    expect(screen.getAllByText(/gemma-3-12b-it/)).toHaveLength(1);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Access plans" })).toBeVisible();
  });

  it("no plan includes them: the sentence and the link, no list", () => {
    const access = accessAnswer("nobody");
    render(<AccessAnswer access={access} />);
    expect(
      screen.getByText(
        "No access plan includes these yet, so nobody can use them, you included. Add them to a plan in Access plans.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("list")).toBeNull();
    expect(screen.getByRole("link", { name: "Access plans" })).toHaveAttribute("href", expect.stringMatching(/^\/people\/?\?tab=plans$/));
  });

  it("category rules: the sentence carries it", () => {
    const access = accessAnswer("category_rules");
    render(<AccessAnswer access={access} />);
    expect(screen.getByText(/People and apps under category rules can’t use them until you add them to a category\./)).toBeVisible();
  });

  it("says how many plans could not be read", () => {
    const access = accessAnswer("nobody");
    render(<AccessAnswer access={{ ...access, unreadable_plans: 2 }} />);
    expect(screen.getByText("2 access plans could not be read, so they include none of these.")).toBeVisible();
    cleanup();
    render(<AccessAnswer access={{ ...access, unreadable_plans: 1 }} />);
    expect(screen.getByText("1 access plan could not be read, so it includes none of these.")).toBeVisible();
  });

  it("is a computed answer: it has no checkbox", () => {
    render(<AccessAnswer access={accessAnswer("you_and_family")} />);
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("loading: says it is checking, with no sentence", () => {
    render(<AccessAnswer loading />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking the access plans…");
    expect(screen.getByRole("heading", { name: "Who can use these models" })).toBeVisible();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("error: says it could not read the plans and offers Try again", async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    render(<AccessAnswer error onRetry={onRetry} />);
    expect(
      screen.getByText(
        "Azizam couldn’t read the access plans. You can still add the engine; check who can use it on its page.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("a qualifier says when the answer is for unsaved changes", () => {
    render(<AccessAnswer access={accessAnswer("nobody")} qualifier="After you save" />);
    expect(screen.getByText("After you save")).toBeVisible();
  });
});

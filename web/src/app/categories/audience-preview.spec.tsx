import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { AudienceSummary, type Audience } from "@/app/categories/audience-preview";

const loss = (itemId: string) => ({
  item_id: itemId, category_id: "adult", category_label: "Adult",
  app_id: "app-kids", app_name: "Kids app", person_id: "person-alex", person_label: "Alex",
  person_plan_id: "plan_family", app_plan_id: "plan_family",
});

describe("AudienceSummary", () => {
  it("names each item a change moves, even when they share a category, an app and a person", () => {
    const audience: Audience = { gains: [], losses: [loss("adult-flow"), loss("adult-video")], cannot_tell: [] };
    render(<AudienceSummary audience={audience} />);
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Kids app for Alex: adult-flow (Adult)",
      "Kids app for Alex: adult-video (Adult)",
    ]);
  });
});

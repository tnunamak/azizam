import { describe, expect, it } from "vitest";
import type { Category } from "./editor";
import { emptySnapshot, reachText, saveCategory, type PlanCategories } from "./simple";

const category = (id: string, label: string): Category => ({
  id, label, description: "", kind: "set", values: ["included"], applies_to: ["release", "workflow"],
});
const adult = category("adult", "Adult");
const experimental = category("experimental", "Experimental");
const brandNew = category("brand-new", "Brand new");

describe("reachText: which access plans include a category", () => {
  const plans: PlanCategories[] = [
    { name: "Family", categories: ["adult"] },
    { name: "Guest", categories: "all" },
    { name: "Kids", categories: ["experimental"] },
  ];

  it("names every plan that includes the category, in the order given", () => {
    expect(reachText(adult, plans)).toBe("Family, Guest");
    expect(reachText(experimental, plans)).toBe("Guest, Kids");
  });

  it("says no plan includes it when every plan leaves it out", () => {
    expect(reachText(brandNew, [{ name: "Kids", categories: ["experimental"] }])).toBe("No access plan includes it yet");
  });

  it("says there are no access plans at all when there are none", () => {
    expect(reachText(adult, [])).toBe("No access plans yet");
  });

  it("counts 'all' as including a category that was added later", () => {
    expect(reachText(brandNew, [{ name: "Everyone", categories: "all" }])).toBe("Everyone");
  });
});

describe("saveCategory: a category is a name and a meaning", () => {
  it("writes the label, the meaning and the membership value, and nothing about who can see it", () => {
    const { snapshot, id } = saveCategory(emptySnapshot(), { label: "Adult", description: " For adults only. " });
    expect(id).toBe("adult");
    expect(snapshot.categories.adult).toEqual({
      id: "adult", label: "Adult", kind: "set", values: ["included"], applies_to: ["release", "workflow"], description: "For adults only.",
    });
  });
});

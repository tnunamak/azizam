import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, within } from "@testing-library/react";

vi.mock("@/lib/toast", () => ({
  toast: { success: vi.fn(), info: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));
vi.mock("@/lib/signin-api", async (original) => {
  const real = await original<typeof import("@/lib/signin-api")>();
  return {
    ...real,
    getSignIn: vi.fn(),
    previewSignIn: vi.fn(),
    discoverProvider: vi.fn(),
    startSignInTest: vi.fn(),
    getSignInTest: vi.fn(),
    cancelSignInTest: vi.fn(),
    linkOwner: vi.fn(),
    saveSignIn: vi.fn(),
    dismissNotice: vi.fn(),
    setOwnerPassword: vi.fn(),
  };
});

import type { Preview, SignInProposal, SignInRule } from "@/lib/signin-api";
import {
  clearPhone,
  emptyPreview,
  lastProposal,
  liveSnapshot,
  mountSignIn,
  setPhone,
  trusted,
  type Mount,
} from "./test-support";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  clearPhone();
});

const caption = () =>
  document.querySelector('[data-slot="sign-in-caption"]')!.textContent;
const rule = (n: number) => screen.getByRole("group", { name: `Rule ${n}` });
const rulesOf = () => (lastProposal()[0] as SignInProposal).rules.rules;

/** A server stub that only knows an unfinished rule blocks Save. */
const server = (p: SignInProposal): Preview =>
  p.rules.rules.some(
    (r) =>
      (r.field !== "everyone" && r.values.length === 0) ||
      !(r.admit || r.admin || r.plan),
  )
    ? emptyPreview({
        blockers: [{ code: "incomplete_rule", message: "Finish the rule." }],
      })
    : emptyPreview();

async function pick(user: Mount["user"], combobox: HTMLElement, field: string) {
  await user.click(combobox);
  await user.click(await screen.findByRole("option", { name: field }));
}

describe("the rule editor", () => {
  it("adds a rule that blocks Save until it is finished", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    await user.click(screen.getByRole("button", { name: "Add a rule" }));
    const added = rule(2);
    expect(within(added).getByText("New, not saved")).toBeInTheDocument();
    expect(
      within(added).getByText("Choose who this rule is for and what happens."),
    ).toBeInTheDocument();
    // The picker opens by itself for the new rule.
    expect(within(added).getByRole("combobox", { name: "Choose groups" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await vi.waitFor(() =>
      expect(caption()).toBe("Finish or remove the new rule first."),
    );
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    // Remove it: the page is back to nothing to save.
    await user.click(within(added).getByRole("button", { name: "Remove rule" }));
    await vi.waitFor(() => expect(caption()).toBe("Nothing to save."));
    expect(screen.queryByRole("group", { name: "Rule 2" })).toBeNull();
  });

  it("deletes a saved rule and says there are none left", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    await user.click(within(rule(1)).getByRole("button", { name: "Remove rule" }));
    expect(screen.getByText("No rules yet.")).toBeInTheDocument();
    await vi.waitFor(() => expect(rulesOf()).toEqual([]));
    expect(
      screen.getByText("Only people you invite can sign in with Keycloak."),
    ).toBeInTheDocument();
  });

  it("resets the values when the field changes, and lists that field's seen values", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    expect(within(rule(1)).getByText("/Trusted")).toBeInTheDocument();
    await pick(user, within(rule(1)).getByRole("combobox", { name: "Rule applies to" }), "Email");
    expect(within(rule(1)).queryByText("/Trusted")).toBeNull();
    const picker = within(rule(1)).getByRole("combobox", { name: "Choose emails" });
    expect(picker).toHaveAttribute("aria-expanded", "true");
    // Emails show the person's name where groups show a count.
    expect(
      within(rule(1)).getByRole("option", { name: /simon\.agent@vivid\.fish\s+Simon Agent/ }),
    ).toBeInTheDocument();
    await vi.waitFor(() => expect(rulesOf()[0]).toMatchObject({ field: "email", values: [] }));
  });

  it("picks seen values with their counts and removes them again", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    const r = rule(1);
    await user.click(within(r).getByRole("combobox", { name: "Choose groups" }));
    expect(within(r).getByRole("option", { name: /\/Family\s+1 person/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    expect(within(r).getByRole("option", { name: /\/Trusted\s+2 people/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(within(r).getByRole("option", { name: /\/Family/ }));
    expect(within(r).getByText("is one of")).toBeInTheDocument();
    await vi.waitFor(() => expect(rulesOf()[0].values).toEqual(["/Trusted", "/Family"]));
    await user.click(within(r).getByRole("button", { name: "Remove /Trusted" }));
    await vi.waitFor(() => expect(rulesOf()[0].values).toEqual(["/Family"]));
  });

  it("adds a value nobody has signed in with, and flags it", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    const r = rule(1);
    await user.click(within(r).getByRole("combobox", { name: "Choose groups" }));
    const add = within(r).getByRole("textbox", {
      name: "Add a group nobody has signed in with…",
    });
    await user.type(add, "/Staff{Enter}");
    const chip = within(r).getByTitle(
      "Nobody has signed in with this yet. It must match exactly.",
    );
    expect(chip).toHaveTextContent("/Staff");
    // A value somebody has signed in with is not flagged.
    expect(within(r).getAllByTitle(/Nobody has signed in/)).toHaveLength(1);
    expect(within(r).getByRole("option", { name: /\/Staff\s+not seen yet/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await vi.waitFor(() => expect(rulesOf()[0].values).toContain("/Staff"));
    // A repeated value (any case) is not added twice.
    await user.type(add, "/staff{Enter}");
    expect(rulesOf()[0].values.filter((v) => v.toLowerCase() === "/staff")).toHaveLength(1);
  });

  it("shortens the add box on a phone and opens the list as a sheet", async () => {
    setPhone(true);
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    await user.click(within(rule(1)).getByRole("combobox", { name: "Choose groups" }));
    expect(within(rule(1)).getByRole("textbox", { name: "Add a group…" })).toBeInTheDocument();
    const sheet = within(rule(1)).getByRole("listbox").parentElement!;
    expect(sheet.className).toContain("fixed");
    expect(sheet.className).toContain("bottom-3");
  });

  it("works from the keyboard: open, move, toggle, close", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    const combobox = within(rule(1)).getByRole("combobox", { name: "Choose groups" });
    combobox.focus();
    await user.keyboard("{Enter}");
    expect(combobox).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(combobox).toHaveAttribute("aria-expanded", "false");
    expect(combobox).toHaveFocus();
    await user.keyboard(" ");
    expect(combobox).toHaveAttribute("aria-expanded", "true");
    // Tab into the list, arrow to the second option, Space toggles it.
    await user.tab();
    const options = within(rule(1)).getAllByRole("option");
    expect(options[0]).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(options[1]).toHaveFocus();
    await user.keyboard(" ");
    await vi.waitFor(() => expect(rulesOf()[0].values).toEqual([]));
    await user.keyboard("{ArrowUp}{Enter}");
    await vi.waitFor(() => expect(rulesOf()[0].values).toEqual(["/Family"]));
    await user.keyboard("{Escape}");
    expect(combobox).toHaveAttribute("aria-expanded", "false");
  });

  it("makes an administrator rule let people in, and says so", async () => {
    const snapshot = liveSnapshot({
      rules: {
        fallback: "closed",
        rules: [{ ...trusted, admit: false, admin: false, plan: "plan_assistant" }],
      },
    });
    const { user } = await mountSignIn(snapshot, { preview: server });
    const r = rule(1);
    const letIn = within(r).getByRole("checkbox", { name: "Let in" });
    expect(letIn).not.toBeChecked();
    expect(letIn).toBeEnabled();
    await user.click(within(r).getByRole("checkbox", { name: "Make administrator" }));
    expect(letIn).toBeChecked();
    expect(letIn).toBeDisabled();
    expect(letIn.closest("label")).toHaveAttribute(
      "title",
      "Administrators are always let in.",
    );
    expect(
      within(r).getByText(
        "Everyone in /Trusted can manage models, apps, keys and plans. They cannot manage people or sign-in, and cannot become the owner.",
      ),
    ).toBeInTheDocument();
    await vi.waitFor(() =>
      expect(rulesOf()[0]).toMatchObject({ admin: true, admit: true }),
    );
  });

  it("warns when Everyone becomes administrator", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    await pick(user, within(rule(1)).getByRole("combobox", { name: "Rule applies to" }), "Everyone");
    expect(within(rule(1)).getByText("every person Keycloak signs in")).toBeInTheDocument();
    expect(within(rule(1)).queryByRole("combobox", { name: /Choose/ })).toBeNull();
    expect(within(rule(1)).getByRole("alert")).toHaveTextContent(
      "Anyone who signs in with Keycloak becomes an administrator, including people who have not signed in yet.",
    );
  });

  it("shows who an edited rule matches from the server's preview of the form, not the saved rules", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: (p) =>
        emptyPreview({
          rule_matches: Object.fromEntries(
            p.rules.rules.map((r) => [
              r.id,
              r.field === "everyone"
                ? { count: 2, names: ["Simon Agent", "Tim ODL"] }
                : { count: 1, names: ["Simon Agent"] },
            ]),
          ),
        }),
    });
    await pick(user, within(rule(1)).getByRole("combobox", { name: "Rule applies to" }), "Everyone");

    await user.click(
      await within(rule(1)).findByRole("button", {
        name: "Matches 2 people who have signed in",
      }),
    );
    expect(within(rule(1)).getByText("Simon Agent, Tim ODL")).toBeInTheDocument();
  });

  it("gives a plan by name, never by id", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    await user.click(screen.getByRole("button", { name: "Add a rule" }));
    const r = rule(2);
    await user.click(within(r).getByRole("option", { name: /\/Family/ }));
    await user.click(within(r).getByRole("button", { name: "Done" }));
    await user.click(within(r).getByRole("combobox", { name: "Give plan" }));
    expect(await screen.findByRole("option", { name: "No plan" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Give plan: Everything" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Give plan: Local and paid" }));
    expect(within(r).getByRole("combobox", { name: "Give plan" })).toHaveTextContent(
      "Give plan: Local and paid",
    );
    expect(document.body.textContent).not.toMatch(/plan_(local|everything|assistant)/);
    await vi.waitFor(() => expect(rulesOf()[1].plan).toBe("plan_local"));
    // A rule that only gives a plan is finished, so Save is open.
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    );
  });

  describe("plan priority", () => {
    const plan = (id: string, value: string, planId: string | null): SignInRule => ({
      id,
      field: "group",
      values: [value],
      admit: true,
      admin: false,
      plan: planId,
    });
    const snapshot = () =>
      liveSnapshot({
        rules: {
          fallback: "closed",
          rules: [
            plan("a", "/A", "plan_everything"),
            plan("b", "/B", null),
            plan("c", "/C", "plan_assistant"),
          ],
        },
        rule_matches: {},
        seen: { ...liveSnapshot().seen, group: [] },
      });

    it("numbers only the plan rules and labels the arrows by their effect", async () => {
      await mountSignIn(snapshot(), { preview: server });
      expect(within(rule(1)).getByText("Plan priority 1")).toBeInTheDocument();
      expect(within(rule(3)).getByText("Plan priority 2")).toBeInTheDocument();
      // The rule without a plan has no badge and no arrows.
      expect(within(rule(2)).queryByText(/Plan priority/)).toBeNull();
      expect(within(rule(2)).queryByRole("button", { name: /Move/ })).toBeNull();
      expect(
        within(rule(1)).getByRole("button", { name: "Move up, higher plan priority" }),
      ).toBeDisabled();
      expect(
        within(rule(1)).getByRole("button", { name: "Move down, lower plan priority" }),
      ).toBeEnabled();
      expect(
        within(rule(3)).getByRole("button", { name: "Move down, lower plan priority" }),
      ).toBeDisabled();
    });

    it("moves a rule past the next plan rule only", async () => {
      const { user } = await mountSignIn(snapshot(), { preview: server });
      await user.click(
        within(rule(1)).getByRole("button", { name: "Move down, lower plan priority" }),
      );
      await vi.waitFor(() =>
        expect(rulesOf().map((r) => r.id)).toEqual(["c", "b", "a"]),
      );
      expect(within(rule(1)).getByText("/C")).toBeInTheDocument();
      expect(within(rule(1)).getByText("Plan priority 1")).toBeInTheDocument();
      expect(within(rule(3)).getByText("Plan priority 2")).toBeInTheDocument();
    });

    it("shows no priority badge for a lone plan rule", async () => {
      await mountSignIn(
        liveSnapshot({
          rules: { fallback: "closed", rules: [plan("a", "/A", "plan_everything")] },
        }),
        { preview: server },
      );
      expect(screen.queryByText(/Plan priority/)).toBeNull();
      expect(screen.queryByRole("button", { name: /Move/ })).toBeNull();
    });
  });
});

describe("Everyone else", () => {
  it("offers the two choices and warns when strangers get in", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    const open = screen.getByRole("radio", { name: /Let them in, but with no access/ });
    expect(open).not.toBeChecked();
    expect(screen.getByText("Only the rules below and invitations let people in.")).toBeInTheDocument();
    await user.click(open);
    expect(open).toBeChecked();
    expect(
      screen.getByText(
        "Anyone with an account at id.vivid.fish can sign in. They can use nothing until you give them a plan.",
      ),
    ).toBeInTheDocument();
    await vi.waitFor(() =>
      expect((lastProposal()[0] as SignInProposal).rules.fallback).toBe("open"),
    );
    await user.click(screen.getByRole("radio", { name: /Keep them out unless I invite them/ }));
    expect(screen.queryByText(/Anyone with an account at/)).toBeNull();
  });
});

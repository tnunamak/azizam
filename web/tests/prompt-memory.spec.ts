import { describe, expect, it } from "vitest";
import {
  isShorter,
  keyPromptNote,
  purgeStatus,
  tryKeepState,
  withForget,
  withinCap,
  type MemberPromptMemory,
} from "@/lib/prompt-memory";

const member = (
  overrides: Partial<MemberPromptMemory> = {},
  household: Partial<MemberPromptMemory["household"]> = {},
): MemberPromptMemory => ({
  remember: true,
  retention: "never",
  effective_remember: true,
  effective_retention: "never",
  ...overrides,
  household: {
    default_remember: true,
    never_keep: false,
    retention_cap: "never",
    ...household,
  },
});

describe("prompt memory rules", () => {
  it("orders periods from shortest to longest", () => {
    expect(isShorter("3_months", "1_year")).toBe(true);
    expect(isShorter("1_year", "never")).toBe(true);
    expect(isShorter("never", "3_months")).toBe(false);
    expect(isShorter("1_year", "1_year")).toBe(false);
  });

  it("lets a member choose only periods within the household cap", () => {
    expect(withinCap("never", "never")).toBe(true);
    expect(withinCap("never", "1_year")).toBe(false);
    expect(withinCap("1_year", "1_year")).toBe(true);
    expect(withinCap("3_months", "1_year")).toBe(true);
  });

  it("says what Try does before Run, household first", () => {
    expect(tryKeepState(null)).toBe("unknown");
    expect(tryKeepState(member())).toBe("choice");
    expect(
      tryKeepState(member({ remember: false, effective_remember: false })),
    ).toBe("member-off");
    // The kill switch wins even when the member's own choice is on.
    expect(
      tryKeepState(member({ effective_remember: false }, { never_keep: true })),
    ).toBe("household-off");
  });
});

describe("withForget", () => {
  it("adds the forget control to a JSON body and keeps other extensions", () => {
    const body = withForget({
      model: "image",
      prompt: "A cat",
      extensions: { "com.azizam.workflow": { seed: 3 } },
    });
    expect(body.extensions).toEqual({
      "com.azizam.workflow": { seed: 3 },
      "com.azizam.prompts": { mode: "forget" },
    });
    expect(body.prompt).toBe("A cat");
  });

  it("keeps a multipart form's extensions as a JSON string", () => {
    const form = withForget({
      extensions: JSON.stringify({ "com.azizam.workflow": { image: {} } }),
    });
    expect(typeof form.extensions).toBe("string");
    expect(JSON.parse(form.extensions as string)).toEqual({
      "com.azizam.workflow": { image: {} },
      "com.azizam.prompts": { mode: "forget" },
    });
  });

  it("creates the extensions object when there is none", () => {
    expect(withForget({ input: "Hello" }).extensions).toEqual({
      "com.azizam.prompts": { mode: "forget" },
    });
  });
});

describe("purgeStatus", () => {
  it("is silent when nothing is pending", () => {
    expect(purgeStatus(undefined)).toBeNull();
    expect(
      purgeStatus({
        pending: false,
        metadata_stripping: { status: "complete" },
      }),
    ).toBeNull();
  });

  it("names the background work and how many files are left", () => {
    expect(
      purgeStatus({
        pending: true,
        metadata_stripping: { status: "queued", files_pending: 12 },
      }),
    ).toBe(
      "Removing copies from older files in the background. 12 files left.",
    );
    expect(
      purgeStatus({
        pending: true,
        metadata_stripping: { status: "retrying", files_pending: 1 },
      }),
    ).toBe(
      "Some older files still hold copies. Azizam keeps retrying in the background. 1 file left.",
    );
  });

  it("never claims a count it was not given", () => {
    expect(
      purgeStatus({ pending: true, metadata_stripping: { status: "queued" } }),
    ).toBe("Removing copies from older files in the background.");
  });
});

describe("keyPromptNote", () => {
  it("says a service or someone else's key is not remembered", () => {
    expect(keyPromptNote({ person_id: null })).toBe(
      "Prompts sent with this key are not remembered.",
    );
  });

  it("says a person's own key acts as them", () => {
    expect(keyPromptNote({ person_id: "usr_1" })).toMatch(/acts as you/);
  });

  it("says nothing when the gateway did not describe the key", () => {
    expect(keyPromptNote(undefined)).toBeNull();
  });
});

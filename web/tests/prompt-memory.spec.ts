import { describe, expect, it } from "vitest";
import {
  isShorter,
  isKnownRetention,
  isRetention,
  keyPromptNote,
  purgeStatus,
  promptNotSavedReason,
  welcomePromptNote,
  tryKeepState,
  withForget,
  withinCap,
  setRetentionContract,
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

  it("compares a known saved period even when it is no longer offered", () => {
    setRetentionContract({
      choices: [
        { id: "never", seconds: null },
        { id: "3_months", seconds: 7_776_000 },
      ],
      known_unoffered: [{ id: "1_year", seconds: 31_536_000 }],
      defaults: {
        default_remember: true,
        never_keep: false,
        retention_cap: "never",
        remember: true,
        retention: "never",
      },
    });
    expect(isKnownRetention("1_year")).toBe(true);
    expect(isRetention("1_year")).toBe(false);
    expect(isShorter("3_months", "1_year")).toBe(true);
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
  it("puts forget inside queued workflow requests as well as the envelope", () => {
    const request = {
      prompt: "Private words",
      extensions: { "com.azizam.workflow": { seed: 7 } },
    };
    const body = withForget({ intent: "image.generate", request });
    expect(body.request.extensions).toEqual({
      "com.azizam.workflow": { seed: 7 },
      "com.azizam.prompts": { mode: "forget" },
    });
    expect(request.extensions).toEqual({ "com.azizam.workflow": { seed: 7 } });
  });
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
  it("reports a startup deadline repair that has not finished", () => {
    expect(purgeStatus({ pending: true, deadline_repair_pending: true })).toBe(
      "Saved prompts haven't been shortened to the current limits yet. Azizam keeps retrying.",
    );
  });
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
      "Some older files still contain prompts. Azizam keeps retrying. 1 file left.",
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
      "Media prompts sent with this key are not remembered. Requests to /v1/responses with store:true can save the conversation until it is explicitly deleted.",
    );
  });

  it("says a person's own key acts as them", () => {
    expect(keyPromptNote({ person_id: "usr_1" })).toMatch(/acts as you/);
  });

  it("says nothing when the gateway did not describe the key", () => {
    expect(keyPromptNote(undefined)).toBeNull();
  });
});

it("shows the full backend key description", () => {
  expect(
    keyPromptNote({
      person_id: null,
      description: "Chat requests may save a conversation until it is deleted.",
    }),
  ).toBe("Chat requests may save a conversation until it is deleted.");
});

describe("prompt memory copy", () => {
  it("names disabled prompt keeping without guessing missing-prompt reasons", () => {
    expect(promptNotSavedReason("member-off", false)).toBe(
      "You turned off prompt keeping.",
    );
    expect(promptNotSavedReason("household-off", true)).toBe(
      "You turned off prompt keeping for everyone.",
    );
    expect(promptNotSavedReason("household-off", false)).toBe(
      "This gateway doesn’t keep prompts.",
    );
    expect(promptNotSavedReason("choice", false)).toBeNull();
    expect(promptNotSavedReason("unknown", false)).toBeNull();
  });

  it("uses keep consistently in the welcome note with privacy and settings", () => {
    expect(welcomePromptNote("choice")).toBe(
      "Azizam can keep what you type so you can find it again. Only you can see it. Change this in Your settings.",
    );
    expect(welcomePromptNote("unknown")).toBeNull();
  });

  it("names unfinished prompt deletion and retrying without claiming completion", () => {
    expect(purgeStatus({ pending: true, database_pending: true })).toBe(
      "Prompt deletion hasn't finished. Azizam keeps retrying.",
    );
  });

  it("names prompts remaining in older files and preserves retry counts", () => {
    expect(
      purgeStatus({
        pending: true,
        metadata_stripping: { status: "retrying", files_pending: 12 },
      }),
    ).toBe(
      "Some older files still contain prompts. Azizam keeps retrying. 12 files left.",
    );
    expect(
      purgeStatus({
        pending: true,
        metadata_stripping: { status: "retrying" },
      }),
    ).toBe("Some older files still contain prompts. Azizam keeps retrying.");
  });
});

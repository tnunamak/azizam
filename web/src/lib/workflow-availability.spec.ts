import { describe, expect, it } from "vitest";
import {
  MEMBER_SETUP_NOTE,
  READY_TO_START,
  workflowAvailabilityView,
} from "./workflow-availability";

describe("workflowAvailabilityView", () => {
  it("does not run an idle workflow explicitly marked unavailable", () => {
    expect(workflowAvailabilityView(
      { status: "idle", available: false },
      { owner: true },
    )).toEqual({
      readiness: "setup", runnable: false, pickerNote: "Unavailable",
      text: "Unavailable. Check its setup.",
      fix: null,
    });
  });

  it("gives only the owner the Engines fix for a disabled idle workflow", () => {
    const availability = {
      status: "idle", available: false, reason_code: "unavailable_disabled",
      reason: "Turned off. Turn it on in Engines.",
    };
    expect(workflowAvailabilityView(availability, {
      owner: true, engineBinding: "worker one",
    })).toEqual({
      readiness: "setup", runnable: false, pickerNote: "Unavailable",
      text: "Turned off. Turn it on in Engines.",
      fix: { href: "/engines?service=worker%20one", action: "Open Engines" },
    });
    expect(workflowAvailabilityView(availability, { owner: false })).toEqual({
      readiness: "setup", runnable: false, pickerNote: "Unavailable",
      text: MEMBER_SETUP_NOTE, fix: null,
    });
  });

  it("keeps an available idle workflow runnable with its reason as owner idle text", () => {
    expect(workflowAvailabilityView({
      status: "idle", available: true, reason: "  Starts when requested.  ",
    }, { owner: true })).toEqual({
      readiness: "ready_to_start", runnable: true, pickerNote: READY_TO_START,
      text: "Starts when requested.", fix: null,
    });
  });

  it("keeps idle without an explicit availability flag runnable", () => {
    expect(workflowAvailabilityView({
      status: "idle", reason: "Starts when requested.",
    }, { owner: true })).toEqual({
      readiness: "ready_to_start", runnable: true, pickerNote: READY_TO_START,
      text: "Starts when requested.", fix: null,
    });
  });

  it.each([
    ["busy", "Busy", "Busy with other requests. Try again soon."],
    ["starting", "Starting", "Try again in a moment."],
    ["degraded", "Degraded", "Requests may fail."],
  ])("keeps unavailable %s in the wait view", (status, pickerNote, text) => {
    expect(workflowAvailabilityView(
      { status, available: false }, { owner: true },
    )).toEqual({ readiness: "wait", runnable: false, pickerNote, text, fix: null });
  });

  it("keeps busy ahead of refusal reason and setup fixes", () => {
    expect(workflowAvailabilityView({
      status: "busy", available: false,
      reason: "Finishing another request.", reason_code: "unavailable_disabled",
    }, { owner: true })).toEqual({
      readiness: "wait", runnable: false, pickerNote: "Busy",
      text: "Finishing another request.", fix: null,
    });
  });

  it("keeps a reasonless backend refusal down and routes only the owner to Engines", () => {
    const row = { available: false, reason_code: "backend_unavailable" } as const;
    expect(workflowAvailabilityView(row, { owner: false })).toEqual({
      readiness: "down", runnable: false, pickerNote: "Unavailable",
      text: "Unavailable. Ask the owner to check this capability.", fix: null,
    });
    expect(workflowAvailabilityView(row, { owner: true })).toEqual({
      readiness: "down", runnable: false, pickerNote: "Unavailable",
      text: "Unavailable. Check its engine.",
      fix: { href: "/engines", action: "Open Engines" },
    });
  });

  it("uses fixed member idle copy and preserves the owner's idle explanation", () => {
    const row = { status: "idle", available: true, reason: "Starts on demand at this site." };
    expect(workflowAvailabilityView(row, { owner: false }).text)
      .toBe("Ready to start when you make a request.");
    expect(workflowAvailabilityView(row, { owner: true }).text)
      .toBe("Starts on demand at this site.");
  });

  it("uses fixed member wait copy and preserves the owner's wait explanation", () => {
    const row = { status: "busy", available: false, reason: "Finishing the fixture request." };
    expect(workflowAvailabilityView(row, { owner: false })).toEqual({
      readiness: "wait", runnable: false, pickerNote: "Busy",
      text: "Busy with other requests. Try again soon.", fix: null,
    });
    expect(workflowAvailabilityView(row, { owner: true }).text)
      .toBe("Finishing the fixture request.");
  });

  it.each([null, undefined])("keeps absent availability %s runnable", (availability) => {
    expect(workflowAvailabilityView(availability, { owner: false })).toEqual({
      readiness: "ready", runnable: true, pickerNote: null, text: null, fix: null,
    });
  });

  it.each([true, false])("uses the down view for refused idle with a reason (owner=%s)", (owner) => {
    expect(workflowAvailabilityView({
      status: "idle", available: false, reason: "Engine is unavailable.",
      engine: "worker one",
    }, { owner })).toEqual({
      readiness: "down", runnable: false,
      pickerNote: owner ? "Engine is unavailable." : "Unavailable",
      text: owner ? "Engine is unavailable." : "Unavailable. Ask the owner to check this capability.",
      fix: owner ? { href: "/engines?service=worker%20one", action: "Open Engines" } : null,
    });
  });

  it.each([
    { missing: [{ kind: "model", name: "sample.bin" }, { kind: "node", name: "SampleNode" }] as const,
      reason: "Gateway fallback.", text: "1 node and 1 file are missing: sample.bin, SampleNode." },
    { missing: [], reason: "Gateway fallback.", text: "Gateway fallback." },
  ])("gives an owner missing-file setup help: $text", ({ missing, reason, text }) => {
    expect(workflowAvailabilityView({
      available: false, reason_code: "required_file_missing", engine: "worker one",
      missing: missing ? [...missing] : undefined, reason,
    }, { owner: true, engineBinding: "other worker" })).toEqual({
      readiness: "setup", runnable: false, pickerNote: "Unavailable", text,
      fix: { href: "/engines?service=worker%20one", action: "Open Engines" },
    });
  });

  it("routes a policy refusal with gateway prose to the owner Engines remedy", () => {
    const row = { available: false, reason_code: "policy_denied", reason: "Not allowed.", engine: "worker one" };
    expect(workflowAvailabilityView(row, { owner: true })).toEqual({
      readiness: "setup", runnable: false, pickerNote: "Unavailable",
      text: "An access plan blocks it. Check its setup in Engines.",
      fix: { href: "/engines?service=worker%20one", action: "Open Engines" },
    });
    expect(workflowAvailabilityView(row, { owner: false }).fix).toBeNull();
  });

  it("treats an engine transition as wait even when gateway prose sounds like an error", () => {
    expect(workflowAvailabilityView({
      available: false, reason_code: "engine_transitioning", reason: "Engine unavailable.",
    }, { owner: true })).toEqual({
      readiness: "wait", runnable: false, pickerNote: "Starting", text: "Try again shortly.", fix: null,
    });
  });

  it.each(["policy_denied", "required_file_missing"])("does not name a page without prose or structured evidence (%s)", (reason_code) => {
    expect(workflowAvailabilityView({ available: false, reason_code, engine: "worker one" }, { owner: true })).toEqual({
      readiness: "setup", runnable: false, pickerNote: "Unavailable", text: "Unavailable. Check its setup.", fix: null,
    });
  });

  it("gives a member only generic missing-file setup help", () => {
    expect(workflowAvailabilityView({
      available: false, status: "busy", reason_code: "required_file_missing",
      reason: "A file is missing: sample.bin.",
      missing: [{ kind: "model", name: "sample.bin" }],
    }, { owner: false })).toEqual({
      readiness: "setup", runnable: false, pickerNote: "Unavailable",
      text: MEMBER_SETUP_NOTE, fix: null,
    });
  });

  it("still shows a down engine as down", () => {
    const reason = "Engine is unavailable.";
    expect(workflowAvailabilityView({
      available: false, reason_code: "backend_unavailable", reason,
    }, { owner: false })).toEqual({
      readiness: "down", runnable: false, pickerNote: "Unavailable",
      text: "Unavailable. Ask the owner to check this capability.", fix: null,
    });
  });

  it.each([
    "engine_transitioning",
    "backend_unavailable",
    "required_file_missing",
    "policy_denied",
    "unavailable_disabled",
    "unavailable_unconfigured",
    "not_available",
    "unknown",
    undefined,
  ])("keeps structured diagnostics in the owner availability view (%s)", (reason_code) => {
    const missing = [
      { kind: "model", name: "ExampleModel.bin" },
      { kind: "node", name: "the owner's local check" },
      { kind: "model", name: "C:\\models\\a.bin" },
    ] as const;
    const availability = {
      available: false, ...(reason_code ? {reason_code} : {}), missing: [...missing], reason: "Engine is unavailable.",
      engine: "worker one",
    };
    const member = workflowAvailabilityView(availability, { owner: false });
    const owner = workflowAvailabilityView(availability, { owner: true });
    expect(member.fix).toBeNull();
    expect(owner.fix).toEqual({ href: "/engines?service=worker%20one", action: "Open Engines" });
    for (const { name } of missing) {
      expect(JSON.stringify(member)).not.toContain(JSON.stringify(name).slice(1, -1));
      expect(owner.text).toContain(name);
    }
  });
});

describe("workflow availability wait status boundaries", () => {
  it.each([true, false])("keeps a running workflow ready (owner=%s)", (owner) => {
    expect(workflowAvailabilityView({ available: true, status: "running" }, { owner })).toEqual({
      readiness: "ready", runnable: true, pickerNote: null, text: null, fix: null,
    });
  });

  it.each([true, false])("shows the fixed transition note (owner=%s)", (owner) => {
    expect(workflowAvailabilityView({
      available: false, reason_code: "engine_transitioning", reason: "Example gateway reason.",
    }, { owner })).toEqual({
      readiness: "wait", runnable: false, pickerNote: "Starting", text: "Try again shortly.", fix: null,
    });
  });

  it.each([
    ["starting", "Starting", "Try again in a moment."],
    ["degraded", "Degraded", "Requests may fail."],
  ])("uses member-safe %s text and preserves the owner's reason", (status, pickerNote, text) => {
    const base = { available: false, status };
    const expected = { readiness: "wait", runnable: false, pickerNote, text, fix: null };
    const reason = "Example gateway reason.";
    expect(workflowAvailabilityView(base, { owner: false })).toEqual(expected);
    expect(workflowAvailabilityView({ ...base, reason }, { owner: false })).toEqual(expected);
    expect(workflowAvailabilityView({ ...base, reason }, { owner: true })).toEqual({ ...expected, text: reason });
  });

  it.each(["constructor", "toString"])("handles an unrecognized status %s without missing copy", (status) => {
    for (const owner of [true, false]) {
      expect(workflowAvailabilityView({ available: false, status }, { owner })).toEqual({
        readiness: "setup", runnable: false, pickerNote: "Unavailable",
        text: owner ? "Unavailable. Check its setup." : MEMBER_SETUP_NOTE, fix: null,
      });
    }
  });
});

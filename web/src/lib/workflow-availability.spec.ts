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
      text: "Its engine is not answering. See Status.",
      fix: { href: "/status", action: "Open Status" },
    });
  });

  it("gives only the owner the Engines fix for a disabled idle workflow", () => {
    const availability = {
      status: "idle", available: false, reason_code: "unavailable_disabled",
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

  it("keeps an available idle workflow runnable with its reason as idle text", () => {
    expect(workflowAvailabilityView({
      status: "idle", available: true, reason: "  Starts when requested.  ",
    }, { owner: false })).toEqual({
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
      pickerNote: "Engine is unavailable.", text: "Engine is unavailable.",
      fix: owner ? { href: "/engines?service=worker%20one", action: "Open Engines" } : null,
    });
  });
});

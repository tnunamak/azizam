import { describe, expect, it } from "vitest";

import {
  MEMBER_SETUP_NOTE,
  READY_TO_START,
  workflowAvailabilityView,
} from "@/lib/workflow-availability";

const composed = { source: "dependencies", kind: "required" };

describe("workflowAvailabilityView: one mapping for Workflows and Try", () => {
  it("runs a ready workflow with nothing to explain", () => {
    for (const owner of [true, false]) {
      expect(
        workflowAvailabilityView(
          { status: "ready", available: true, reason: "Every engine it needs is ready.", evidence: composed },
          { owner },
        ),
      ).toEqual({ readiness: "ready", runnable: true, pickerNote: null, text: null, fix: null });
    }
  });

  it("reads a stopped on-demand engine as Ready to start, with the gateway's reason", () => {
    const view = workflowAvailabilityView(
      {
        status: "idle",
        available: true,
        reason: "Face service starts when you make a request.",
        engine: "faces",
        evidence: composed,
      },
      { owner: true },
    );
    expect(view).toEqual({
      readiness: "ready_to_start",
      runnable: true,
      pickerNote: READY_TO_START,
      text: "Face service starts when you make a request.",
      fix: null,
    });
  });

  it("falls back to shared idle copy when the gateway gives no reason", () => {
    expect(
      workflowAvailabilityView({ status: "idle", available: true }, { owner: false }).text,
    ).toBe("Ready to start when you make a request.");
  });

  it("names the down engine to members, without a fix link", () => {
    const view = workflowAvailabilityView(
      { status: "unavailable", reason_code: "unavailable", available: false, reason: "ComfyUI is down.", engine: "comfyui", evidence: composed },
      { owner: false, engineBinding: "gateway" },
    );
    expect(view).toEqual({
      readiness: "down",
      runnable: false,
      pickerNote: "ComfyUI is down.",
      text: "ComfyUI is down.",
      fix: null,
    });
  });

  it("gives the owner the same reason and a link to that engine", () => {
    const view = workflowAvailabilityView(
      { status: "unavailable", reason_code: "unavailable", available: false, reason: "ComfyUI is down.", engine: "comfyui", evidence: composed },
      { owner: true, engineBinding: "gateway" },
    );
    expect(view.text).toBe("ComfyUI is down.");
    expect(view.fix).toEqual({ href: "/engines?service=comfyui", action: "Open Engines" });
  });

  it("explains an unchecked composed engine instead of calling it unavailable", () => {
    const view = workflowAvailabilityView(
      { status: "unknown", reason_code: "unknown", available: false, reason: "Face service has not been checked yet.", engine: "faces", evidence: composed },
      { owner: false },
    );
    expect(view.readiness).toBe("down");
    expect(view.text).toBe("Face service has not been checked yet.");
  });

  it("keeps the generic setup copy for workflows without a gateway reason", () => {
    const disabled = { status: "unavailable", reason_code: "unavailable_disabled", available: false };
    expect(workflowAvailabilityView(disabled, { owner: false })).toEqual({
      readiness: "setup",
      runnable: false,
      pickerNote: "Unavailable",
      text: MEMBER_SETUP_NOTE,
      fix: null,
    });
    expect(workflowAvailabilityView(disabled, { owner: true, engineBinding: "comfyui" })).toMatchObject({
      text: "Turned off. Turn it on in Engines.",
      fix: { href: "/engines?service=comfyui", action: "Open Engines" },
    });
    expect(
      workflowAvailabilityView({ status: "unavailable", reason_code: "unavailable", available: false }, { owner: true }).fix,
    ).toEqual({ href: "/status", action: "Open Status" });
  });

  it("lets busy, starting and degraded workflows wait without a fix link", () => {
    expect(
      workflowAvailabilityView({ status: "busy", available: false }, { owner: true }),
    ).toEqual({
      readiness: "wait",
      runnable: false,
      pickerNote: "Busy",
      text: "Busy with other requests. Try again soon.",
      fix: null,
    });
  });
});

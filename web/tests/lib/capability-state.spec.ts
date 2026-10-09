import { describe, expect, it } from "vitest";
import {
  activeCapRefusal,
  capabilityView,
  engineSymptom,
  verdictFor,
  type EngineSignal,
  type RequestSignal,
} from "@/lib/capability-state";
import {
  failingSince,
  healthHistory,
  healthStory,
  hourSlots,
  hoursUp,
} from "@/components/product/uptime-bar";

const now = 1_800_000_000;
const fresh = { source: "status", observed_at_s: now - 60 };
const chat = { key: "chat", label: "Chat", status: "ready", available: true };
const engine = (overrides: Partial<EngineSignal> = {}): EngineSignal => ({
  serviceId: "local-chat",
  name: "Local chat",
  labels: ["Chat"],
  enabled: true,
  observation: "up",
  tone: "ok",
  ...overrides,
});

describe("capabilityView: one state word from recorded evidence", () => {
  it("is Ready only with a fresh check", () => {
    expect(capabilityView({ ...chat, evidence: fresh }, { now }).state).toBe("ready");
  });
  it("says Unknown, with the age, when the check is stale", () => {
    const view = capabilityView(
      { ...chat, evidence: { source: "status", observed_at_s: now - 7 * 3600 } },
      { now },
    );
    expect(view.state).toBe("unknown");
    expect(view.detail).toBe("Last checked 7 h ago.");
  });
  it("says Unknown when there is no evidence, even if available is true", () => {
    expect(capabilityView({ ...chat, evidence: null }, { now }).state).toBe("unknown");
  });
  it("never shows a request as a check", () => {
    const view = capabilityView(
      { ...chat, evidence: { source: "request", observed_at_s: now - 60 } },
      { now },
    );
    expect(view.checkedAt).toBeNull();
    expect(view.usedAt).toBe(now - 60);
  });
  it("is Down when every engine for it fails, and names the fault plainly", () => {
    const view = capabilityView(
      { ...chat, evidence: fresh },
      { now, engines: [engine({ observation: "down", tone: "bad", lastError: "Connection refused" })] },
    );
    expect(view.state).toBe("down");
    expect(view.detail).toBe("Local chat is not accepting connections.");
  });
  it("is Degraded, not Down, when its only engine answers badly", () => {
    const view = capabilityView(
      { ...chat, evidence: fresh },
      { now, engines: [engine({ observation: "degraded", tone: "warn" })] },
    );
    expect(view.state).toBe("degraded");
  });
  it("is Degraded when one of two engines fails", () => {
    const view = capabilityView(
      { ...chat, evidence: fresh },
      {
        now,
        engines: [
          engine(),
          engine({ serviceId: "b", name: "Backup", observation: "down", tone: "bad" }),
        ],
      },
    );
    expect(view.state).toBe("degraded");
  });
  it("does not count a stopped standby engine nobody uses as failing", () => {
    const view = capabilityView(
      { ...chat, evidence: fresh },
      { now, engines: [engine({ observation: "down", tone: "neutral", inUse: false })] },
    );
    expect(view.state).toBe("ready");
  });
  it("does not report Busy beside a failed request", () => {
    const failed: RequestSignal = { capability: "chat", status: "failed", at: now - 120 };
    const view = capabilityView(
      { ...chat, status: "busy", evidence: fresh },
      { now, requests: [failed] },
    );
    expect(view.state).toBe("degraded");
  });
  it("reads an engine the gateway could not check as Unknown, with its last check", () => {
    const view = capabilityView(
      {
        ...chat,
        status: "unknown",
        available: false,
        evidence: { source: "health", kind: "no_response", observed_at_s: now },
      },
      {
        now,
        engines: [
          engine({ observation: "unavailable", tone: "neutral", inUse: true, checkedAt: now - 7 * 3600 }),
        ],
      },
    );
    expect(view.state).toBe("unknown");
    expect(view.detail).toBe("Last checked 7 h ago; no answer since.");
    expect(view.checkedAt).toBeNull();
  });
  it("reads an unavailable projection without a check time as Down", () => {
    const view = capabilityView(
      { ...chat, status: "unavailable", available: false, evidence: null },
      { now },
    );
    expect(view.state).toBe("down");
  });
  it("keeps Off for owner choices and Blocked for plan limits", () => {
    expect(
      capabilityView({ ...chat, reason_code: "unavailable_disabled" }, { now }).state,
    ).toBe("off");
    expect(capabilityView({ ...chat, reason_code: "policy_denied" }, { now }).state).toBe(
      "blocked",
    );
  });
});

describe("spending-cap refusals", () => {
  const refused: RequestSignal = {
    capability: "chat",
    status: "rejected",
    at: now - 3600,
    warnings: ["The Everyone plan has used its daily remote spending allowance."],
  };
  it("apply until a later paid request succeeds", () => {
    expect(activeCapRefusal([refused])).toBe(refused);
    expect(
      activeCapRefusal([
        refused,
        { capability: "chat", status: "succeeded", at: now - 60, remote: true },
      ]),
    ).toBeNull();
  });
  it("are not cleared by a local success", () => {
    expect(
      activeCapRefusal([
        refused,
        { capability: "chat", status: "succeeded", at: now - 60, remote: false },
      ]),
    ).toBe(refused);
  });
});

describe("verdict line", () => {
  const view = (key: string, state: "ready" | "down" | "unknown" | "off") =>
    capabilityView(
      {
        key,
        label: key[0].toUpperCase() + key.slice(1),
        status: state === "down" ? "unavailable" : "ready",
        available: state !== "down",
        reason_code: state === "off" ? "unavailable_disabled" : null,
        evidence: state === "unknown" ? null : fresh,
      },
      { now },
    );
  it("says everything works only when every counted capability is Ready", () => {
    expect(verdictFor([view("chat", "ready"), view("video", "off")]).text).toBe(
      "Everything works",
    );
    expect(verdictFor([view("chat", "ready"), view("video", "unknown")]).text).toBe(
      "Video unknown",
    );
  });
  it("says nothing works only when every counted capability is Down", () => {
    expect(verdictFor([view("chat", "down"), view("video", "down")]).text).toBe(
      "Nothing works right now",
    );
    expect(
      verdictFor([view("chat", "down"), view("video", "down"), view("image", "unknown")]).text,
    ).toBe("3 of 3 capabilities need attention");
  });
  it("names faults without a full stop", () => {
    const text = verdictFor([view("chat", "ready"), view("video", "down")]).text;
    expect(text).toBe("Video down");
    expect(text.endsWith(".")).toBe(false);
  });
});

describe("engine symptoms", () => {
  it("keep transport words out", () => {
    expect(engineSymptom({ name: "Box", lastError: "ECONNREFUSED 127.0.0.1" })).toBe(
      "Box is not accepting connections.",
    );
    expect(engineSymptom({ name: "Box", lastError: "read timed out" })).toBe(
      "Box did not answer in time.",
    );
  });
});

describe("uptime: hours up out of checked hours in the window", () => {
  const end = 1_800_000_000;
  it("does not turn one sample into 24 good hours", () => {
    const { up, checked } = hoursUp(hourSlots([[end - 600, 1]], end));
    expect({ up, checked }).toEqual({ up: 1, checked: 1 });
  });
  it("ignores samples outside the 24 hours", () => {
    const { checked } = hoursUp(hourSlots([[end - 30 * 3600, 1]], end));
    expect(checked).toBe(0);
  });
  it("counts an hour with any failed check as not up", () => {
    const slots = hourSlots(
      [
        [end - 600, 1],
        [end - 300, 0],
      ],
      end,
    );
    expect(hoursUp(slots)).toEqual({ up: 0, checked: 1 });
    expect(failingSince(slots)).toBe(end - 3600);
  });
});

describe("the health story matches the strip (D1.3)", () => {
  const end = 1_800_000_000;
  // One check per hour, oldest first: hour index 0 is 24 h ago.
  const day = (values: Array<number | null>) =>
    hourSlots(
      values.flatMap((value, index) =>
        value === null ? [] : [[end - (24 - index) * 3600 + 1800, value]],
      ),
      end,
    );
  const up = Array<number | null>(24).fill(1);

  it("counts every failing spell, not only the latest", () => {
    const values = [...up];
    values[1] = values[2] = 0; // spell 1
    values[8] = values[9] = 0; // spell 2
    values[22] = 0.5; // degraded now
    values[23] = 0.5;
    const history = healthHistory(day(values));
    expect(history).toMatchObject({ down: 4, degraded: 2, spells: 3 });
    expect(history.failingSince).toBe(end - 2 * 3600);
    const story = healthStory(history)!;
    expect(story).toMatch(
      /^Down 4 and degraded 2 of the last 24 hours, in 3 spells; failing again since about /,
    );
  });

  it("says one spell plainly", () => {
    const values = [...up];
    values[23] = 0;
    expect(healthStory(healthHistory(day(values)))).toMatch(
      /^Down 1 of the last 24 hours; failing since about /,
    );
  });

  it("says when it recovered if it is not failing now", () => {
    const values = [...up];
    values[5] = 0;
    const history = healthHistory(day(values));
    expect(history.recoveredAt).toBe(end - 18 * 3600);
    expect(healthStory(history)).toMatch(/; working again since about /);
  });

  it("says nothing when every checked hour was up", () => {
    expect(healthStory(healthHistory(day(up)))).toBeNull();
  });
});

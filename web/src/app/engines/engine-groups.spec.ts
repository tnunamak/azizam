import { describe, expect, it } from "vitest";
import type { CurrentHealthService, Service } from "@/lib/api";
import { groupEngines, humanCapabilities } from "./engine-groups";

function service(id: string, patch: Partial<Service> = {}): Service {
  return {
    id,
    name: id,
    kind: "http",
    source: "ui",
    enabled: true,
    editable: true,
    capabilities: ["chat"],
    role: "primary",
    priority: 100,
    config: {},
    ...patch,
  };
}

describe("engine capability groups", () => {
  it("groups by capability and folds standby into the highest priority primary without losing engines", () => {
    const rows = [
      service("secondary", { priority: 20 }),
      service("primary", { priority: 10 }),
      service("standby", { role: "standby" }),
      service("image", { capabilities: ["images", "video"] }),
    ];
    const { groups } = groupEngines(rows, new Map(), false, null);
    expect(groups.map((group) => group.capability)).toEqual(["Chat", "Images"]);
    expect(groups[0].entries[0].service.id).toBe("primary");
    expect(groups[0].entries[0].alternatives.map((row) => row.id)).toEqual([
      "standby",
    ]);
    expect(
      groups.flatMap((group) =>
        group.entries.flatMap((entry) => [
          entry.service,
          ...entry.alternatives,
        ]),
      ),
    ).toHaveLength(4);
  });

  it("respects an explicit parent and leaves orphan fallback visible", () => {
    const rows = [
      service("first"),
      service("second"),
      service("fallback", {
        role: "fallback",
        config: { fallback_for: "second" },
      }),
      service("orphan", {
        role: "standby",
        config: { primary_service_id: "missing" },
      }),
    ];
    const { groups } = groupEngines(rows, new Map(), false, null);
    expect(
      groups[0].entries.find((entry) => entry.service.id === "second")
        ?.alternatives[0].id,
    ).toBe("fallback");
    expect(
      groups[0].entries.some((entry) => entry.service.id === "orphan"),
    ).toBe(true);
  });

  it("hides disabled and explicitly unconfigured engines, while allowing their selection and review", () => {
    const rows = [
      service("ready"),
      service("disabled", { enabled: false }),
      service("unconfigured"),
    ];
    const health = new Map([
      ["unconfigured", { configured: false } as CurrentHealthService],
    ]);
    expect(groupEngines(rows, health, false, null).hiddenCount).toBe(2);
    expect(
      groupEngines(rows, health, false, null).groups[0].entries.map(
        (entry) => entry.service.id,
      ),
    ).toEqual(["ready"]);
    expect(
      groupEngines(rows, health, true, null).groups[0].entries,
    ).toHaveLength(3);
    expect(
      groupEngines(rows, health, false, "disabled").groups[0].entries,
    ).toHaveLength(2);
  });

  it("does not treat missing health observations as not in use", () => {
    expect(
      groupEngines([service("unknown")], new Map(), false, null).hiddenCount,
    ).toBe(0);
  });

  it("omits protocol tags from user-facing capability summaries and groups without changing raw data", () => {
    const rows = [
      service("api", {
        capabilities: ["openai", "chat", "completions", "llm_execution"],
      }),
      service("protocol-only", {
        capabilities: ["typesafe_systemone"],
      }),
    ];

    expect(humanCapabilities(rows[0].capabilities)).toEqual(["Chat", "Text"]);
    expect(rows[0].capabilities).toEqual([
      "openai",
      "chat",
      "completions",
      "llm_execution",
    ]);

    const { groups } = groupEngines(rows, new Map(), false, null);
    expect(groups.map((group) => group.capability)).toEqual([
      "Chat",
      "Other engines",
    ]);
    expect(groups[0].entries[0].service.id).toBe("api");
    expect(groups[1].entries[0].service.id).toBe("protocol-only");
  });
});

it("groups speech and tts together as Voice without rewriting engine names", () => {
  const rows = [
    service("speech", { name: "Owner Speech engine", capabilities: ["speech"] }),
    service("tts", { name: "Owner TTS engine", capabilities: ["tts"] }),
  ];
  const { groups } = groupEngines(rows, new Map(), false, null);
  expect(groups).toHaveLength(1);
  expect(groups[0].capability).toBe("Voice");
  expect(groups[0].entries.map(({ service }) => service.name)).toEqual([
    "Owner Speech engine", "Owner TTS engine",
  ]);
  expect(humanCapabilities(["speech", "tts"])).toEqual(["Voice"]);
});

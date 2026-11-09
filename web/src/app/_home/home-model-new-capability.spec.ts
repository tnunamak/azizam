import { beforeEach, describe, expect, it, vi } from "vitest";
import type { HomeCapability, HomeReceipt } from "./home-api";
import {
  capabilityLabel, chooserRows, kindOf, offeredRows, orderChooser, recentItems,
} from "./home-model";
import { requestsFromHome, useCapabilityViews } from "@/lib/use-capability-views";
import type { CapabilityView } from "@/lib/capability-state";

const query = vi.hoisted(() => ({ home: undefined as unknown }));
vi.mock("@tanstack/react-query", () => ({
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({
    data: queryKey[0] === "account" && queryKey[1] === "home" ? query.home : undefined,
    isPending: false, isError: false, isFetching: false, refetch: vi.fn(),
  }),
}));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => ({ state: "ready", providers: [], refetch: vi.fn() }),
}));

function row(capability: string, overrides: Partial<HomeCapability> = {}): HomeCapability {
  return {
    capability, capability_title: capability === "diagram" ? "Diagrams" : "Images",
    icon_key: "image", intent: `${capability}.generate`,
    input_modalities: ["text"], output_modalities: ["image"],
    status: "ready", reason_code: "ready", available: true,
    typical_seconds: 12, cost_hint: { kind: "free", price_usd: null },
    ...overrides,
  } as HomeCapability;
}

const running: HomeReceipt = {
  request_id: "diagram-run", intent: "diagram.generate", status: "running",
  path: "/gateway/v1/media/jobs", accepted_at_ms: 1_800_000_000_000,
};
function items(rows: HomeCapability[], receipt = running) {
  return recentItems({ rows, receipts: [receipt], media: [], chats: [] });
}

beforeEach(() => { query.home = undefined; });

describe("new API capability identity and presentation", () => {
  it("keeps a new diagram receipt and derives its making kind and time from its row", () => {
    const result = items([row("image", { typical_seconds: 99 }), row("diagram")]);
    expect(result.map((item) => item.kind)).toEqual(["making"]);
    expect(result[0]).toMatchObject({
      kind: "making", key: "making:diagram-run", making: "image",
      capability: "diagram", typicalSeconds: 12, startedAt: 1_800_000_000,
    });
  });

  it("uses declared outputs rather than the reused icon or intent spelling", () => {
    expect(items([row("diagram", { output_modalities: ["video"] })])[0])
      .toMatchObject({ making: "video", capability: "diagram" });
    expect(items([row("diagram", { output_modalities: ["audio"] })])[0])
      .toMatchObject({ making: "audio", capability: "diagram" });
  });

  it("keeps a generic making item when a new row declares an unfamiliar output", () => {
    expect(items([row("diagram", { output_modalities: ["blueprint"] })])[0])
      .toMatchObject({ kind: "making", making: "item", capability: "diagram" });
  });

  it("keeps a generic making item when a matching row has no output metadata", () => {
    expect(items([row("diagram", { output_modalities: [] })])[0])
      .toMatchObject({ kind: "making", making: "item", capability: "diagram" });
  });

  it("preserves older image receipts and does not invent making tiles for an unmatched chat", () => {
    expect(items([], { ...running, intent: "image.generate" })[0])
      .toMatchObject({ making: "image", capability: "image" });
    expect(items([], { ...running, intent: "chat.completions", path: "/v1/chat/completions" }))
      .toEqual([]);
  });

  it("does not turn finished or failed receipts into making items", () => {
    expect(items([row("diagram")], { ...running, status: "succeeded" })).toEqual([]);
    expect(items([row("diagram")], { ...running, status: "failed" })).toEqual([]);
  });

  it("keeps the diagram and image identities separate even with the same drawing", () => {
    expect([row("diagram"), row("image")].map(kindOf)).toEqual(["diagram", "image"]);
  });

  it("filters by capability identity rather than a shared or plumbing icon", () => {
    const rows = [
      row("diagram", { icon_key: "embed" }),
      row("embed", { icon_key: "image" }),
      row("new-plumbing-id", { icon_key: "embed" }),
    ];
    expect(chooserRows(rows).map((item) => item.capability)).toEqual(["diagram", "new-plumbing-id"]);
  });

  it("applies a blocked state only to its capability, not another row sharing its icon", () => {
    const rows = [row("diagram"), row("image")];
    const views = [
      { key: "diagram", state: "blocked" },
      { key: "image", state: "ready" },
    ] as CapabilityView[];
    expect(offeredRows(rows, views).map((item) => item.capability)).toEqual(["image"]);
  });

  it("orders usage by the matching API identity for a new intent", () => {
    const rows = [row("image"), row("diagram")];
    expect(orderChooser(rows, [{ ...running, status: "succeeded" }]).map(kindOf))
      .toEqual(["diagram", "image"]);
  });

  it("keeps API titles and visible fallback labels instead of borrowing the icon's name", () => {
    expect(capabilityLabel(row("diagram"))).toBe("Diagrams");
    expect(capabilityLabel(row("diagram", { capability_title: undefined }))).toBe("diagram");
    expect(capabilityLabel(row("image", { capability_title: undefined }))).toBe("Images");
    expect(capabilityLabel(row("new-plumbing-id", { capability_title: undefined, icon_key: "embed" })))
      .toBe("new-plumbing-id");
  });

  it("builds capability views keyed diagram and image, not image twice", () => {
    query.home = { authenticated: true, capabilities: [row("diagram"), row("image")], receipts: [] };
    const result = useCapabilityViews(false);
    expect(result.views.map((view) => view.key)).toEqual(["diagram", "image"]);
    expect(result.views.map((view) => view.label)).toEqual(["Diagrams", "Images"]);
  });

  it("uses identity for member view filtering and preserves an unlisted plumbing id", () => {
    query.home = {
      authenticated: true, receipts: [], capabilities: [
        row("diagram", { icon_key: "embed" }),
        row("embed", { icon_key: "image" }),
        row("new-plumbing-id", { icon_key: "embed", capability_title: undefined }),
      ],
    };
    const result = useCapabilityViews(false);
    expect(result.views.map((view) => view.key)).toEqual(["diagram", "new-plumbing-id"]);
    expect(result.views[1].label).toBe("new-plumbing-id");
  });

  it("maps receipt evidence to the new API capability identity", () => {
    const home = {
      capabilities: [row("diagram"), row("image")], receipts: [running],
    } as Parameters<typeof requestsFromHome>[0];
    expect(requestsFromHome(home)[0]).toMatchObject({ capability: "diagram", status: "running" });
  });

  it("accepts an API icon key outside the old closed list", () => {
    const custom = row("diagram", { icon_key: "blueprint" });
    expect(kindOf(custom)).toBe("diagram");
    expect(custom.icon_key).toBe("blueprint");
  });
});

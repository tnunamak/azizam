import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  ApiError,
  api,
  type AccessPlan,
  type AccessPreview,
  type AccessPreviewEntry,
  type AccessRule,
} from "@/lib/api";
import { CheckAccess } from "@/app/people/plans";

const plan: AccessPlan = {
  id: "plan_family",
  name: "Family",
  created_at: 1,
  updated_at: 1,
  plan: {} as AccessPlan["plan"],
  summary: null,
};

const rule = (
  code: string,
  kind: AccessRule["kind"],
  id: string | null,
  name: string | null,
  plain_reason: string,
): AccessRule => ({ code, kind, id, name, plain_reason });

const NOT_VIDEO = rule(
  "plan.capability_not_granted",
  "plan",
  "plan_family",
  "Family",
  "The Family plan doesn't include video.",
);
const ALL_CHAT = rule(
  "plan.allows_all",
  "plan",
  "plan_family",
  "Family",
  "The Family plan includes every chat item, now and as they are added.",
);
const HIDDEN = rule(
  "category.hidden",
  "category",
  "adult",
  "Adult",
  "It is in the Adult category, which this plan can't see.",
);
const UNREADABLE = rule(
  "category.rules_unavailable",
  "category",
  null,
  null,
  "The category rules couldn't be read just now, so access is refused until they can be.",
);

const entry = (
  over: Partial<AccessPreviewEntry> & Pick<AccessPreviewEntry, "id" | "name">,
): AccessPreviewEntry => ({
  alias: false,
  group: "chat",
  group_label: "Chat",
  state: "allowed",
  reason: null,
  rule: null,
  points_to: [],
  kinds: [],
  ...over,
});

const preview = (entries: AccessPreviewEntry[]): AccessPreview => {
  const groups = entries.reduce<AccessPreview["groups"]>((found, item) => {
    let group = found.find((g) => g.group === item.group);
    if (!group) {
      group = { group: item.group, label: item.group_label, total: 0, usable: 0, blocked: 0, unknown: 0 };
      found.push(group);
    }
    group.total += 1;
    if (item.state === "blocked") group.blocked += 1;
    else if (item.state === "unknown") group.unknown += 1;
    else group.usable += 1;
    return found;
  }, []);
  return {
    plan_ids: ["plan_family"],
    summary: {
      usable: entries.filter((item) => item.state === "allowed" || item.state === "partial").length,
      total: entries.length,
      blocked: entries.filter((item) => item.state === "blocked").length,
      unknown: entries.filter((item) => item.state === "unknown").length,
    },
    groups,
    entries,
  };
};

const SMALL = [
  entry({ id: "fast", name: "Fast chat", rule: ALL_CHAT }),
  entry({
    id: "big",
    name: "Big chat",
    state: "blocked",
    reason: "The Family plan lists the chat items it allows, and Big chat isn't one of them.",
    rule: rule(
      "plan.item_not_listed",
      "plan",
      "plan_family",
      "Family",
      "The Family plan lists the chat items it allows, and Big chat isn't one of them.",
    ),
  }),
  entry({
    id: "flux",
    name: "Flux",
    group: "image",
    group_label: "Images",
    state: "partial",
    reason: "The Family plan doesn't include images.",
    rule: rule("plan.capability_not_granted", "plan", "plan_family", "Family", "The Family plan doesn't include images."),
    kinds: [
      { kind: "chat", label: "Chat", allowed: true, state: "allowed", reason: null, rule: ALL_CHAT },
      {
        kind: "image",
        label: "Images",
        allowed: false,
        state: "blocked",
        reason: "The Family plan doesn't include images.",
        rule: rule("plan.capability_not_granted", "plan", "plan_family", "Family", "The Family plan doesn't include images."),
      },
    ],
  }),
];

const LONG = Array.from({ length: 8 }, (_, index) =>
  entry({
    id: `model-${index}`,
    name: `Model ${index}`,
    state: index % 2 ? "blocked" : "allowed",
    reason: index % 2 ? `Model ${index} is blocked.` : null,
    rule: index % 2 ? NOT_VIDEO : ALL_CHAT,
  }),
);

function mount(
  result: AccessPreview | Error,
  props: { canEdit?: boolean; onEditPlan?: (id: string) => void } = {},
) {
  const spy = vi.spyOn(api.accessPlans, "preview");
  if (result instanceof Error) spy.mockRejectedValueOnce(result).mockResolvedValue(preview(SMALL));
  else spy.mockResolvedValue(result);
  const close = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CheckAccess plan={plan} close={close} canEdit={props.canEdit ?? true} onEditPlan={props.onEditPlan} />
    </QueryClientProvider>,
  );
  return { spy, close };
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const rowOf = (name: string) => screen.getByText(name).closest("li") as HTMLElement;

describe("Check access", () => {
  it("lists what the plan can and cannot use without any typing", async () => {
    const { spy } = mount(preview(SMALL));
    expect(await screen.findByText("Can use 2 of 3")).toBeVisible();
    expect(screen.getByText(/Previewing Family for both the person and the app/)).toBeVisible();
    expect(screen.getByText(/does not assign the plan or change Try/)).toBeVisible();
    expect(spy).toHaveBeenCalledExactlyOnceWith("plan_family");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByLabelText("Model or workflow")).toBeNull();
    expect(screen.queryByLabelText("Request kind")).toBeNull();
    const chat = within(screen.getByRole("heading", { name: "Chat" }).closest("section") as HTMLElement);
    expect(chat.getByText("Fast chat")).toBeVisible();
    expect(chat.getByText("Allowed")).toBeVisible();
    const images = within(screen.getByRole("heading", { name: "Images" }).closest("section") as HTMLElement);
    expect(images.getByText("Flux")).toBeVisible();
  });

  it("names the rule that blocked a row, in plain words", async () => {
    mount(
      preview([
        entry({
          id: "vid",
          name: "Clip maker",
          group: "video",
          group_label: "Video",
          state: "blocked",
          reason: NOT_VIDEO.plain_reason,
          rule: NOT_VIDEO,
        }),
      ]),
    );
    await screen.findByText("Can use 0 of 1");
    const row = rowOf("Clip maker");
    expect(within(row).getByText("Blocked")).toBeVisible();
    expect(row).toHaveTextContent("Blocked: The Family plan doesn't include video.");
    expect(row).not.toHaveTextContent("plan_family");
  });

  it("shows kinds compactly only for an entry allowed for some kinds", async () => {
    mount(preview(SMALL));
    await screen.findByText("Can use 2 of 3");
    const flux = rowOf("Flux");
    expect(within(flux).getByText("Partly allowed")).toBeVisible();
    expect(flux).toHaveTextContent("Chat ✓allowed · Images ✗blocked");
    expect(within(flux).getByText("Images: The Family plan doesn't include images.")).toBeVisible();
    expect(rowOf("Fast chat")).not.toHaveTextContent("✓");
  });
});

describe("Check access: the rule behind a row", () => {
  it("links a blocked plan rule to the plan editor, and opens it in place", async () => {
    const user = userEvent.setup();
    const onEditPlan = vi.fn();
    const { close } = mount(preview([SMALL[1]]), { onEditPlan });
    await screen.findByText("Can use 0 of 1");
    const link = within(rowOf("Big chat")).getByRole("link", { name: "Edit the Family plan" });
    expect(link).toHaveAttribute("href", expect.stringMatching(/^\/people\/?\?plan=plan_family$/));
    await user.click(link);
    expect(onEditPlan).toHaveBeenCalledExactlyOnceWith("plan_family");
    expect(close).not.toHaveBeenCalled();
  });

  it("links a category rule to that category", async () => {
    mount(
      preview([
        entry({ id: "secret", name: "Secret", state: "blocked", reason: HIDDEN.plain_reason, rule: HIDDEN }),
      ]),
    );
    await screen.findByText("Can use 0 of 1");
    const link = within(rowOf("Secret")).getByRole("link", { name: "Edit the Adult category" });
    expect(link).toHaveAttribute("href", expect.stringMatching(/^\/categories\/?\?category=adult$/));
    expect(rowOf("Secret")).toHaveTextContent("Blocked: It is in the Adult category, which this plan can't see.");
  });

  it("leaves the edit links out for someone who may not edit", async () => {
    mount(preview([SMALL[1]]), { canEdit: false });
    await screen.findByText("Can use 0 of 1");
    expect(within(rowOf("Big chat")).getByText(/isn't one of them/)).toBeVisible();
    expect(screen.queryByRole("link", { name: /Edit/ })).toBeNull();
  });

  it("shows why a row is allowed only when it is expanded", async () => {
    const user = userEvent.setup();
    mount(preview([SMALL[0]]));
    await screen.findByText("Can use 1 of 1");
    expect(screen.queryByText(/Why allowed/)).toBeNull();
    const toggle = within(rowOf("Fast chat")).getByRole("button", { name: "Why Fast chat" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const row = rowOf("Fast chat");
    expect(row).toHaveTextContent(
      "Why allowed: The Family plan includes every chat item, now and as they are added.",
    );
    expect(row).toHaveTextContent("plan.allows_all");
    expect(within(row).getByRole("link", { name: "Edit the Family plan" })).toBeVisible();
    await user.click(toggle);
    expect(screen.queryByText(/Why allowed/)).toBeNull();
  });

  it("lists every kind's rule when a blocked row is expanded", async () => {
    const user = userEvent.setup();
    mount(preview([SMALL[2]]));
    await screen.findByText("Can use 1 of 1");
    await user.click(screen.getByRole("button", { name: "Why Flux" }));
    const row = rowOf("Flux");
    expect(row).toHaveTextContent("Chat: Why allowed: The Family plan includes every chat item");
    expect(row).toHaveTextContent("Images: Blocked: The Family plan doesn't include images.");
    expect(row).toHaveTextContent("plan.capability_not_granted");
  });

  it("shows an alias with the model it points to, linked to that model", async () => {
    mount(
      preview([
        entry({
          id: "fast",
          name: "fast",
          alias: true,
          points_to: [{ id: "big-model", name: "Big chat" }],
          rule: ALL_CHAT,
        }),
      ]),
    );
    await screen.findByText("Can use 1 of 1");
    const link = within(rowOf("fast")).getByRole("link", { name: "Big chat" });
    expect(link).toHaveAttribute("href", expect.stringMatching(/^\/models\/?\?model=big-model$/));
    expect(rowOf("fast")).toHaveTextContent("Points to Big chat");
  });
});

describe("Check access: cannot tell", () => {
  const unknown = entry({
    id: "mystery",
    name: "Mystery",
    state: "unknown",
    reason: UNREADABLE.plain_reason,
    rule: UNREADABLE,
  });

  it("says it cannot tell, with the reason, and does not count the row as usable", async () => {
    mount(preview([SMALL[0], unknown]));
    await screen.findByText("Can use 1 of 2");
    const row = rowOf("Mystery");
    expect(within(row).getByText("Can't tell")).toBeVisible();
    expect(within(row).queryByText("Blocked")).toBeNull();
    expect(row).toHaveTextContent(
      "Can't tell: The category rules couldn't be read just now, so access is refused until they can be.",
    );
    expect(screen.getByText("1 can't tell")).toBeVisible();
  });

  it("links a rules-unavailable row to the categories page when the viewer may edit", async () => {
    mount(preview([unknown]));
    await screen.findByText("Can use 0 of 1");
    const link = within(rowOf("Mystery")).getByRole("link", { name: "Open Categories" });
    expect(link).toHaveAttribute("href", expect.stringMatching(/^\/categories\/?$/));
  });
});

describe("Check access: counts, filter and search", () => {
  it("counts each group on its own header", async () => {
    mount(preview([...SMALL, entry({ id: "mystery", name: "Mystery", state: "unknown", reason: "?", rule: UNREADABLE })]));
    await screen.findByText("Can use 2 of 4");
    const chat = screen.getByRole("heading", { name: "Chat" }).closest("section") as HTMLElement;
    expect(chat).toHaveTextContent("1 of 3 usable · 1 blocked · 1 can't tell");
    const images = screen.getByRole("heading", { name: "Images" }).closest("section") as HTMLElement;
    expect(within(images).getByText("1 of 1 usable")).toBeVisible();
  });

  it("filters a long list by name and by blocked only", async () => {
    const user = userEvent.setup();
    mount(preview(LONG));
    await screen.findByText("Can use 4 of 8");
    expect(screen.getAllByRole("listitem")).toHaveLength(8);
    await user.type(screen.getByRole("searchbox", { name: "Filter models and workflows" }), "model 3");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("Model 3")).toBeVisible();
    await user.clear(screen.getByRole("searchbox", { name: "Filter models and workflows" }));
    const toggle = screen.getByRole("button", { name: "Show blocked only" });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.queryByText("Allowed")).toBeNull();
    await user.type(screen.getByRole("searchbox", { name: "Filter models and workflows" }), "nothing like it");
    expect(screen.getByText("Nothing matches.")).toBeVisible();
  });

  it("keeps a row that cannot be told under the blocked filter", async () => {
    const user = userEvent.setup();
    const rows = [
      ...LONG,
      entry({ id: "mystery", name: "Mystery", state: "unknown", reason: UNREADABLE.plain_reason, rule: UNREADABLE }),
    ];
    mount(preview(rows));
    await screen.findByText("Can use 4 of 9");
    await user.click(screen.getByRole("button", { name: "Show blocked only" }));
    expect(within(rowOf("Mystery")).getByText("Can't tell")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
  });

  it("searches the name of the model an alias points to", async () => {
    const user = userEvent.setup();
    const rows = [
      ...LONG,
      entry({ id: "quick", name: "quick", alias: true, points_to: [{ id: "model-9", name: "Zebra model" }], rule: ALL_CHAT }),
    ];
    mount(preview(rows));
    await screen.findByText("Can use 5 of 9");
    await user.type(screen.getByRole("searchbox", { name: "Filter models and workflows" }), "zebra");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("quick")).toBeVisible();
  });

  it("keeps the filter out of a short list", async () => {
    mount(preview(SMALL));
    await screen.findByText("Can use 2 of 3");
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show blocked only" })).toBeNull();
  });
});

describe("Check access: the dialog", () => {
  it("says what the check does not cover", async () => {
    mount(preview(SMALL));
    await screen.findByText("Can use 2 of 3");
    expect(
      screen.getByText(/does not check that an engine is running, that a spending cap has room/),
    ).toBeVisible();
  });

  it("says so when the gateway offers nothing to check", async () => {
    mount(preview([]));
    expect(await screen.findByText("The gateway has no models or workflows to check yet.")).toBeVisible();
  });

  it("shows an error with a retry, not an empty list", async () => {
    const user = userEvent.setup();
    const { spy } = mount(new ApiError("access_preview_unavailable", "What this plan can use could not be read.", 503));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByText(/Can use/)).toBeNull();
    await user.click(screen.getByRole("button", { name: /try again|retry/i }));
    expect(await screen.findByText("Can use 2 of 3")).toBeVisible();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("closes from the Close button", async () => {
    const user = userEvent.setup();
    const { close } = mount(preview(SMALL));
    await screen.findByText("Can use 2 of 3");
    await user.click(screen.getAllByRole("button", { name: "Close" }).at(-1) as HTMLElement);
    expect(close).toHaveBeenCalled();
  });
});

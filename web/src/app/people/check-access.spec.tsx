import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, api, type AccessPlan, type AccessPreview, type AccessPreviewEntry } from "@/lib/api";
import { CheckAccess } from "@/app/people/plans";

const plan: AccessPlan = {
  id: "plan_family",
  name: "Family",
  created_at: 1,
  updated_at: 1,
  plan: {} as AccessPlan["plan"],
  summary: null,
};

const entry = (over: Partial<AccessPreviewEntry> & Pick<AccessPreviewEntry, "id" | "name">): AccessPreviewEntry => ({
  alias: false,
  group: "chat",
  group_label: "Chat",
  state: "allowed",
  reason: null,
  kinds: [],
  ...over,
});

const preview = (entries: AccessPreviewEntry[]): AccessPreview => ({
  plan_ids: ["plan_family"],
  summary: {
    usable: entries.filter((item) => item.state !== "blocked").length,
    total: entries.length,
  },
  entries,
});

const SMALL = [
  entry({ id: "fast", name: "Fast chat" }),
  entry({
    id: "big",
    name: "Big chat",
    state: "blocked",
    reason: "big isn't in your access plan.",
  }),
  entry({
    id: "flux",
    name: "Flux",
    group: "image",
    group_label: "Images",
    state: "partial",
    reason: "flux isn't in your access plan.",
    kinds: [
      { kind: "chat", label: "Chat", allowed: true, reason: null },
      { kind: "image", label: "Images", allowed: false, reason: "flux isn't in your access plan." },
    ],
  }),
];

const LONG = Array.from({ length: 8 }, (_, index) =>
  entry({
    id: `model-${index}`,
    name: `Model ${index}`,
    state: index % 2 ? "blocked" : "allowed",
    reason: index % 2 ? `model-${index} isn't in your access plan.` : null,
  }),
);

function mount(result: AccessPreview | Error) {
  const spy = vi.spyOn(api.accessPlans, "preview");
  if (result instanceof Error) spy.mockRejectedValueOnce(result).mockResolvedValue(preview(SMALL));
  else spy.mockResolvedValue(result);
  const close = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CheckAccess plan={plan} close={close} />
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

describe("Check access", () => {
  it("lists what the plan can and cannot use without any typing", async () => {
    const { spy } = mount(preview(SMALL));
    expect(await screen.findByText("Can use 2 of 3")).toBeVisible();
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

  it("explains a blocked entry in plain words", async () => {
    mount(preview(SMALL));
    await screen.findByText("Can use 2 of 3");
    const row = screen.getByText("Big chat").closest("li") as HTMLElement;
    expect(within(row).getByText("Blocked")).toBeVisible();
    expect(within(row).getByText("big isn't in your access plan.")).toBeVisible();
  });

  it("shows kinds compactly only for an entry allowed for some kinds", async () => {
    mount(preview(SMALL));
    await screen.findByText("Can use 2 of 3");
    const flux = screen.getByText("Flux").closest("li") as HTMLElement;
    expect(within(flux).getByText("Partly allowed")).toBeVisible();
    expect(flux).toHaveTextContent("Chat ✓allowed · Images ✗blocked");
    expect(within(flux).getByText("Images: flux isn't in your access plan.")).toBeVisible();
    const fast = screen.getByText("Fast chat").closest("li") as HTMLElement;
    expect(fast).not.toHaveTextContent("✓");
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

  it("keeps the filter out of a short list", async () => {
    mount(preview(SMALL));
    await screen.findByText("Can use 2 of 3");
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show blocked only" })).toBeNull();
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

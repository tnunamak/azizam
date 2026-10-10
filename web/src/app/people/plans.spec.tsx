import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type PolicyEditorChoice, type Profile } from "@/lib/api";
import { PlanEditor } from "@/app/people/plans";

const intents: PolicyEditorChoice[] = [
  { id: "chat", label: "Chat", description: null, aliases: ["chat.completions"] },
  { id: "image.generate", label: "Make a picture", description: null, aliases: ["images.generate"] },
  { id: "speech.synthesis", label: "Read text aloud", description: null, aliases: ["audio.speech"] },
  { id: "models.list", label: "List models", description: null, aliases: ["capabilities.list"] },
  { id: "video.generate", label: "Make a video", description: null, aliases: [] },
];
const plan = (saved: string[]): Profile => ({
  id: "media-creator",
  name: "Media creator",
  description: null,
  created_at: 1,
  protocols: [],
  workflows: [],
  status_visibility: "basic",
  profile: { allow: { intents: saved } },
});

function mount(saved: string[]) {
  vi.spyOn(api.policyEditor, "get").mockResolvedValue({
    profile: {
      create: { profile: {} },
      choices: {
        intents,
        ...Object.fromEntries(
          ["exposure", "models", "protocols", "workflows", "trust_tiers", "resource_tiers", "tags"].map((key) => [key, []]),
        ),
      },
    },
  } as never);
  const update = vi.spyOn(api.profiles, "update").mockResolvedValue({} as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PlanEditor plan={plan(saved)} close={vi.fn()} saved={vi.fn(async () => {})} />
    </QueryClientProvider>,
  );
  return update;
}

async function capabilities() {
  const legend = await screen.findByText("Capabilities", { selector: "legend" });
  return within(legend.closest("fieldset") as HTMLElement);
}

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Access plan capabilities", () => {
  it("checks saved values under their plain choice in either spelling", async () => {
    mount(["chat", "image.generate", "audio.speech", "capabilities.list"]);
    const set = await capabilities();
    for (const label of ["Chat", "Make a picture", "Read text aloud", "List models"])
      expect(set.getByRole("checkbox", { name: new RegExp(`^${label}`) })).toBeChecked();
    expect(set.getByRole("checkbox", { name: /^Make a video/ })).not.toBeChecked();
    expect(set.queryByText(/Other saved values/)).not.toBeInTheDocument();
  });

  it("labels every choice in words and keeps the raw id as secondary text", async () => {
    mount([]);
    const set = await capabilities();
    for (const choice of intents) {
      const box = set.getByRole("checkbox", { name: new RegExp(`^${choice.label}`) });
      expect(box.closest("label")).toHaveTextContent(`${choice.label}${choice.id}`);
    }
  });

  it("lists only genuinely unknown ids as other saved values", async () => {
    mount(["chat.completions", "extension.custom"]);
    const set = await capabilities();
    expect(set.getByRole("checkbox", { name: /^Chat/ })).toBeChecked();
    expect(set.getByText("Other saved values (preserved): extension.custom")).toBeInTheDocument();
  });

  it("unchecking a choice removes its saved alias and keeps unknown ids", async () => {
    const update = mount(["audio.speech", "chat", "extension.custom"]);
    const set = await capabilities();
    await userEvent.click(set.getByRole("checkbox", { name: /^Read text aloud/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save access plan" }));
    expect(update).toHaveBeenCalledWith(
      "media-creator",
      expect.objectContaining({ profile: { allow: { intents: ["chat", "extension.custom"] } } }),
    );
  });
});

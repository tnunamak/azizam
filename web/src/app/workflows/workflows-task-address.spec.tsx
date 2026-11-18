// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import WorkflowsPage from "./page";

const viewer = vi.hoisted(() => ({ role: "member", admin: false }));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer }));
vi.mock("./owner-view", () => ({ OwnerView: () => null }));

const workflow = (id: string, capability: string, title: string) => ({
  id, capability, title, description: "A sample workflow.", inputs: [],
  outputs: [{ name: "result", media_type: capability === "image" ? "image/png" : "video/mp4" }],
  aliases: [], engine_binding: null, native_route: null, typical_seconds: null,
  discovery_url: "/workflows/" + id,
  availability: { status: "ready", reason_code: "ready", available: true },
  examples: { curl: "", python: "", javascript: "" },
});
const catalog = {
  schema_version: 1,
  workflows: [workflow("pictures", "image", "Picture sample"), workflow("clips", "video", "Video sample")],
};
const setupCatalog = {
  schema_version: 1,
  workflows: [
    workflow("pictures", "image", "Picture sample"),
    {
      ...workflow("clips", "video", "Setup video sample"),
      availability: { status: "not_configured", reason_code: "unavailable_unconfigured", available: false },
    },
  ],
};
const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><WorkflowsPage /></QueryClientProvider>);
  return client;
}
function stubCatalog(value = catalog) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value), { status: 200 })));
}
async function selected(value: string) {
  await waitFor(() => {
    const select = screen.getByRole("combobox", { name: "Task" }) as HTMLSelectElement;
    expect(select.value).toBe(value);
    const label = select.selectedOptions[0].textContent!;
    const desktop = within(screen.getByRole("group", { name: "Workflow task" }));
    expect(desktop.getByRole("button", { name: label }).getAttribute("aria-pressed")).toBe("true");
  });
}
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/workflows/");
  viewer.role = "member";
  viewer.admin = false;
});

describe("workflow task address", () => {
  it("applies an existing task after the catalog loads to desktop and mobile", async () => {
    window.history.replaceState(null, "", "/workflows/?capability=video");
    let reply!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { reply = resolve; })));
    mount();
    await waitFor(() => expect(reply).toBeTypeOf("function"));
    await act(async () => { reply(new Response(JSON.stringify(catalog), { status: 200 })); });
    await selected("video");
    expect(screen.getByRole("button", { name: "Details for Video sample" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Details for Picture sample" })).toBeNull();
  });

  it("leaves All selected for an unknown task", async () => {
    window.history.replaceState(null, "", "/workflows/?capability=unknown");
    stubCatalog();
    mount();
    await selected("all");
    expect(screen.getByRole("button", { name: "Details for Picture sample" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Details for Video sample" })).toBeTruthy();
  });

  it("leaves All selected for a member when the address task only has setup workflows", async () => {
    window.history.replaceState(null, "", "/workflows/?capability=video");
    stubCatalog(setupCatalog);
    mount();
    await selected("all");
    expect(screen.getByRole("button", { name: "Details for Picture sample" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Details for Setup video sample" })).toBeNull();
  });

  it("selects an address task with only setup workflows for the owner", async () => {
    viewer.role = "owner";
    viewer.admin = true;
    window.history.replaceState(null, "", "/workflows/?capability=video");
    stubCatalog(setupCatalog);
    mount();
    await selected("video");
  });

  it("keeps a later task choice across refresh without rewriting the address, like Models", async () => {
    window.history.replaceState({ retained: true }, "", "/workflows/?capability=video&other=keep#section");
    stubCatalog();
    const client = mount();
    await selected("video");
    fireEvent.change(screen.getByRole("combobox", { name: "Task" }), { target: { value: "image" } });
    await selected("image");
    await act(async () => { await client.invalidateQueries({ queryKey: ["workflows", "catalog"] }); });
    await selected("image");
    expect(window.location.search).toBe("?capability=video&other=keep");
    expect(window.location.hash).toBe("#section");
    expect(window.history.state).toEqual({ retained: true });
    fireEvent.click(within(screen.getByRole("group", { name: "Workflow task" })).getByRole("button", { name: "All" }));
    await selected("all");
    expect(window.location.search).toBe("?capability=video&other=keep");
  });
});

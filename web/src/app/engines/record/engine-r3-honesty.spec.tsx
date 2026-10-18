import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi, type EngineView } from "@/lib/engines-api";
import { accessAnswer, definitions, engineView, renderWithClient, testResult } from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";
const viewer = vi.hoisted(() => ({ role: "owner" }));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer }));
function mount(over: Partial<EngineView> = {}) {
  const view = engineView(over);
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  return renderWithClient(<EngineRecordPage serviceId={view.service_id} onBack={() => {}} onRemove={() => {}} />);
}
beforeEach(() => {
  viewer.role = "owner";
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("nobody"));
});
afterEach(cleanup);
it.each(["failed", "not_started"] as const)("enabled with %s evidence never claims ready", async (status) => {
  mount({ check: { status, ...(status === "failed" ? { checked_at: 123 } : {}) } });
  await screen.findByRole("heading", { level: 1 });
  const header = document.querySelector("header")!;
  expect(header).toHaveTextContent("On");
  expect(header).toHaveTextContent(status === "failed" ? "Check failed" : "Unknown");
  expect(header).not.toHaveTextContent("Ready");
});
it("protocol mismatch disables checks and explains the reason", async () => {
  mount({ unsupported_fields: { address: "This saved protocol does not match." }, can_edit_offerings: false });
  expect(await screen.findByRole("button", { name: "Check again" })).toBeDisabled();
  expect(screen.getByText("This protocol cannot be checked from this page yet.")).toBeVisible();
});
it("local search works for a read-only model list", async () => {
  viewer.role = "admin";
  mount();
  const search = await screen.findByRole("searchbox", { name: "Search models" });
  expect(search).toBeEnabled();
  await userEvent.type(search, "nomic");
  expect(screen.queryByRole("checkbox", { name: /Offer qwen/ })).toBeNull();
  expect(screen.getByRole("checkbox", { name: /Offer nomic/ })).toBeDisabled();
});
it("a draft test is labeled unsaved and never sent as saved evidence", async () => {
  mount();
  const user = userEvent.setup();
  const more = await screen.findByText("More options", { selector: "summary span" });
  if (!more.closest("details")?.open) await user.click(more);
  await user.type(screen.getByLabelText("Extra path before every request"), "/draft");
  vi.spyOn(enginesApi, "test").mockResolvedValue({ ...testResult("chat_pass"), via: "draft" });
  const update = vi.spyOn(enginesApi, "update").mockResolvedValue({ schema_version: 1, services: {} } as never);
  await user.click(screen.getByRole("button", { name: "Run a real test for Chat" }));
  expect(await screen.findByText(/Tested your unsaved changes/)).toBeVisible();
  expect(update).not.toHaveBeenCalled();
});
it("editing after a test starts cannot relabel it as proof of the new draft", async () => {
  mount();
  const user = userEvent.setup();
  let complete!: (v: ReturnType<typeof testResult>) => void;
  vi.spyOn(enginesApi, "test").mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  vi.spyOn(enginesApi, "update").mockResolvedValue({ schema_version: 1, services: {} } as never);
  await user.click(await screen.findByRole("button", { name: "Run a real test for Chat" }));
  const more = screen.getByText("More options", { selector: "summary span" });
  if (!more.closest("details")?.open) await user.click(more);
  expect(screen.getByLabelText("Extra path before every request")).toBeEnabled();
  await user.type(screen.getByLabelText("Extra path before every request"), "/changed");
  expect(screen.getByLabelText("Extra path before every request")).toHaveValue("/changed");
  await act(async () => complete({ ...testResult("chat_pass"), via: "saved" }));
  await waitFor(() => expect(screen.queryByLabelText("Real test of Chat")).toBeNull());
  expect(screen.queryByText(/Tested the saved settings/)).toBeNull();
});

it.each(["path", "header", "model"])("discarding tested draft %s changes leaves no saved proof", async (field) => {
  const mounted = mount();
  const user = userEvent.setup();
  await screen.findByRole("heading", { level: 1 });
  const more = screen.getByText("More options", { selector: "summary span" });
  if (!more.closest("details")?.open) await user.click(more);
  if (field === "path") await user.type(screen.getByLabelText("Extra path before every request"), "/draft");
  if (field === "header") {
    await user.click(screen.getByRole("button", { name: "Add a header" }));
    await user.type(screen.getByLabelText("Header name"), "X-Tenant");
    await user.type(screen.getByLabelText("Value"), "draft");
  }
  if (field === "model") await user.click(screen.getByRole("checkbox", { name: /Offer qwen/ }));
  const run = vi.spyOn(enginesApi, "test").mockResolvedValue({ ...testResult("chat_pass"), via: "draft" });
  const update = vi.spyOn(enginesApi, "update");
  await user.click(screen.getByRole("button", { name: "Run a real test for Chat" }));
  expect(await screen.findByText(/Tested your unsaved changes/)).toBeVisible();
  expect(run).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.queryByLabelText("Real test of Chat")).toBeNull();
  expect(update).not.toHaveBeenCalled();
  mounted.unmount();
  mount();
  await screen.findByRole("heading", { level: 1 });
  expect(screen.queryByLabelText("Real test of Chat")).toBeNull();
});

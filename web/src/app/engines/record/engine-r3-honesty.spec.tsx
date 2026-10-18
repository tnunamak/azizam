import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
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
  await user.click(more);
  await user.type(screen.getByLabelText("Extra path"), "/draft");
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
  await user.click(screen.getByText("More options", { selector: "summary span" }));
  await user.type(screen.getByLabelText("Extra path"), "/changed");
  complete({ ...testResult("chat_pass"), via: "saved" });
  await waitFor(() => expect(screen.queryByText(/Tested the saved settings/)).toBeNull());
  expect(screen.queryByLabelText("Real test of Chat")).toBeNull();
});

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { technicalWords } from "@/app/history/history-detail";
import { ViewerContext, type Viewer } from "@/components/shell/session";
import { hideHostPaths } from "@/lib/host-path-text";
import type { RequestHistoryRecord } from "@/lib/request-history";
import { HOST_PATH } from "../../../tests/helpers/host-path";
import { TechnicalDetails } from "./technical-details";

// Invented paths, joined here so no host path is spelled in one piece.
const modelFile = ["", "srv", "models", "LLMs", "GGUF", "Example-Q4_K_M.gguf"].join("/");
const homeFile = ["", "home", "alex", "models", "x.gguf"].join("/");
const url = ["https:", "", "example.com", "home", "alex"].join("/");

const record = {
  record_id: "rec_1",
  kind: "chat",
  created_at: 1_760_000_000,
  kept_until: null,
  origin_kind: null,
  request_id: "req_1",
  parts: {
    accepted: { model: "chat-a", messages: [{ role: "user", content: "hello" }] },
    received: { model: modelFile, error: { message: `failed to open ${homeFile}, giving up` }, link: url },
  },
  omissions: [],
  summary: { status: "failed" },
} as unknown as RequestHistoryRecord;

function shown(role: string): string {
  const viewer: Viewer = { authenticated: true, name: "Alex", email: null, role, admin: role === "owner" };
  render(
    <ViewerContext.Provider value={viewer}>
      <TechnicalDetails text={technicalWords(record, "Notes")} />
    </ViewerContext.Provider>,
  );
  return screen.getByText(/App: Notes/).textContent ?? "";
}

afterEach(cleanup);

it.each(["member", "admin", "some-new-role"])("cuts every host path to its file name for a %s and keeps the rest", (role) => {
  const text = shown(role);
  expect(text).not.toMatch(HOST_PATH);
  expect(text).toContain('"model": "Example-Q4_K_M.gguf"');
  expect(text).toContain("failed to open x.gguf, giving up");
  expect(text).toContain(url);
  expect(text).toContain('"content": "hello"');
});

it("shows the owner the record as it was kept", () => {
  const text = shown("owner");
  expect(text).toContain(modelFile);
  expect(text).toContain(homeFile);
});

it.each([
  { name: "a path at the end of the text", text: `read ${homeFile}`, expected: "read x.gguf" },
  { name: "a quoted path", text: `"${modelFile}"`, expected: '"Example-Q4_K_M.gguf"' },
  { name: "a bare home folder", text: `in ${["", "home", "alex"].join("/")} now`, expected: "in [folder] now" },
  { name: "a mount root with one folder", text: `on ${["", "mnt", "disk2"].join("/")}.`, expected: "on [folder]." },
  { name: "a path inside a URL", text: url, expected: url },
  { name: "a relative path", text: "models/home/x.gguf", expected: "models/home/x.gguf" },
])("hideHostPaths: $name", ({ text, expected }) => {
  expect(hideHostPaths(text)).toBe(expected);
});

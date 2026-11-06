import { describe, expect, it } from "vitest";
import path from "node:path";
import { lintText, loadTermList, type Role, type TextItem } from "../e2e/sweep/rules";

const lists = path.resolve(__dirname, "../e2e/sweep/lists");
const research = loadTermList(path.join(lists, "research-notes.txt"));
const jargon = loadTermList(path.join(lists, "member-jargon.txt"));
const item = (text: string, extra: Partial<TextItem> = {}): TextItem => ({ text, kind: "text", selector: "p", ref: 0, ...extra });
const rules = (text: string, role: Role = "member", extra: Partial<TextItem> = {}) =>
  lintText([item(text, extra)], { role, research, jargon }).map((finding) => finding.rule);

describe("sweep text rules", () => {
  it("flags filesystem paths but not app URLs", () => {
    expect(rules("/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Q4_K_M.gguf", "owner")).toContain("path");
    expect(rules("Choose ./source-image.bin", "owner")).toContain("path");
    expect(rules("C:\\Models\\x", "owner")).toContain("path");
    expect(rules("Open https://ai.household.example/", "owner")).not.toContain("path");
    expect(rules("Go to / to start", "owner")).not.toContain("path");
  });

  it("flags raw ids, softly when an owner sees a labelled request id", () => {
    expect(rules("req_household_02_03", "owner")).toContain("raw-id");
    expect(rules("3f2a9c1e-4b6d-4e8f-9a0b-1c2d3e4f5a6b", "owner")).toContain("raw-id");
    expect(rules("Version 20261009", "owner")).not.toContain("raw-id");
    const labelled = lintText([item("req_household_02_03", { label: "Request ID" })], { role: "owner", research, jargon });
    expect(labelled[0].severity).toBe("warn");
  });

  it("flags raw errors", () => {
    expect(rules("Request failed — Failed to fetch")).toContain("raw-error");
    expect(rules("source_image is required.")).toContain("raw-error");
    expect(rules("Cost: NaN")).toContain("raw-error");
    expect(rules("Picture is required.")).not.toContain("raw-error");
  });

  it("flags research notes and member jargon", () => {
    expect(rules("A Reddit thread calls this the piss filter", "owner")).toContain("research-note");
    expect(rules("Request body · string", "member")).toContain("member-jargon");
    expect(rules("Request body · string", "owner")).not.toContain("member-jargon");
    expect(rules("Make a picture", "member")).toEqual([]);
  });

  it("fails short Unknown states and warns on prose", () => {
    expect(rules("Chat unknown", "owner")).toContain("unknown");
    expect(rules("1 unknown", "owner")).toContain("unknown");
    const prose = lintText([item("Cost measured for 34 requests. 21 paid requests have unknown cost.")], { role: "owner", research, jargon });
    expect(prose.find((finding) => finding.rule === "unknown")?.severity).toBe("warn");
    const long = "We could not measure this engine yet because nothing has used it since the gateway started, so its state is unknown for now.";
    expect(lintText([item(long)], { role: "owner", research, jargon }).filter((finding) => finding.severity === "fail")).toEqual([]);
  });
});

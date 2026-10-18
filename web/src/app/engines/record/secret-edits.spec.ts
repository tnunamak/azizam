import { describe, expect, it } from "vitest";
import { secretDrafts, secretCommands, secretProblem } from "./secret-edits";
const record = {
  config: {
    headers: [{ id: "ent_header", name: "X-Tenant", configured: true }],
    base_url: "https://example.test",
    base_url_entries: { userinfo: { id: "ent_url", configured: true } },
    env: { TOKEN: { id: "ent_env", configured: true } },
  },
  process: { start: ["runner", "--key", { id: "ent_arg", configured: true }] },
};
describe("write-only carrier drafts", () => {
  it("shows configured metadata with empty inputs and omits untouched commands", () => {
    const draft = secretDrafts(record);
    expect(
      draft.find((d) => d.carrier === "config.headers")?.rows[0],
    ).toMatchObject({ id: "ent_header", value: "", configured: true });
    expect(secretCommands(draft)).toEqual([]);
  });
  it("uses stable ids for header replace and explicit clear", () => {
    const draft = secretDrafts(record);
    const header = draft.find((d) => d.carrier === "config.headers")!;
    header.rows[0].value = "new private bytes";
    expect(secretCommands(draft)).toContainEqual({
      carrier: "config.headers",
      operation: "replace",
      entry_id: "ent_header",
      value: "new private bytes",
    });
    header.rows[0].value = "";
    header.rows[0].removed = true;
    expect(secretCommands(draft)).toContainEqual({
      carrier: "config.headers",
      operation: "clear",
      entry_id: "ent_header",
    });
  });
  it("defaults env values to secret adds and keeps untouched entries by omission", () => {
    const draft = secretDrafts(record);
    const env = draft.find((d) => d.carrier === "config.env")!;
    env.rows.push({
      key: "new",
      name: "EXTRA",
      originalName: "",
      value: "neutral-value",
      secret: true,
    });
    expect(secretCommands(draft)).toEqual([
      {
        carrier: "config.env",
        operation: "add",
        name: "EXTRA",
        value: "neutral-value",
      },
    ]);
  });
  it("reorders argv by identity and clears only replaced or removed entries", () => {
    const draft = secretDrafts(record);
    const argv = draft.find((d) => d.carrier === "process.start")!;
    argv.rows = [argv.rows[0], argv.rows[2], argv.rows[1]];
    expect(secretCommands(draft)).toContainEqual({
      carrier: "process.start",
      operation: "set",
      items: [
        { literal: "runner" },
        { retain: "ent_arg" },
        { literal: "--key" },
      ],
      clear: [],
    });
    argv.rows[1].value = "replacement";
    expect(secretCommands(draft)).toContainEqual({
      carrier: "process.start",
      operation: "set",
      items: [
        { literal: "runner" },
        { secret: "replacement" },
        { literal: "--key" },
      ],
      clear: ["ent_arg"],
    });
  });
  it.each(["[redacted]", "<configured>", "<redacted>"])(
    "rejects reserved value %s",
    (value) => {
      const draft = secretDrafts(record);
      draft[0].rows[0].value = value;
      expect(secretProblem(draft)).toMatch(/reserved/i);
    },
  );
  it("requires a program", () => {
    const draft = secretDrafts(record);
    draft.find((d) => d.carrier === "process.start")!.rows[0].value = "";
    expect(secretProblem(draft)).toMatch(/program/i);
  });
  it("changes URL credentials only through explicit commands", () => {
    const draft = secretDrafts(record);
    const url = draft.find((d) => d.carrier === "config.base_url")!;
    expect(url.rows[0].value).toBe("");
    url.rows[0].value = "https://user:pass@example.test";
    expect(secretCommands(draft)).toEqual([
      {
        carrier: "config.base_url",
        operation: "set_credentials",
        url: "https://user:pass@example.test",
      },
    ]);
    url.rows[0].removed = true;
    expect(secretCommands(draft)).toEqual([
      { carrier: "config.base_url", operation: "clear_credentials" },
    ]);
  });
});
it("never sends URL credentials through the public endpoint field", async () => {
  const { endpointProblem } = await import("./secret-edits");
  expect(endpointProblem("https://user:password@example.test/v1")).toMatch(
    /credentials/i,
  );
  expect(endpointProblem("https://example.test/v1?token=private")).toMatch(
    /credentials/i,
  );
  expect(endpointProblem("https://example.test/v1#private")).toMatch(
    /credentials/i,
  );
  expect(endpointProblem("http://127.0.0.1:8080/v1")).toBeNull();
});
it("edits canonical command fields without offering ignored compatibility copies", () => {
  const drafts = secretDrafts({
    process: {
      driver: "command",
      start: ["runner"],
      legacy_lifecycle: {
        driver_config: {
          start: ["old-runner"],
          env: { OLD: { id: "ent_old", configured: true } },
        },
      },
    },
  });
  expect(drafts.map((d) => d.carrier)).toContain("process.start");
  expect(drafts.map((d) => d.carrier)).not.toContain(
    "process.legacy_lifecycle.driver_config.start",
  );
  expect(drafts.map((d) => d.carrier)).not.toContain(
    "process.legacy_lifecycle.driver_config.env",
  );
  expect(drafts.map((d) => d.carrier)).toContain("process.stop");
  expect(drafts.map((d) => d.carrier)).toContain("process.status");
});

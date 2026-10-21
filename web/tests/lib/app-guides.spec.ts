import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { chatAlias, transcriptionAlias } from "@/app/_home/chat-alias";
import {
  appGuides,
  keyAppById,
  keyApps,
  mcpApps,
  resolveKeyGuide,
  resolveMcpGuide,
  suggestedModel,
  type GuideValues,
} from "@/lib/app-guides";

const values: GuideValues = {
  openai_url: "https://gateway.test/openai/v1",
  anthropic_url: "https://gateway.test/anthropic",
  mcp_url: "https://gateway.test/mcp",
  chat_model: "house-chat",
  transcription_model: "house-whisper",
};
const known = [
  "openai_url",
  "anthropic_url",
  "mcp_url",
  "chat_model",
  "transcription_model",
];

function placeholdersIn(text: string): string[] {
  // "${GATEWAY_API_KEY}" is an env reference in a snippet, not a placeholder.
  return [...text.matchAll(/(?<!\$)\{([a-z][a-z_]*)\}/g)].map((match) => match[1]);
}

describe("App guide data", () => {
  it("gives every key app steps, fields, an https docs link and exactly one key field", () => {
    expect(keyApps.length).toBeGreaterThan(0);
    for (const app of keyApps) {
      const { key } = app;
      expect(key.steps.length, app.id).toBeGreaterThan(0);
      expect(key.fields.length, app.id).toBeGreaterThan(0);
      expect(key.docs, app.id).toMatch(/^https:\/\//);
      // OpenWhispr asks for a key in each of its two groups; every other app once.
      const keyFields = key.fields.filter((field) => field.value === "{key}");
      const groups = new Set(key.fields.map((field) => field.group ?? ""));
      expect(keyFields.length, app.id).toBe(groups.size);
      for (const group of groups)
        expect(
          keyFields.filter((field) => (field.group ?? "") === group),
          `${app.id} ${group}`,
        ).toHaveLength(1);
    }
  });

  it("uses only known placeholders", () => {
    const texts: string[] = [];
    for (const app of appGuides.apps) {
      for (const step of [...(app.key?.steps ?? []), ...(app.mcp?.steps ?? [])])
        texts.push(step);
      for (const field of app.key?.fields ?? []) texts.push(field.value);
      if (app.key?.snippet) texts.push(app.key.snippet.code);
    }
    for (const text of texts)
      for (const name of placeholdersIn(text)) {
        if (name === "key") continue;
        expect(known, `{${name}} in ${text}`).toContain(name);
      }
  });

  it("keeps ids unique, names no recipe, carries no gateway key and offers OpenWhispr but no Phone", () => {
    const ids = appGuides.apps.map((app) => app.id);
    expect(new Set(ids).size).toBe(ids.length);
    const text = JSON.stringify(appGuides);
    expect(text).not.toMatch(/recipe/i);
    expect(text).not.toMatch(/\b(opk|oak)_/);
    expect(keyAppById("openwhispr")?.name).toBe("OpenWhispr");
    expect(appGuides.apps.map((app) => app.name)).not.toContain("Phone");
  });

  it("marks third-party entries unverified and gives each MCP app an https docs link", () => {
    expect(keyApps.some((app) => app.key.status === "unverified")).toBe(true);
    for (const app of mcpApps) {
      expect(app.mcp.docs, app.id).toMatch(/^https:\/\//);
      expect(app.mcp.steps.length, app.id).toBeGreaterThan(0);
      expect(["sign-in", "sign-in-or-key"]).toContain(app.mcp.auth);
    }
  });
});

describe("resolveKeyGuide", () => {
  it("fills the address and model, marks each field and puts no key anywhere", () => {
    const guide = resolveKeyGuide(keyAppById("open-webui")!.key, values);
    const byLabel = Object.fromEntries(guide.fields.map((f) => [f.label, f]));
    expect(byLabel["URL"]).toMatchObject({ value: values.openai_url, isAddress: true, isModel: false, isKey: false });
    expect(byLabel["Model IDs"]).toMatchObject({ value: "house-chat", isModel: true, isAddress: false });
    expect(byLabel["API Key"]).toMatchObject({ isKey: true, value: "" });
    expect(guide.keyLabel).toBe("API Key");
    expect(guide.missingTasks).toEqual([]);
    // Claude Code takes the Anthropic address; Codex the OpenAI one.
    const claude = resolveKeyGuide(keyAppById("claude-code")!.key, values);
    expect(claude.fields.find((f) => f.isAddress)?.value).toBe(values.anthropic_url);
    expect(claude.snippet?.code).toContain(`'${values.anthropic_url}'`);
    expect(resolveKeyGuide(keyAppById("codex")!.key, values).fields.find((f) => f.isAddress)?.value).toBe(values.openai_url);
    for (const app of keyApps) {
      const resolved = resolveKeyGuide(app.key, values);
      expect(resolved.fields.filter((f) => f.isKey).every((f) => f.value === ""), app.id).toBe(true);
      const text = JSON.stringify(resolved);
      expect(text, app.id).not.toMatch(/\b(opk|oak)_/);
      expect(text, app.id).not.toMatch(/\{(openai_url|anthropic_url|mcp_url|chat_model|transcription_model)\}/);
    }
  });

  it("reports the model tasks a person has none of, per OpenWhispr group", () => {
    const whisper = keyAppById("openwhispr")!.key;
    const full = resolveKeyGuide(whisper, values);
    expect(full.fields.filter((f) => f.isModel).map((f) => [f.group, f.value])).toEqual([
      ["Speech-to-Text", "house-whisper"],
      ["Dictation Cleanup (optional)", "house-chat"],
    ]);
    const none = { ...values, chat_model: null, transcription_model: null };
    const missing = resolveKeyGuide(whisper, none);
    expect(missing.missingTasks.sort()).toEqual(["chat", "transcription"]);
    expect(missing.fields.filter((f) => f.isModel).every((f) => f.missing)).toBe(true);
    expect(resolveKeyGuide(whisper, { ...values, chat_model: null }).missingTasks).toEqual(["chat"]);
    expect(resolveKeyGuide(keyAppById("python")!.key, none).missingTasks).toEqual(["chat"]);
  });

  it("replaces an unsafe or missing value in a snippet but shows the real one in a field", () => {
    const unsafe = "x'$(printf unsafe)`printf unsafe`\"";
    const guide = resolveKeyGuide(keyAppById("python")!.key, {
      ...values,
      chat_model: unsafe,
      openai_url: `https://gateway.test/${unsafe}`,
    });
    expect(guide.snippet!.code).toContain("MODEL_NAME");
    expect(guide.snippet!.code).not.toContain("printf unsafe");
    expect(guide.fields.find((f) => f.isModel)?.value).toBe(unsafe);
    const absent = resolveKeyGuide(keyAppById("python")!.key, { ...values, chat_model: null });
    expect(absent.snippet!.code).toContain('model="MODEL_NAME"');
  });
});

describe.skipIf(process.platform === "win32")("Snippets run as shell", () => {
  const unsafe = "'$(printf unsafe)`printf unsafe`";
  const run = (script: string, env: Record<string, string> = {}) =>
    execFileSync("/bin/sh", ["-c", script], {
      env: { NODE_ENV: "test", GATEWAY_API_KEY: "synthetic-key", ...env },
      encoding: "utf8",
    })
      .trim()
      .split("\n");

  it("passes the curl URL and JSON as literal arguments, even for an unsafe model", () => {
    const guide = resolveKeyGuide(keyAppById("curl")!.key, {
      ...values,
      chat_model: unsafe,
    });
    const args = run('curl() { printf "%s\\n" "$@"; };\n' + guide.snippet!.code);
    expect(args[0]).toBe(`${values.openai_url}/chat/completions`);
    expect(JSON.parse(args.at(-1)!).model).toBe("MODEL_NAME");
    expect(args).toContain("Authorization: Bearer synthetic-key");
    expect(args.join("\n")).not.toContain("unsafe");
    const safe = resolveKeyGuide(keyAppById("curl")!.key, values);
    const safeArgs = run('curl() { printf "%s\\n" "$@"; };\n' + safe.snippet!.code);
    expect(JSON.parse(safeArgs.at(-1)!).model).toBe("house-chat");
  });

  it("sets Claude Code address and model without command substitution", () => {
    const guide = resolveKeyGuide(keyAppById("claude-code")!.key, {
      ...values,
      chat_model: unsafe,
      anthropic_url: `https://gateway.test/${unsafe}`,
    });
    const out = run(
      'claude() { printf "%s\\n" "$ANTHROPIC_BASE_URL" "$ANTHROPIC_MODEL" "$ANTHROPIC_DEFAULT_HAIKU_MODEL" "$ANTHROPIC_AUTH_TOKEN"; };\n' +
        guide.snippet!.code,
    );
    expect(out).toEqual(["MODEL_NAME", "MODEL_NAME", "MODEL_NAME", "synthetic-key"]);
  });
});

describe("resolveMcpGuide", () => {
  it("fills the MCP address in every MCP app's steps", () => {
    expect(mcpApps.some((app) => app.mcp.steps.some((step) => step.includes("{mcp_url}")))).toBe(true);
    for (const app of mcpApps)
      expect(resolveMcpGuide(app.mcp, values.mcp_url).steps.join("\n"), app.id).not.toContain("{mcp_url}");
    const claude = resolveMcpGuide(mcpApps.find((a) => a.id === "claude-code")!.mcp, values.mcp_url);
    expect(claude.steps[0]).toContain(`azizam ${values.mcp_url}`);
  });
});

describe("suggestedModel", () => {
  const row = (id: string, capability: string, extra = {}) => ({
    id,
    capability,
    capabilities: [{ capability }],
    ...extra,
  });
  it("prefers the recommended model, then a local one, then any", () => {
    expect(
      suggestedModel(
        [row("paid", "chat", { locality: "cloud" }), row("near", "chat", { locality: "local" }), row("pick", "chat", { recommended: true })],
        "chat",
      ),
    ).toBe("pick");
    expect(
      suggestedModel([row("paid", "chat"), row("near", "chat", { locality: "local" })], "chat"),
    ).toBe("near");
    expect(suggestedModel([row("paid", "chat")], "chat")).toBe("paid");
  });
  it("prefers a name that points at a model, and skips one known to be unavailable", () => {
    const pinned = row("default-fast", "chat", { locality: "local", recommended: true });
    expect(suggestedModel([pinned, row("fast", "chat", { alias: true })], "chat")).toBe("fast");
    expect(suggestedModel([pinned], "chat")).toBe("default-fast");
    expect(
      suggestedModel([row("fast", "chat", { alias: true, availability: { available: false } }), pinned], "chat"),
    ).toBe("default-fast");
    expect(
      suggestedModel([row("down", "chat", { alias: true, availability: { available: false } })], "chat"),
    ).toBeNull();
  });
  it("matches the task, reads it from the first capability, and returns null when there is none", () => {
    expect(suggestedModel([row("voice", "transcribe")], "chat")).toBeNull();
    expect(suggestedModel([row("voice", "transcribe")], "transcribe")).toBe("voice");
    expect(suggestedModel([], "chat")).toBeNull();
    expect(suggestedModel([{ id: "x", capabilities: [{ capability: "chat" }] }], "chat")).toBe("x");
  });
});

describe("Home model names", () => {
  const chat = { operation: "chat", available: true, status: "ready" as const, reason_code: "ready" };
  it("uses the gateway's recommended chat alias", () => {
    expect(chatAlias([])).toBeNull();
    expect(
      chatAlias([{ ...chat, id: "utility-check" }, { ...chat, id: "house-chat" }], "house-chat"),
    ).toBe("house-chat");
    // No name is special: "default" is an ordinary alias.
    expect(chatAlias([{ ...chat, id: "utility-check" }, { ...chat, id: "default" }])).toBe("utility-check");
  });
  it("prefers a permitted local chat alias, then any allowed one, skipping unavailable and non-chat aliases", () => {
    expect(chatAlias([{ ...chat, id: "paid", local: false }, { ...chat, id: "local-chat", local: true }])).toBe("local-chat");
    expect(chatAlias([{ ...chat, id: "paid", local: false }, { ...chat, id: "unknown" }])).toBe("paid");
    expect(chatAlias([{ ...chat, id: "off", local: true, available: false }, { ...chat, id: "on" }])).toBe("on");
    expect(chatAlias([{ ...chat, id: "images", operation: "images", local: true }])).toBeNull();
  });
  it("finds a transcription alias, local first", () => {
    const stt = { ...chat, operation: "audio.transcribe" };
    expect(transcriptionAlias([])).toBeNull();
    expect(
      transcriptionAlias([{ ...stt, id: "cloud-stt", local: false }, { ...stt, id: "local-stt", local: true }]),
    ).toBe("local-stt");
    expect(transcriptionAlias([chat as never])).toBeNull();
  });
});

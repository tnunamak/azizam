// node --test tests/showcase/core.test.mjs   (also run by tests/test_showcase.py when node exists)
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const C = require("../../showcase/core.js");
const sectionsDoc = JSON.parse(readFileSync(new URL("../../showcase/sections.json", import.meta.url)));

const wf = (id, intent, extra = {}) => ({ id, intent, display_name: id, description: "d. e. f.", typical_seconds: null, outputs: [], ...extra });
const openapi = {
  components: { schemas: { In: { type: "object", required: ["prompt"], properties: {
    prompt: { type: "string" }, seed: { type: "integer", minimum: 0 },
    source_image: { type: "string", "x-azizam-asset": true },
    width: { type: "integer", default: 832 }, height: { type: "integer", default: 480 },
    length: { type: "integer", minimum: 5 }, steps: { type: "integer" } } } } },
};
const oxSync = {
  id: "image.edit.demo", intent: "image.edit", inputs_schema: { $ref: "#/components/schemas/In" },
  route_inputs: [{ name: "n", type: "integer", in: "body", field: "n" }, { name: "size", type: "string", in: "form", field: "size" }],
  execution: { recommended: "sync", async: null, sync: { method: "POST", path: "/v1/images/edits", content_type: "multipart/form-data", model: "demo",
    fields: { prompt: { in: "form", field: "prompt" }, source_image: { in: "file", field: "image" }, steps: { in: "extension", field: "steps" } } } },
};
const oxVideo = {
  id: "video.edit.demo2", intent: "video.edit", inputs_schema: { $ref: "#/components/schemas/In" }, route_inputs: [],
  execution: { recommended: "async", sync: null, async: { kind: "video",
    submit: { method: "POST", path: "/v1/videos", content_type: "application/json", model: "video-demo2" },
    poll: { path: "/v1/videos/{video_id}" }, content: { path: "/v1/videos/{video_id}/content" }, cancel: { path: "/v1/videos/{video_id}" },
    terminal: ["completed", "failed"],
    fields: { prompt: { in: "body", field: "prompt" }, width: { in: "body", field: "size", combine: ["width", "height"] }, height: { in: "body", field: "size", combine: ["width", "height"] },
      length: { in: "body", field: "seconds" }, source_image: { in: "extension", field: "source_video" } } } },
};
const oxJob = {
  id: "audio.generate.demo3", intent: "audio.generate", inputs_schema: { $ref: "#/components/schemas/In" }, route_inputs: [],
  execution: { recommended: "async", sync: { method: "POST", path: "/v1/audio/generate", content_type: "application/json", model: "demo3", fields: { prompt: { in: "body", field: "prompt" } } },
    async: { kind: "media_job", submit: { method: "POST", path: "/gateway/v1/media/jobs", content_type: "application/json", model: "demo3" },
      poll: { path: "/gateway/v1/media/jobs/{job_id}" }, content: { path: "/gateway/v1/media/jobs/{job_id}/content" }, terminal: ["succeeded", "failed", "canceled"],
      fields: { prompt: { in: "body", field: "prompt" }, steps: { in: "extension", field: "steps" } } } },
};

test("classifyWorkflow follows section rules and falls back", () => {
  const cls = (id, intent) => C.classifyWorkflow(sectionsDoc.sections, wf(id, intent));
  assert.equal(cls("image.generate.krea2-turbo", "image.generate"), "images");
  assert.equal(cls("image.edit.seedvr2-upscale", "image.edit"), "image-edits");
  assert.equal(cls("video.generate.ltx-2.5-t2v", "video.generate"), "video-create");
  assert.equal(cls("video.generate.ltx-2.5-long", "video.generate"), "video-long");
  assert.equal(cls("video.edit.bernini-r-remix", "video.edit"), "video-edit");
  assert.equal(cls("video.edit.rife-interpolate", "video.edit"), "video-enhance");
  assert.equal(cls("audio.generate.ace-step-1.5-xl-turbo", "audio.generate"), "music");
  assert.equal(cls("audio.generate.yue2-song", "audio.generate"), "songs");
  assert.equal(cls("audio.generate.sa3-small-sfx", "audio.generate"), "sfx");
  assert.equal(cls("audio.generate.tangoflux", "audio.generate"), "sfx");
  // A brand-new intent still shows up.
  assert.equal(cls("depth.estimate.zoe", "depth.estimate"), "more");
});

test("buildSections is ordered by modality and shows new workflows automatically", () => {
  const ctx = {
    sectionsDoc, openapi: { ...openapi, "x-azizam-workflows": [oxSync] },
    workflows: [wf("video.generate.ltx-2.5-t2v", "video.generate", { typical_seconds: 45 }), wf("image.edit.demo", "image.edit"), wf("depth.estimate.zoe", "depth.estimate")],
    capabilities: { capabilities: [{ workflow: "demo", available: false }] },
    aliases: { operations: { chat: { stable: [{ alias: "default", resolves_to: "default" }, { alias: "vision", resolves_to: "default" }] }, embeddings: { stable: [{ alias: "embed", resolves_to: "nomic" }] },
      speech: { stable: [{ alias: "tts", resolves_to: "chatterbox", engine: "chatterbox" }] }, transcriptions: { stable: [{ alias: "stt", resolves_to: "parakeet" }, { alias: "stt-diarize", resolves_to: "nd" }] },
      videos: { stable: [{ alias: "video", resolves_to: "video-ltx-2.5-t2v" }] } } },
  };
  const secs = C.buildSections(ctx);
  const ids = secs.map((s) => s.id);
  assert.deepEqual(ids, ["chat", "image-edits", "video-create", "speech", "transcription", "embeddings", "protocols", "more"]);
  const cardIds = Object.fromEntries(secs.map((s) => [s.id, s.cards.map((c) => c.id)]));
  assert.deepEqual(cardIds.chat, ["chat", "vision"]);
  assert.deepEqual(cardIds.speech, ["tts", "voices", "custom-voices"]);
  assert.deepEqual(cardIds.transcription, ["stt", "stt-diarize"]);
  assert.equal(cardIds.protocols.length, 4);
  assert.deepEqual(cardIds.more, ["depth.estimate.zoe"]);
  const video = secs.find((s) => s.id === "video-create").cards[0];
  assert.deepEqual(video.aliases, ["video"]);
  assert.equal(video.typicalSeconds, 45);
  assert.equal(secs.find((s) => s.id === "image-edits").cards[0].available, false);
});

test("formFields reads the schema, marks assets, adds route inputs but not n", () => {
  const f = C.formFields(oxSync, openapi);
  const by = Object.fromEntries(f.map((x) => [x.name, x]));
  assert.equal(by.prompt.required, true);
  assert.equal(by.prompt.multiline, true);
  assert.equal(by.source_image.type, "asset");
  assert.equal(by.source_image.media, "image");
  assert.equal(by.seed.type, "integer");
  assert.equal(by.seed.min, 0);
  assert.ok(by.size && by.size.route);
  assert.equal(by.n, undefined);
  assert.equal(by.steps.primary, false);
  assert.equal(f[0].required, true);
});

test("planWorkflow: sync multipart maps form, file and extension fields", () => {
  const plan = C.planWorkflow({ ox: oxSync }, { prompt: "p", steps: 5, size: "512x512", source_image: { filename: "a.png", dataUri: "data:x" } });
  assert.equal(plan.mode, "sync");
  assert.equal(plan.path, "/v1/images/edits");
  assert.equal(plan.contentType, "multipart/form-data");
  const names = plan.form.map((p) => p.name);
  assert.deepEqual(names, ["model", "prompt", "image", "size", "extensions", "response_format"].filter((n) => names.includes(n)));
  assert.ok(plan.form.find((p) => p.name === "image").file);
  assert.deepEqual(JSON.parse(plan.form.find((p) => p.name === "extensions").value), { [C.EXT_KEY]: { steps: 5 } });
  assert.equal(plan.form.find((p) => p.name === "model").value, "demo");
});

test("planWorkflow: async video combines size, converts length to seconds, inlines files", () => {
  const plan = C.planWorkflow({ ox: oxVideo }, { prompt: "p", width: 832, height: 480, length: 48, source_image: { filename: "a.mp4", dataUri: "data:video/mp4;base64,AA" } });
  assert.equal(plan.mode, "async");
  assert.equal(plan.json.model, "video-demo2");
  assert.equal(plan.json.size, "832x480");
  assert.equal(plan.json.seconds, 2);
  assert.deepEqual(plan.json.extensions[C.EXT_KEY], { source_video: "data:video/mp4;base64,AA" });
  assert.equal(plan.job.kind, "video");
  assert.deepEqual(plan.job.ok, ["completed"]);
  assert.equal(C.fillPath(plan.job.poll, "id", "video_1"), "/v1/videos/video_1");
  // Only one of width/height given: size is omitted instead of half-built.
  assert.equal(C.planWorkflow({ ox: oxVideo }, { prompt: "p", width: 832 }).json.size, undefined);
});

test("planWorkflow: media_job wraps the sync-shaped request and honors mode override", () => {
  const plan = C.planWorkflow({ ox: oxJob }, { prompt: "boom", steps: 3 });
  assert.equal(plan.path, "/gateway/v1/media/jobs");
  assert.equal(plan.json.intent, "audio.generate");
  assert.deepEqual(plan.json.request, { model: "demo3", prompt: "boom", extensions: { [C.EXT_KEY]: { steps: 3 } } });
  assert.deepEqual(plan.job.ok, ["succeeded"]);
  const sync = C.planWorkflow({ ox: oxJob }, { prompt: "boom" }, "sync");
  assert.equal(sync.path, "/v1/audio/generate");
  assert.equal(sync.job, undefined);
});

test("snippets reflect the plan and never embed an inline file body", () => {
  const plan = C.planWorkflow({ ox: oxSync }, { prompt: "it's", source_image: { filename: "a.png", dataUri: "data:" + "A".repeat(500) } });
  const curl = C.curlFor(plan, "http://h");
  assert.match(curl, /\/v1\/images\/edits/);
  assert.match(curl, /-F 'image=@a\.png'/);
  assert.match(curl, /prompt=it'\\''s/);
  assert.match(curl, /Authorization: Bearer \$AZIZAM_API_KEY/);
  const jplan = C.planWorkflow({ ox: oxVideo }, { prompt: "p", source_image: { filename: "a.mp4", dataUri: "data:video/mp4;base64," + "A".repeat(500) } });
  assert.ok(C.curlFor(jplan, "http://h").length < 900);
  const py = C.pythonFor(jplan, "http://h");
  assert.match(py, /requests\.post\(BASE \+ "\/v1\/videos"/);
  assert.match(py, /job\["id"\]/);
  assert.match(C.jsFor(plan, "http://h"), /new FormData\(\)/);
});

test("buildSections shows public recorded examples when discovery needs a key", () => {
  const ctx = {
    sectionsDoc, openapi: {}, workflows: [], capabilities: { capabilities: [] }, aliases: { operations: {} },
    exampleCards: ["chat", "voices", "image.generate.flux-dev", "video.generate.ltx-t2v", "protocol-openai", "nope"],
    live: false,
  };
  const secs = C.buildSections(ctx);
  const cardIds = Object.fromEntries(secs.map((s) => [s.id, s.cards.map((c) => c.id)]));
  assert.deepEqual(cardIds.chat, ["chat"]);
  assert.deepEqual(cardIds.speech, ["voices"]);
  assert.deepEqual(cardIds.images, ["image.generate.flux-dev"]);
  assert.deepEqual(cardIds["video-create"], ["video.generate.ltx-t2v"]);
  assert.deepEqual(cardIds.protocols, ["protocol-openai"]);
  assert.equal(secs.find((s) => s.id === "images").cards[0].kind, "recorded");
  assert.equal(secs.find((s) => s.id === "images").cards[0].title, "flux-dev");
});

test("statusSummary tells busy from broken", () => {
  assert.equal(C.statusSummary(null, null).level, "warn");
  assert.equal(C.statusSummary({}, { comfyui: { running: 1, pending: 2 }, llm: { processing: false } }).level, "busy");
  assert.equal(C.statusSummary({}, { comfyui: { running: 1, pending: 2 }, llm: { processing: false } }).text, "Busy · 1 rendering, 2 queued");
  assert.deepEqual(C.statusSummary({}, { comfyui: { pending: 2 } }), { level: "busy", text: "Busy · 0 rendering, 2 queued" });
  assert.deepEqual(C.statusSummary({}, { llm: { processing: true } }), { level: "busy", text: "Busy · chat processing" });
  assert.equal(C.statusSummary({}, { comfyui: { running: 1, pending: 2 }, llm: { processing: true } }).text,
    "Busy · 1 rendering, 2 queued; chat processing");
  assert.deepEqual(C.statusSummary({}, { engines: [{ status: "off" }] }), { level: "warn", text: "Some engines are off" });
  assert.deepEqual(C.statusSummary({}, { engines: [{ status: "available" }] }), { level: "ok", text: "Ready" });
  assert.equal(C.statusSummary({}, { comfyui: { running: 0, pending: 0 }, llm: { processing: false } }).level, "ok");
  assert.equal(C.statusSummary({}, { comfyui: { running: 0, pending: 0 }, llm: { processing: true } }).level, "busy");
  assert.equal(C.statusSummary({}, null).level, "unknown");
});

test("examplesByCard groups and orders", () => {
  const g = C.examplesByCard([{ id: "b", card: "x", order: 1 }, { id: "a", card: "x", order: 0 }, { id: "c", card: "y" }]);
  assert.deepEqual(g.x.map((e) => e.id), ["a", "b"]);
  assert.deepEqual(Object.keys(g).sort(), ["x", "y"]);
});

test("isSameOrigin: only the gateway origin may receive the page-memory API key", () => {
  const base = "https://gw.example:5000/showcase";
  for (const ok of ["/v1/videos/1/content", "v1/x", "https://gw.example:5000/files/a.png", "?q=1", ""]) {
    assert.equal(C.isSameOrigin(ok, base), true, ok);
  }
  for (const bad of [
    "https://attacker.example/exfil", "http://gw.example:5000/x", "https://gw.example/x",
    "https://gw.example:5000.attacker.example/x", "https://gw.example:5000@attacker.example/x",
    "//attacker.example/x", "/\\attacker.example/x", "\\\\attacker.example\\x",
    "data:text/plain,hi", "javascript:alert(1)", "blob:https://attacker.example/1",
  ]) {
    assert.equal(C.isSameOrigin(bad, base), false, JSON.stringify(bad));
  }
  assert.equal(C.isSameOrigin("/x", "not a url"), false);
});

test("app.js api() attaches the Bearer key only after the same-origin check", () => {
  const src = readFileSync(new URL("../../showcase/app.js", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("async function api("), src.indexOf("async function apiJson("));
  assert.match(body, /if \(C\.isSameOrigin\(path, location\.href\)\) o\.headers = authHeaders\(o\.headers\)/);
  assert.doesNotMatch(body.replace(/if \(C\.isSameOrigin[^\n]*\n/, ""), /authHeaders\(/);
  assert.match(body, /credentials = "omit"/);
});

test("announcements suppress repeated timer and streaming status updates", () => {
  const messages = [];
  const announce = C.createAnnouncer((message) => messages.push(message));
  assert.equal(announce("Rendering"), true);
  for (let i = 0; i < 100; i++) assert.equal(announce("Rendering"), false);
  announce("Downloading result");
  announce("Request finished");
  assert.deepEqual(messages, ["Rendering", "Downloading result", "Request finished"]);
  announce("");
  assert.equal(announce("Rendering"), true);
});

test("tabs support arrows, wraparound, Home and End without trapping other keys", () => {
  assert.equal(C.tabIndexForKey("ArrowRight", 2, 3), 0);
  assert.equal(C.tabIndexForKey("ArrowLeft", 0, 3), 2);
  assert.equal(C.tabIndexForKey("Home", 2, 3), 0);
  assert.equal(C.tabIndexForKey("End", 0, 3), 2);
  assert.equal(C.tabIndexForKey("Tab", 1, 3), null);
  assert.equal(C.tabIndexForKey("ArrowRight", 0, 0), null);
});

test("key controls use memory only, tolerate denied storage, and restore focus", () => {
  const src = readFileSync(new URL("../../showcase/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(src, /localStorage\.(?:getItem|setItem)|sessionStorage|location\.reload/);
  const nodes = {};
  for (const id of ["apikey", "keypanel", "keytoggle", "keysave", "keyclear", "base", "status-text", "status"]) {
    nodes["#" + id] = {
      value: "", hidden: id === "keypanel", attrs: {}, events: {},
      setAttribute(k, v) { this.attrs[k] = v; },
      removeAttribute(k) { delete this.attrs[k]; },
      addEventListener(k, fn) { this.events[k] = fn; },
      focus() { nodes.focus = id; },
      click() { this.events.click(); },
    };
  }
  const body = src.slice(src.indexOf("  function initKey()"), src.indexOf("  initKey();"));
  let loads = 0;
  const run = new Function("$", "localStorage", "loadAll", "pollStatus",
    'let pageKey = ""; const KEY_STORE = "legacy"; const BASE = "https://example.test"; ' +
    body + '; initKey(); return () => pageKey;');
  const key = run((id) => nodes[id], { removeItem() { throw new Error("Storage blocked"); } }, () => loads++, () => {});
  nodes["#keytoggle"].click();
  assert.equal(nodes["#keypanel"].hidden, false);
  assert.equal(nodes.focus, "apikey");
  nodes["#apikey"].value = " dummy-test-value ";
  nodes["#keysave"].click();
  assert.equal(key(), "dummy-test-value");
  assert.equal(nodes["#apikey"].value, "");
  assert.equal(nodes["#keytoggle"].attrs["aria-expanded"], "false");
  assert.equal(nodes.focus, "keytoggle");
  nodes["#keyclear"].click();
  assert.equal(key(), "");
  assert.equal(loads, 2);
});

test("result live region excludes output, timers and streamed tokens", () => {
  const src = readFileSync(new URL("../../showcase/app.js", import.meta.url), "utf8");
  const result = src.slice(src.indexOf("  function resultBox()"), src.indexOf("  /** Run a thing"));
  assert.match(result, /class: "result" \}/);
  assert.match(result, /role: "status", "aria-live": "polite"/);
  assert.match(result, /box.append\(live, content\)/);
  assert.match(result, /C.createAnnouncer/);
  assert.match(src, /announcement: text/);
  assert.match(src, /if \(statusText.textContent !== sum.text\)/);
});

test("media and panel contracts support lazy loading and keyboard recovery", () => {
  const src = readFileSync(new URL("../../showcase/app.js", import.meta.url), "utf8");
  assert.match(src, /loading: "lazy", decoding: "async"/);
  assert.doesNotMatch(src, /preload: "metadata"/);
  assert.match(src, /preload: "none"/);
  assert.match(src, /event.key !== "Escape"/);
  assert.match(src, /panel._returnFocus.isConnected/);
  assert.match(src, /"aria-controls": id/);
  assert.match(src, /button.tabIndex = i === index \? 0 : -1/);
  assert.match(src, /version !== loadVersion/);
  assert.match(src, /Retry discovery/);
});

test("app parses without executing discovery or generation requests", () => {
  const src = readFileSync(new URL("../../showcase/app.js", import.meta.url), "utf8");
  assert.doesNotThrow(() => new Function(src));
});

test("Try it Escape returns focus to the opener without intercepting other keys", () => {
  const src = readFileSync(new URL("../../showcase/app.js", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("  function focusPanel("), src.indexOf("  /** Build the code tabs"));
  let focused = null, prevented = 0, stopped = 0;
  const summary = { focus() { focused = "summary"; } };
  const field = { focus() { focused = "field"; } };
  const opener = { isConnected: true, focus() { focused = "opener"; } };
  const doc = { activeElement: summary };
  const events = {};
  const panel = {
    open: true, _returnFocus: null,
    querySelector(sel) { return sel === "summary" ? summary : field; },
    addEventListener(k, fn) { events[k] = fn; },
    contains(el) { return el === summary || el === field; },
  };
  new Function("document", "panel", body + "; managePanel(panel);")(doc, panel);
  events.toggle();
  assert.equal(focused, "field");
  panel._returnFocus = opener;
  events.keydown({ key: "Tab" });
  assert.equal(panel.open, true);
  events.keydown({ key: "Escape", preventDefault() { prevented++; }, stopPropagation() { stopped++; } });
  assert.equal(panel.open, false);
  assert.equal(focused, "opener");
  assert.equal(prevented, 1);
  assert.equal(stopped, 1);
  panel.open = true; panel._returnFocus = { isConnected: false };
  events.keydown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  assert.equal(focused, "summary");
});

test("recordedParts keeps recorded inputs and marks outputs it cannot show", () => {
  assert.deepEqual(C.recordedParts(null), { inputs: [], outputs: [null] });
  assert.deepEqual(C.recordedParts({ inputs: null, outputs: [] }), { inputs: [], outputs: [null] });
  const text = { kind: "text", text: "hi" };
  const image = { kind: "image", file: "a.png" };
  const json = { kind: "json", value: { a: 1 } };
  const parts = C.recordedParts({
    inputs: [null, { value: null }, { value: "  " }, { value: 0 }, { value: false }, { kind: "image", file: "../x.png" }, { kind: "image", file: "in.png" }],
    outputs: [null, { kind: "text", text: "  " }, { kind: "image" }, { kind: "image", file: "../private.png" }, { kind: "json", value: null }, text, image, json],
  });
  assert.deepEqual(parts.inputs, [{ value: 0 }, { value: false }, { kind: "image", file: "in.png" }]);
  assert.deepEqual(parts.outputs, [null, null, null, null, null, text, image, json]);
});

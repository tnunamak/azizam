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
    source_image: { type: "string", "x-vividfish-asset": true },
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
    sectionsDoc, openapi: { ...openapi, "x-vividfish-workflows": [oxSync] },
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
  assert.match(curl, /Authorization: Bearer \$OPENAI_PROXY_API_KEY/);
  const jplan = C.planWorkflow({ ox: oxVideo }, { prompt: "p", source_image: { filename: "a.mp4", dataUri: "data:video/mp4;base64," + "A".repeat(500) } });
  assert.ok(C.curlFor(jplan, "http://h").length < 900);
  const py = C.pythonFor(jplan, "http://h");
  assert.match(py, /requests\.post\(BASE \+ "\/v1\/videos"/);
  assert.match(py, /job\["id"\]/);
  assert.match(C.jsFor(plan, "http://h"), /new FormData\(\)/);
});

test("statusSummary tells busy from broken", () => {
  assert.equal(C.statusSummary(null, null).level, "warn");
  assert.equal(C.statusSummary({}, { comfyui: { running: 1, pending: 2 }, llm: { processing: false } }).level, "busy");
  assert.match(C.statusSummary({}, { comfyui: { running: 1, pending: 2 }, llm: { processing: false } }).text, /1 rendering, 2 queued/);
  assert.equal(C.statusSummary({}, { comfyui: { running: 0, pending: 0 }, llm: { processing: false } }).level, "ok");
  assert.equal(C.statusSummary({}, { comfyui: { running: 0, pending: 0 }, llm: { processing: true } }).level, "busy");
  assert.equal(C.statusSummary({}, null).level, "unknown");
});

test("examplesByCard groups and orders", () => {
  const g = C.examplesByCard([{ id: "b", card: "x", order: 1 }, { id: "a", card: "x", order: 0 }, { id: "c", card: "y" }]);
  assert.deepEqual(g.x.map((e) => e.id), ["a", "b"]);
  assert.deepEqual(Object.keys(g).sort(), ["x", "y"]);
});

/* Showcase core: pure logic, no DOM, no network.
 *
 * Turns the gateway's discovery documents into section/card models, form
 * fields, request plans and copy-paste snippets. app.js does the DOM and the
 * fetches. Tests: tests/showcase_core.test.mjs (node --test).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.ShowcaseCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const EXT_KEY = "com.vividfish.workflow";
  const PRIMARY = new Set([
    "prompt", "tags", "lyrics", "duration", "width", "height", "length", "seed",
    "scale", "multiplier", "negative_prompt", "size", "seconds", "image_strength",
    "source_subject", "reference_subject",
  ]);
  const MULTILINE = new Set(["prompt", "negative_prompt", "lyrics", "tags"]);

  // ---------------------------------------------------------------- sections

  function shortId(wf) {
    if (wf.short_id) return wf.short_id;
    const i = wf.id.indexOf(".", wf.id.indexOf(".") + 1);
    return i < 0 ? wf.id : wf.id.slice(i + 1);
  }

  function ruleMatches(rule, wf) {
    if (!rule) return false;
    const sid = shortId(wf);
    if (rule.intent && rule.intent !== wf.intent) return false;
    if (rule.id && !new RegExp(rule.id).test(sid)) return false;
    if (rule.not_id && new RegExp(rule.not_id).test(sid)) return false;
    return true;
  }

  /** First section whose rule matches, else the fallback section. */
  function classifyWorkflow(sections, wf) {
    for (const s of sections) if (!s.fallback && ruleMatches(s.match, wf)) return s.id;
    const fb = sections.find((s) => s.fallback);
    return fb ? fb.id : null;
  }

  // ----------------------------------------------------------------- schemas

  function resolveRef(openapi, node) {
    let cur = node;
    for (let i = 0; cur && cur.$ref && i < 8; i++) {
      const parts = cur.$ref.replace(/^#\//, "").split("/");
      cur = parts.reduce((o, k) => (o ? o[k] : undefined), openapi);
    }
    return cur || {};
  }

  function assetKind(name) {
    if (/video/i.test(name)) return "video";
    if (/audio|voice|speech/i.test(name)) return "audio";
    return "image";
  }

  /** Form fields from a workflow's inputs schema plus its route inputs. */
  function formFields(ox, openapi) {
    const schema = resolveRef(openapi, ox.inputs_schema || {});
    const props = schema.properties || {};
    const required = new Set(schema.required || []);
    const fields = [];
    for (const [name, p0] of Object.entries(props)) {
      const p = resolveRef(openapi, p0);
      const asset = !!p["x-vividfish-asset"];
      fields.push({
        name,
        type: asset ? "asset" : p.type === "integer" || p.type === "number" ? p.type : p.enum ? "enum" : "string",
        required: required.has(name),
        min: p.minimum, max: p.maximum, def: p.default, enum: p.enum,
        description: p.description || "",
        media: asset ? assetKind(name) : null,
        multiline: !asset && MULTILINE.has(name),
        primary: required.has(name) || PRIMARY.has(name) || asset,
      });
    }
    for (const r of ox.route_inputs || []) {
      if (r.name === "n" || props[r.name]) continue;
      fields.push({
        name: r.name,
        type: r.type === "integer" || r.type === "number" ? r.type : "string",
        required: false, min: r.minimum, max: r.maximum, def: undefined, enum: undefined,
        description: r.description || "", media: null, multiline: false, primary: true,
        route: true,
      });
    }
    fields.sort((a, b) => Number(!!b.required) - Number(!!a.required));
    return fields;
  }

  // ------------------------------------------------------------ request plan

  function isBlank(v) {
    return v === undefined || v === null || v === "" || (typeof v === "number" && Number.isNaN(v));
  }

  function isFile(v) {
    return v && typeof v === "object" && ("dataUri" in v || "blob" in v);
  }

  /**
   * Request plan for one workflow.
   *
   * entry: {ox} (an x-vividfish-workflows item, with execution).
   * values: field name -> string | number | {filename, type, dataUri|blob}.
   * mode: "sync" | "async"; default is the workflow's recommended mode.
   *
   * Plan: {mode, method, path, contentType, model, json?, form?, job?}
   * form is a list of {name, value} or {name, file}. job names the polling
   * protocol ("media_job" or "video") and its paths.
   */
  function planWorkflow(entry, values, mode) {
    const ex = entry.ox.execution || {};
    const chosen = mode || ex.recommended || (ex.sync ? "sync" : "async");
    const useAsync = chosen === "async" && ex.async;
    const spec = useAsync ? ex.async : ex.sync;
    if (!spec) throw new Error("workflow has no " + chosen + " execution");
    const submit = useAsync ? spec.submit : spec;
    const fields = spec.fields || {};
    const multipart = submit.content_type === "multipart/form-data";
    const body = {};
    const ext = {};
    const form = [];
    const route = {};
    for (const r of entry.ox.route_inputs || []) route[r.name] = r;
    const combined = {};

    const put = (where, field, value, isAsset) => {
      if (where === "body" || where === "form") {
        if (multipart) form.push(isAsset && isFile(value) ? { name: field, file: value } : { name: field, value: String(value) });
        else body[field] = isAsset && isFile(value) ? value.dataUri : value;
      } else if (where === "extension") {
        ext[field] = isAsset && isFile(value) ? (multipart ? { multipart_field: field } : value.dataUri) : value;
        if (multipart && isAsset && isFile(value)) form.push({ name: field, file: value });
      } else if (where === "file") {
        form.push({ name: field, file: value });
      }
    };

    for (const [name, f] of Object.entries(fields)) {
      let v = values[name];
      if (isBlank(v) || f.in === "none") continue;
      if (f.combine) {
        combined[f.field] = combined[f.field] || {};
        combined[f.field][name] = v;
        continue;
      }
      let field = f.field;
      if (useAsync && spec.kind === "video" && name === "length" && field === "seconds") v = Number(v) / 24;
      put(f.in, field, v, isFile(v));
    }
    for (const [target, parts] of Object.entries(combined)) {
      const order = (fields[Object.keys(parts)[0]].combine) || Object.keys(parts);
      if (order.every((k) => !isBlank(parts[k]))) {
        const joined = order.map((k) => parts[k]).join("x");
        const where = fields[order[0]].in;
        put(where, target, joined, false);
      }
    }
    for (const [name, r] of Object.entries(route)) {
      const v = values[name];
      if (isBlank(v) || name === "n" || fields[name]) continue;
      put(r.in || "body", r.field || name, v, false);
    }

    const plan = {
      mode: useAsync ? "async" : "sync",
      method: submit.method || "POST",
      path: submit.path,
      contentType: submit.content_type || "application/json",
      model: submit.model || null,
    };
    const imageRoute = /^\/v1\/images\//.test(plan.path);
    if (multipart) {
      if (plan.model) form.unshift({ name: "model", value: plan.model });
      if (Object.keys(ext).length) form.push({ name: "extensions", value: JSON.stringify({ [EXT_KEY]: ext }) });
      if (imageRoute) form.push({ name: "response_format", value: "b64_json" });
      plan.form = form;
    } else {
      const json = {};
      if (plan.model) json.model = plan.model;
      Object.assign(json, body);
      if (Object.keys(ext).length) json.extensions = { [EXT_KEY]: ext };
      if (imageRoute) json.response_format = "b64_json";
      plan.json = json;
    }
    if (useAsync && spec.kind === "media_job") {
      plan.job = {
        kind: "media_job",
        poll: spec.poll.path, content: spec.content.path, cancel: spec.cancel && spec.cancel.path,
        terminal: spec.terminal, ok: ["succeeded"],
      };
      plan.json = { intent: entry.ox.intent, request: plan.json };
      plan.model = submit.model;
    } else if (useAsync) {
      plan.job = {
        kind: "video", poll: spec.poll.path, content: spec.content.path,
        cancel: spec.cancel && spec.cancel.path, terminal: spec.terminal, ok: ["completed"],
      };
    }
    return plan;
  }

  function fillPath(path, key, id) {
    return path.replace(/\{[^}]+\}/, encodeURIComponent(id));
  }

  // ---------------------------------------------------------------- snippets

  function shQuote(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }

  const AUTH = '-H "Authorization: Bearer $OPENAI_PROXY_API_KEY"';

  function clip(value, max) {
    if (typeof value === "string" && value.length > max) return value.slice(0, 40) + "...";
    return value;
  }

  function jsonForDisplay(obj) {
    return JSON.stringify(obj, (k, v) => clip(v, 120), 2);
  }

  function curlFor(plan, base) {
    const lines = [];
    const out = plan.job ? "" : "";
    if (plan.contentType === "multipart/form-data") {
      lines.push(`curl -sS ${base}${plan.path} ${AUTH}`);
      for (const p of plan.form) {
        if (p.file) lines.push(`  -F ${shQuote(p.name + "=@" + (p.file.filename || "input"))}`);
        else lines.push(`  -F ${shQuote(p.name + "=" + p.value)}`);
      }
    } else {
      lines.push(`curl -sS ${base}${plan.path} ${AUTH}`);
      lines.push(`  -H 'Content-Type: application/json'`);
      lines.push(`  -d ${shQuote(jsonForDisplay(plan.json))}`);
    }
    let text = lines.join(" \\\n");
    if (plan.job) {
      const poll = plan.job.poll.replace(/\{[^}]+\}/, "$JOB_ID");
      const content = plan.job.content.replace(/\{[^}]+\}/, "$JOB_ID");
      text += `\n\n# poll until status is ${plan.job.ok[0]} (or failed), then download\ncurl -sS ${base}${poll} ${AUTH}\ncurl -sS ${base}${content} ${AUTH} -o output`;
    } else if (plan.path.startsWith("/gateway/v1/workflows/") || /audio\/generate|videos/.test(plan.path)) {
      text += " \\\n  -o output";
    }
    return text;
  }

  function pyLiteral(v, indent) {
    const pad = "    ".repeat(indent);
    if (v === null) return "None";
    if (typeof v === "boolean") return v ? "True" : "False";
    if (typeof v === "number") return String(v);
    if (typeof v === "string") return JSON.stringify(clip(v, 120));
    if (Array.isArray(v)) return "[" + v.map((x) => pyLiteral(x, indent)).join(", ") + "]";
    const entries = Object.entries(v);
    if (!entries.length) return "{}";
    return "{\n" + entries.map(([k, x]) => `${pad}    ${JSON.stringify(k)}: ${pyLiteral(x, indent + 1)},`).join("\n") + `\n${pad}}`;
  }

  function pythonFor(plan, base) {
    const head = 'import os, requests\n\nBASE = "' + base + '"\nHEADERS = {"Authorization": "Bearer " + os.environ["OPENAI_PROXY_API_KEY"]}\n\n';
    let call;
    if (plan.contentType === "multipart/form-data") {
      const data = {}, files = [];
      for (const p of plan.form) {
        if (p.file) files.push(`    "${p.name}": open(${JSON.stringify(p.file.filename || "input")}, "rb"),`);
        else data[p.name] = p.value;
      }
      call = `r = requests.post(BASE + "${plan.path}", headers=HEADERS,\n    data=${pyLiteral(data, 1)},\n    files={\n${files.join("\n")}\n    })\n`;
    } else {
      call = `r = requests.post(BASE + "${plan.path}", headers=HEADERS,\n    json=${pyLiteral(plan.json, 1)})\n`;
    }
    call += "r.raise_for_status()\n";
    if (plan.job) {
      const id = plan.job.kind === "media_job" ? "job_id" : "id";
      call += `job = r.json()\nimport time\nwhile True:  # poll until done\n    s = requests.get(BASE + "${plan.job.poll.replace(/\{[^}]+\}/, '" + job["' + id + '"] + "')}", headers=HEADERS).json()\n    if s["status"] in ${JSON.stringify(plan.job.terminal)}: break\n    time.sleep(5)\nopen("output", "wb").write(requests.get(BASE + "${plan.job.content.replace(/\{[^}]+\}/, '" + job["' + id + '"] + "')}", headers=HEADERS).content)\n`;
    } else {
      call += 'print(r.headers.get("content-type"), len(r.content), "bytes")\n';
    }
    return head + call;
  }

  function jsFor(plan, base) {
    const lines = ['const BASE = "' + base + '";', 'const headers = { Authorization: "Bearer " + process.env.OPENAI_PROXY_API_KEY };'];
    if (plan.contentType === "multipart/form-data") {
      lines.push("const form = new FormData();");
      for (const p of plan.form) {
        if (p.file) lines.push(`form.append(${JSON.stringify(p.name)}, new Blob([fs.readFileSync(${JSON.stringify(p.file.filename || "input")})]), ${JSON.stringify(p.file.filename || "input")});`);
        else lines.push(`form.append(${JSON.stringify(p.name)}, ${JSON.stringify(p.value)});`);
      }
      lines.push(`const res = await fetch(BASE + ${JSON.stringify(plan.path)}, { method: "POST", headers, body: form });`);
    } else {
      lines.push(`const res = await fetch(BASE + ${JSON.stringify(plan.path)}, {\n  method: "POST",\n  headers: { ...headers, "Content-Type": "application/json" },\n  body: JSON.stringify(${jsonForDisplay(plan.json).replace(/\n/g, "\n  ")}),\n});`);
    }
    lines.push("if (!res.ok) throw new Error(await res.text());");
    lines.push(plan.job ? "const job = await res.json(); // then poll " + plan.job.poll : "const out = await res.arrayBuffer();");
    return lines.join("\n");
  }

  // ----------------------------------------------------------- cards/sections

  /** Cards for non-workflow sources (aliases, protocols). */
  function aliasCards(sectionId, source, ctx) {
    const ops = (ctx.aliases && ctx.aliases.operations) || {};
    const cards = [];
    const op = source.replace("aliases:", "");
    const stable = (ops[op] && ops[op].stable) || [];
    if (sectionId === "chat") {
      const chat = stable.filter((a) => a.alias !== "vision");
      if (chat.length) cards.push({ id: "chat", kind: "chat", title: "Chat", aliases: chat, op });
      const vision = stable.find((a) => a.alias === "vision");
      if (vision) cards.push({ id: "vision", kind: "vision", title: "Vision", aliases: [vision], op });
    } else if (sectionId === "speech") {
      if (stable.length) cards.push({ id: "tts", kind: "tts", title: "Text to speech", aliases: stable, op });
      cards.push({ id: "voices", kind: "voices", title: "Voices", aliases: [], op });
      cards.push({ id: "custom-voices", kind: "custom-voices", title: "Custom voices", aliases: [], op });
    } else if (sectionId === "transcription") {
      const diar = stable.filter((a) => /diarize/.test(a.alias));
      const plain = stable.filter((a) => !/diarize/.test(a.alias));
      if (plain.length) cards.push({ id: "stt", kind: "stt", title: "Transcription", aliases: plain, op });
      if (diar.length) cards.push({ id: "stt-diarize", kind: "stt-diarize", title: "Speaker diarization", aliases: diar, op });
    } else if (sectionId === "embeddings") {
      if (stable.length) cards.push({ id: "embeddings", kind: "embeddings", title: "Embeddings", aliases: stable, op });
    }
    return cards;
  }

  const PROTOCOLS = [
    { id: "protocol-openai", kind: "protocol", flavor: "openai", title: "OpenAI Chat Completions", path: "/v1/chat/completions", note: "Any OpenAI SDK or tool. Model names such as gpt-4o resolve to the stable aliases." },
    { id: "protocol-responses", kind: "protocol", flavor: "responses", title: "OpenAI Responses", path: "/v1/responses", note: "responses.create, previous_response_id, function tools, stored responses." },
    { id: "protocol-anthropic", kind: "protocol", flavor: "anthropic", title: "Anthropic Messages", path: "/anthropic/v1/messages", note: "Anthropic SDK and Claude Code. Model names such as claude-sonnet-* resolve to aliases." },
    { id: "protocol-ollama", kind: "protocol", flavor: "ollama", title: "Ollama", path: "/ollama/api/chat", note: "Ollama clients. Chat, generate, embed and read-only model routes." },
  ];

  /**
   * ctx: {sectionsDoc, workflows, openapi, capabilities, aliases}.
   * Returns [{id,title,blurb,cards:[...]}] in section order, without empty
   * sections (a section with no cards is hidden, not an error).
   */
  function buildSections(ctx) {
    const sections = ctx.sectionsDoc.sections;
    const out = sections.map((s) => ({ id: s.id, title: s.title, blurb: s.blurb, cards: [] }));
    const byId = Object.fromEntries(out.map((s) => [s.id, s]));
    const oxById = {};
    for (const ox of (ctx.openapi && ctx.openapi["x-vividfish-workflows"]) || []) oxById[ox.id] = ox;
    const avail = capabilityIndex(ctx.capabilities);
    const aliasMap = aliasesByWorkflow(ctx.aliases);

    for (const wf of ctx.workflows || []) {
      const sid = shortId(wf);
      const sec = byId[classifyWorkflow(sections, wf)];
      if (!sec) continue;
      const ox = oxById[wf.id] || null;
      const ex = (ox && ox.execution) || {};
      const submit = ex.sync || (ex.async && ex.async.submit) || {};
      sec.cards.push({
        id: wf.id, kind: "workflow", title: wf.display_name || sid, description: wf.description || "",
        intent: wf.intent, workflowId: wf.id, shortId: sid, typicalSeconds: wf.typical_seconds,
        resourceTier: wf.resource_tier, trust: wf.trust_tier,
        model: submit.model || sid,
        aliases: (aliasMap[sid] || []).concat(ox && ox.aliases ? ox.aliases : []).filter((v, i, a) => a.indexOf(v) === i),
        available: avail[sid] !== false,
        outputs: wf.outputs || [], ox,
      });
    }
    for (const s of sections) {
      if (s.source === "protocols") byId[s.id].cards.push(...PROTOCOLS.map((p) => ({ ...p })));
      else if (s.source && s.source.startsWith("aliases:")) byId[s.id].cards.push(...aliasCards(s.id, s.source, ctx));
    }
    for (const sec of out) sec.cards.sort((a, b) => (a.kind === "workflow" && b.kind === "workflow" ? (a.typicalSeconds ?? 1e9) - (b.typicalSeconds ?? 1e9) : 0));
    return out.filter((s) => s.cards.length);
  }

  /** workflow short id -> false when every capability row for it is unavailable. */
  function capabilityIndex(caps) {
    const idx = {};
    for (const c of (caps && caps.capabilities) || []) {
      if (!c.workflow) continue;
      const k = c.workflow.replace(/^[a-z]+\.[a-z]+\./, "");
      idx[k] = idx[k] === true || c.available !== false;
    }
    return idx;
  }

  /** workflow short id -> stable aliases pointing at it. */
  function aliasesByWorkflow(aliases) {
    const out = {};
    const ops = (aliases && aliases.operations) || {};
    for (const op of ["images", "image_edits", "videos", "audio_generate"]) {
      for (const a of (ops[op] && ops[op].stable) || []) {
        const target = String(a.resolves_to || "").replace(/^video-/, "");
        (out[target] = out[target] || []).push(a.alias);
      }
    }
    return out;
  }

  // ------------------------------------------------------------------ status

  /** One line + level for the status strip. level: ok | busy | warn | unknown */
  function statusSummary(health, status) {
    if (!health) return { level: "warn", text: "Gateway unreachable" };
    const parts = [];
    let level = "ok";
    if (status && status.comfyui) {
      const { running, pending } = status.comfyui;
      if (running || pending) {
        level = "busy";
        parts.push(`GPU busy: ${running} rendering, ${pending} queued`);
      } else parts.push("GPU idle");
    }
    if (status && status.llm) {
      if (status.llm.processing) { level = "busy"; parts.push("LLM generating"); }
      else parts.push("LLM idle");
    }
    if (!status) { level = "unknown"; parts.push("status needs an API key"); }
    return { level, text: parts.join(" - ") || "Gateway healthy" };
  }

  /** Group curated examples by the card they belong to. */
  function examplesByCard(list) {
    const out = {};
    for (const e of list || []) (out[e.card] = out[e.card] || []).push(e);
    for (const k of Object.keys(out)) out[k].sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.id.localeCompare(b.id));
    return out;
  }

  return {
    EXT_KEY, shortId, classifyWorkflow, resolveRef, formFields, planWorkflow, fillPath,
    curlFor, pythonFor, jsFor, buildSections, capabilityIndex, aliasesByWorkflow,
    statusSummary, examplesByCard, PROTOCOLS, isBlank,
  };
});

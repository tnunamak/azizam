/* Showcase page: fetches discovery, renders cards, runs Try-it requests.
 * Pure logic lives in core.js. Same-origin only; the optional API key stays in
 * memory for this page and is sent only to this gateway.
 */
(function () {
  "use strict";
  const C = window.ShowcaseCore;
  const BASE = location.origin;
  const KEY_STORE = "azizam.showcase.apiKey";
  const $ = (sel, root) => (root || document).querySelector(sel);

  // ------------------------------------------------------------------ helpers

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  let pageKey = "";
  const apiKey = () => pageKey;
  function authHeaders(extra) {
    const hd = Object.assign({}, extra || {});
    if (apiKey()) hd.Authorization = "Bearer " + apiKey();
    return hd;
  }

  class ApiError extends Error {
    constructor(status, body) {
      super(typeof body === "string" ? body : (body && body.error && (body.error.message || body.error.code)) || "HTTP " + status);
      this.status = status; this.body = body;
    }
  }

  async function api(path, opts) {
    const o = Object.assign({}, opts || {});
    // Paths can come from server responses (e.g. data[0].url); the Bearer key
    // goes only to the gateway origin, never to a URL some response named.
    if (C.isSameOrigin(path, location.href)) o.headers = authHeaders(o.headers);
    else { o.headers = Object.assign({}, o.headers || {}); o.credentials = "omit"; }
    const res = await fetch(path, o);
    if (!res.ok) {
      let body = await res.text();
      try { body = JSON.parse(body); } catch (e) { /* keep text */ }
      throw new ApiError(res.status, body);
    }
    return res;
  }

  async function apiJson(path, opts) { return (await api(path, opts)).json(); }

  function fmtSeconds(s) {
    if (s === null || s === undefined) return null;
    if (s < 90) return "~" + Math.round(s) + " s";
    return "~" + (s / 60).toFixed(s < 600 ? 1 : 0) + " min";
  }

  function readDataUri(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
  }

  function copyButton(getText) {
    const b = h("button", { class: "mini", type: "button", text: "Copy" });
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(getText()); b.textContent = "Copied"; }
      catch (e) { b.textContent = "Select and copy"; }
      setTimeout(() => (b.textContent = "Copy"), 1500);
    });
    return b;
  }

  function mediaEl(kind, src, opts) {
    const o = opts || {};
    if (kind === "image") return h("a", { href: src, target: "_blank", rel: "noopener" }, h("img", { src, alt: o.alt || "Generated image", loading: "lazy", decoding: "async" }));
    if (kind === "audio") return h("audio", { src, controls: true, preload: "none" });
    if (kind === "video") return h("video", { src, controls: true, preload: "none", playsinline: true, loop: o.loop });
    return h("pre", { class: "out-text", text: String(src) });
  }

  function kindFromType(type) {
    if (/^image\//.test(type)) return "image";
    if (/^audio\//.test(type)) return "audio";
    if (/^video\//.test(type)) return "video";
    return "text";
  }

  // ------------------------------------------------------------------- state

  const state = { data: null, examples: [], examplesByCard: {}, voices: null, forms: {} };

  // ----------------------------------------------------------------- results

  function resultBox() {
    const box = h("div", { class: "result" });
    const live = h("p", { class: "hint result-status", role: "status", "aria-live": "polite", "aria-atomic": "true" });
    const content = h("div");
    box.append(live, content);
    const announce = C.createAnnouncer((text) => { live.textContent = text; });
    return {
      el: box,
      clear() { content.replaceChildren(); announce(""); },
      announce,
      progress(label, opts) {
        const o = opts || {};
        announce(o.announcement || label);
        const bar = h("div", { class: "bar" }, h("span", { style: o.pct != null ? "width:" + o.pct + "%" : "" }));
        if (o.pct == null) bar.classList.add("indeterminate");
        content.replaceChildren(h("div", { class: "progress" }, h("div", { class: "plabel", text: label }), bar, o.hint ? h("div", { class: "hint", text: o.hint }) : null));
      },
      error(err) {
        const status = err.status;
        let msg = err.message || String(err);
        let hint = "Review the inputs, then use the run button to retry.";
        if (status === 401 || status === 403) hint = "Authentication needed. Add an API key with the key button at the top of the page.";
        else if (status === 503) hint = "The backend is busy or starting. Check the status strip, then retry.";
        else if (err.name === "TypeError") hint = "Check your connection, then use the run button to retry.";
        announce("Request failed. " + hint);
        const detail = err.body && typeof err.body === "object" ? JSON.stringify(err.body, null, 2) : null;
        content.replaceChildren(h("div", { class: "error" }, h("strong", { text: (status ? "HTTP " + status + ": " : "") + msg }), hint ? h("p", { text: hint }) : null, detail ? h("details", {}, h("summary", { text: "Raw error" }), h("pre", { text: detail })) : null));
      },
      media(kind, src, meta, filename) {
        announce("Result ready. Use the download link to save it.");
        const dl = h("a", { class: "mini", href: src, download: filename || "output", text: "Download" });
        content.replaceChildren(h("div", { class: "done" }, mediaEl(kind, src), h("div", { class: "meta" }, meta || "", " ", dl)));
      },
      text(text, meta) {
        if (!String(text || "").trim()) text = "No output returned. Review the inputs, then use the run button to retry.";
        announce("Result ready.");
        content.replaceChildren(h("div", { class: "done" }, h("pre", { class: "out-text", text }), meta ? h("div", { class: "meta", text: meta }) : null));
      },
      node(n, meta) {
        content.replaceChildren(h("div", { class: "done" }, n, meta ? h("div", { class: "meta", text: meta }) : null));
      },
    };
  }

  /** Run a thing with a Run/Cancel button pair and an elapsed timer. */
  function runner(button, res, typical, task) {
    let ctrl = null;
    let timer = null;
    const label = button.textContent;
    const cancel = h("button", { class: "mini danger", type: "button", text: "Cancel", hidden: true });
    button.after(cancel);
    cancel.addEventListener("click", () => ctrl && ctrl.abort());
    button.addEventListener("click", async () => {
      if (ctrl) return;
      ctrl = new AbortController();
      const t0 = Date.now();
      res.clear();
      button.disabled = true; cancel.hidden = false;
      cancel.focus();
      const hint = typical ? "Typical: " + fmtSeconds(typical) + ". Slower if another job holds the GPU." : "";
      const ctx = {
        signal: ctrl.signal,
        elapsed: () => (Date.now() - t0) / 1000,
        progress: (text, pct) => res.progress(text + " (" + Math.round((Date.now() - t0) / 1000) + " s)", { pct, hint, announcement: text }),
      };
      ctx.progress("Starting");
      timer = setInterval(() => ctx.lastLabel && ctx.progress(ctx.lastLabel, ctx.lastPct), 1000);
      const wrap = ctx.progress;
      ctx.progress = (text, pct) => { ctx.lastLabel = text; ctx.lastPct = pct; wrap(text, pct); };
      try { await task(ctx); if (ctrl.signal.aborted) throw new DOMException("aborted", "AbortError"); res.announce("Request finished. Review the result below."); }
      catch (e) {
        if (e.name === "AbortError") { res.node(h("p", { class: "hint", text: "Canceled. Use the run button to try again." })); res.announce("Canceled. Use the run button to try again."); }
        else res.error(e);
      } finally {
        const restoreFocus = document.activeElement === cancel;
        clearInterval(timer); ctrl = null; button.disabled = false; cancel.hidden = true; button.textContent = label;
        if (restoreFocus) button.focus();
      }
    });
  }

  const sleep = (ms, signal) => new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    if (signal) signal.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("aborted", "AbortError")); });
  });

  // -------------------------------------------------------- workflow execution

  async function executePlan(plan, ctx) {
    const init = { method: plan.method, signal: ctx.signal };
    if (plan.form) {
      const fd = new FormData();
      for (const p of plan.form) {
        if (p.file) fd.append(p.name, p.file.blob || (await (await fetch(p.file.dataUri)).blob()), p.file.filename || "input");
        else fd.append(p.name, p.value);
      }
      init.body = fd;
    } else {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(plan.json);
    }
    ctx.progress(plan.job ? "Submitting job" : "Rendering (the request stays open until done)");
    const res = await api(plan.path, init);
    if (!plan.job) return readSyncResult(res);
    const job = await res.json();
    const id = job.job_id || job.id;
    if (!id) throw new Error("No job id in response");
    const ok = new Set(plan.job.ok), term = new Set(plan.job.terminal);
    let status = job;
    const every = plan.job.kind === "video" ? 3000 : 2000;
    while (!term.has(status.status)) {
      const pct = typeof status.progress === "number" ? Math.min(99, status.progress <= 1 ? status.progress * 100 : status.progress) : null;
      ctx.progress(status.status === "queued" ? "Queued behind other GPU work" : "Rendering: " + status.status, pct);
      await sleep(every, ctx.signal);
      status = await apiJson(C.fillPath(plan.job.poll, "id", id), { signal: ctx.signal });
    }
    if (!ok.has(status.status)) {
      const e = new ApiError(0, status.error || status);
      e.message = typeof status.error === "string" ? status.error : (status.error && (status.error.message || status.error.code)) || "Job " + status.status;
      throw e;
    }
    ctx.progress("Downloading result", 100);
    const content = await api(C.fillPath(plan.job.content, "id", id), { signal: ctx.signal });
    return readSyncResult(content);
  }

  async function readSyncResult(res) {
    const type = (res.headers.get("content-type") || "").split(";")[0];
    if (type === "application/json") {
      const j = await res.json();
      const d = j.data && j.data[0];
      if (d && d.b64_json) {
        const mt = d.media_type || d.mime_type || "image/png";
        const bin = Uint8Array.from(atob(d.b64_json), (c) => c.charCodeAt(0));
        return { kind: kindFromType(mt), blob: new Blob([bin], { type: mt }), json: j };
      }
      if (d && d.url) {
        const r2 = await api(d.url);
        const b = await r2.blob();
        return { kind: kindFromType(b.type), blob: b, json: j };
      }
      return { kind: "json", json: j };
    }
    const blob = await res.blob();
    return { kind: kindFromType(type), blob };
  }

  function showResult(res, out, secondsLabel) {
    if (out.kind === "json") return res.text(JSON.stringify(out.json, null, 2), secondsLabel);
    const url = URL.createObjectURL(out.blob);
    const ext = (out.blob.type.split("/")[1] || "bin").replace("mpeg", "mp3").replace("x-wav", "wav");
    res.media(out.kind, url, secondsLabel, "output." + ext);
  }

  // ---------------------------------------------------------------- form bits

  function fieldInput(f, onFile) {
    const id = "f-" + Math.random().toString(36).slice(2, 8);
    let input;
    if (f.type === "asset") {
      const accept = f.media === "video" ? "video/*" : f.media === "audio" ? "audio/*" : "image/*";
      input = h("input", { type: "file", id, accept });
    } else if (f.type === "enum") {
      input = h("select", { id }, h("option", { value: "", text: "(default)" }), f.enum.map((v) => h("option", { value: v, text: v })));
    } else if (f.type === "integer" || f.type === "number") {
      input = h("input", { type: "number", id, min: f.min, max: f.max, step: f.type === "integer" ? 1 : "any", placeholder: f.def !== undefined ? "default " + f.def : "" });
    } else if (f.multiline) {
      input = h("textarea", { id, rows: f.name === "lyrics" ? 5 : 3, placeholder: f.def ? String(f.def) : "" });
    } else {
      input = h("input", { type: "text", id, placeholder: f.def !== undefined ? String(f.def) : "" });
    }
    if (f.required) input.setAttribute("aria-required", "true");
    if (f.description) input.setAttribute("aria-describedby", id + "-help");
    const range = f.min !== undefined && f.max !== undefined ? " (" + f.min + " to " + f.max + ")" : f.min !== undefined ? " (min " + f.min + ")" : f.max !== undefined ? " (max " + f.max + ")" : "";
    const wrap = h("div", { class: "field" + (f.required ? " required" : "") },
      h("label", { for: id, text: f.name + (f.required ? " *" : "") + range }),
      input,
      f.description ? h("small", { id: id + "-help", text: f.description.length > 160 ? f.description.slice(0, 157) + "..." : f.description }) : null);
    return { wrap, input };
  }

  async function readField(f, input) {
    if (f.type === "asset") {
      if (input._example) return input._example;
      const file = input.files && input.files[0];
      if (!file) return undefined;
      return { filename: file.name, type: file.type, blob: file, dataUri: await readDataUri(file) };
    }
    if (f.type === "integer" || f.type === "number") return input.value === "" ? undefined : Number(input.value);
    return input.value === "" ? undefined : input.value;
  }

  /** Attach an example media file as the value of a file input. */
  function setExampleFile(input, url, filename, type) {
    const token = {};
    input._exampleToken = token;
    input._example = null;
    let note = input.parentElement.querySelector(".example-file");
    if (!note) { note = h("span", { class: "example-file", role: "status", "aria-live": "polite" }); input.after(note); }
    note.textContent = "Loading example file. Wait before running.";
    fetch(url).then((response) => {
      if (!response.ok) throw new Error("Example file unavailable");
      return response.blob();
    }).then(async (blob) => {
      const dataUri = await readDataUri(blob);
      if (input._exampleToken !== token) return;
      input._example = { filename, type: blob.type || type, blob, dataUri };
      note.textContent = "Using example: " + filename;
    }).catch(() => {
      if (input._exampleToken === token) note.textContent = "Example file could not load. Choose a local file.";
    });
    input.addEventListener("change", () => {
      input._exampleToken = null; input._example = null;
      const n = input.parentElement.querySelector(".example-file"); if (n) n.remove();
    }, { once: true });
  }

  // --------------------------------------------------------------- try-it forms

  function tryWorkflow(card, examples) {
    const fields = C.formFields({ inputs_schema: card.ox.inputs_schema, route_inputs: card.ox.route_inputs }, state.data.openapi);
    const exec = card.ox.execution || {};
    const form = h("form", { class: "tryform", onsubmit: (e) => e.preventDefault() });
    const inputs = {};
    const main = h("div", { class: "fields" }), adv = h("div", { class: "fields" });
    for (const f of fields) {
      const { wrap, input } = fieldInput(f);
      inputs[f.name] = { f, input };
      (f.primary ? main : adv).append(wrap);
    }
    form.append(main);
    if (adv.children.length) form.append(h("details", { class: "advanced" }, h("summary", { text: "Advanced options (" + adv.children.length + ")" }), adv));
    const modes = [exec.sync && "sync", exec.async && "async"].filter(Boolean);
    let modeSel = null;
    if (modes.length > 1) {
      modeSel = h("select", { class: "mode", "aria-label": "Execution mode" }, modes.map((m) => h("option", { value: m, text: m === "async" ? "async job (recommended for long renders)" : "sync (waits on one request)", selected: m === exec.recommended })));
      form.append(h("div", { class: "field" }, h("label", { text: "Mode" }), modeSel));
    }
    const typical = card.typicalSeconds;
    const slow = typical && typical >= 60;
    const btn = h("button", { class: "run" + (slow ? " slow" : ""), type: "button", text: "Run" + (typical ? " (" + fmtSeconds(typical) + ")" : "") });
    const res = resultBox();
    const code = h("pre", { class: "plan" });
    form.append(h("div", { class: "actions" }, btn));
    if (slow) form.append(h("p", { class: "warn", text: "Slow: typically " + fmtSeconds(typical) + " and it holds the GPU. Other users wait while it runs." }));
    form.append(res.el);

    const collect = async () => {
      const values = {};
      for (const [name, { f, input }] of Object.entries(inputs)) values[name] = await readField(f, input);
      for (const [name, { f }] of Object.entries(inputs)) {
        if (f.required && C.isBlank(values[name])) { inputs[name].input.focus(); throw new Error("Missing required field: " + name); }
      }
      return values;
    };
    runner(btn, res, typical, async (ctx) => {
      const values = await collect();
      const plan = C.planWorkflow({ ox: card.ox }, values, modeSel ? modeSel.value : undefined);
      const t0 = Date.now();
      const out = await executePlan(plan, ctx);
      showResult(res, out, "Took " + ((Date.now() - t0) / 1000).toFixed(1) + " s");
    });
    form._load = (ex) => loadInto(inputs, ex);
    return form;
  }

  /** Fill form fields from an example's inputs (name-keyed). */
  function loadInto(inputs, ex) {
    for (const inp of ex.inputs || []) {
      const slot = inputs[inp.name];
      if (!slot) continue;
      if (inp.file) setExampleFile(slot.input, ex.base + inp.file, inp.file, inp.type);
      else if (inp.value !== undefined && "value" in slot.input) slot.input.value = inp.value;
    }
  }

  function simpleForm(spec, examples) {
    // spec: {fields:[{name,label,type,options,value,multiline,accept}], run(values, ctx, res), typical, button}
    const form = h("form", { class: "tryform", onsubmit: (e) => e.preventDefault() });
    const inputs = {};
    const fieldsEl = h("div", { class: "fields" });
    for (const f of spec.fields) {
      const id = "s-" + Math.random().toString(36).slice(2, 8);
      let input;
      if (f.type === "select") input = h("select", { id }, f.options.map((o) => h("option", { value: o.value ?? o, text: o.label ?? o, selected: (o.value ?? o) === f.value })));
      else if (f.type === "file") input = h("input", { type: "file", id, accept: f.accept });
      else if (f.type === "checkbox") input = h("input", { type: "checkbox", id, checked: !!f.value });
      else if (f.multiline) input = h("textarea", { id, rows: f.rows || 3, text: f.value || "" });
      else input = h("input", { type: f.type || "text", id, value: f.value ?? "", placeholder: f.placeholder || "" });
      inputs[f.name] = { f, input };
      fieldsEl.append(h("div", { class: "field" + (f.wide ? " wide" : "") }, h("label", { for: id, text: f.label }), input, f.help ? h("small", { text: f.help }) : null));
    }
    const btn = h("button", { class: "run", type: "button", text: spec.button || "Run" });
    const res = resultBox();
    form.append(fieldsEl, h("div", { class: "actions" }, btn), res.el);
    runner(btn, res, spec.typical, async (ctx) => {
      const values = {};
      for (const [name, { f, input }] of Object.entries(inputs)) {
        if (f.type === "file") {
          if (input._example) values[name] = input._example;
          else if (input.files[0]) values[name] = { filename: input.files[0].name, type: input.files[0].type, blob: input.files[0], dataUri: await readDataUri(input.files[0]) };
        } else if (f.type === "checkbox") values[name] = input.checked;
        else values[name] = input.value;
      }
      await spec.run(values, ctx, res);
    });
    form._load = (ex) => loadInto(inputs, ex);
    form._inputs = inputs;
    return form;
  }

  const SAMPLE_TEXT = "The quick brown fox jumps over the lazy dog, then naps in the afternoon sun.";

  function chatStream(path, body, ctx, res, onDelta) {
    return api(path, { method: "POST", signal: ctx.signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(async (r) => {
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (payload === "[DONE]") return;
          try { onDelta(JSON.parse(payload)); } catch (e) { /* partial */ }
        }
      }
    });
  }

  function chatLike(card, imageField) {
    const aliases = card.aliases.map((a) => ({ value: a.alias, label: a.alias + " -> " + a.resolves_to }));
    const fields = [
      { name: "alias", label: "Model alias", type: "select", options: aliases, value: aliases[0] && aliases[0].value },
    ];
    if (imageField) fields.push({ name: "image", label: "Image", type: "file", accept: "image/*" });
    fields.push({ name: "prompt", label: imageField ? "Question about the image" : "Message", multiline: true, value: imageField ? "What shapes and colors do you see?" : "Explain in two sentences why the sky is blue.", wide: true });
    return simpleForm({
      fields, button: "Send", typical: card.aliases[0] && /fast/.test(card.aliases[0].alias) ? 5 : 20,
      async run(v, ctx, res) {
        const content = [{ type: "text", text: v.prompt }];
        if (imageField) {
          if (!v.image) throw new Error("Choose an image first");
          content.unshift({ type: "image_url", image_url: { url: v.image.dataUri } });
        }
        const body = { model: v.alias, stream: true, messages: [{ role: "user", content: imageField ? content : v.prompt }] };
        let answer = "", thinking = 0;
        const t0 = Date.now();
        const answerEl = h("pre", { class: "out-text" });
        res.node(h("div", {}, h("div", { class: "hint thinking", text: "Thinking..." }), answerEl));
        const thinkEl = res.el.querySelector(".thinking");
        await chatStream("/v1/chat/completions", body, ctx, res, (j) => {
          const d = j.choices && j.choices[0] && j.choices[0].delta || {};
          if (d.reasoning_content) { thinking += d.reasoning_content.length; thinkEl.textContent = "Thinking... (" + thinking + " chars of reasoning)"; }
          if (d.content) { answer += d.content; answerEl.textContent = answer; }
        });
        if (!answer.trim()) answerEl.textContent = "No response returned. Edit your message, then select Send.";
        thinkEl.textContent = "Done in " + ((Date.now() - t0) / 1000).toFixed(1) + " s" + (thinking ? " (reasoning hidden: " + thinking + " chars)" : "");
      },
    });
  }

  function cosine(a, b) {
    let dot = 0, na = 0, nb = 0;
    for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
    return dot / Math.sqrt(na * nb);
  }

  function tryEmbeddings(card) {
    return simpleForm({
      fields: [{ name: "text", label: "Texts, one per line (2 to 6)", multiline: true, rows: 4, wide: true, value: "A cat sat on the mat.\nA kitten rested on the rug.\nQuarterly revenue grew 12 percent." }],
      button: "Embed", typical: 1,
      async run(v, ctx, res) {
        const lines = v.text.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 6);
        if (lines.length < 1) throw new Error("Enter at least one line");
        const t0 = Date.now();
        const r = await apiJson("/v1/embeddings", { method: "POST", signal: ctx.signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: card.aliases[0].alias, input: lines }) });
        const vecs = (r.data || []).map((d) => d.embedding);
        if (!vecs.length) { res.text("No vectors returned. Edit the texts, then select Embed."); return; }
        const table = h("table", { class: "sim" }, h("tr", {}, h("th"), vecs.map((_, i) => h("th", { text: "#" + (i + 1) }))),
          vecs.map((a, i) => h("tr", {}, h("th", { text: "#" + (i + 1) + " " + lines[i].slice(0, 28) }), vecs.map((b) => h("td", { text: cosine(a, b).toFixed(2) })))));
        res.node(h("div", {}, h("p", { text: vecs.length + " vectors, " + vecs[0].length + " dimensions. First values: " + vecs[0].slice(0, 5).map((x) => x.toFixed(3)).join(", ") + " ..." }), h("p", { class: "hint", text: "Cosine similarity matrix:" }), table), "Took " + ((Date.now() - t0) / 1000).toFixed(2) + " s");
      },
    });
  }

  function voiceOptions() {
    const v = state.voices || {};
    const opts = [];
    for (const x of v.voices || []) opts.push({ value: x.voice_id, label: x.voice_id + " (" + [x.language, x.gender].filter(Boolean).join(", ") + ")" });
    for (const x of v.data || []) opts.push({ value: x.id, label: "custom: " + (x.name || x.id) });
    return opts.length ? opts : [{ value: "alloy", label: "alloy" }];
  }

  function speak(model, voice, text, format, ctx) {
    const voiceVal = /^voice_/.test(voice) ? { id: voice } : voice;
    return api("/v1/audio/speech", { method: "POST", signal: ctx.signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model, voice: voiceVal, input: text, response_format: format || "mp3" }) }).then((r) => r.blob());
  }

  function tryTts(card) {
    const tiers = card.aliases.map((a) => ({ value: a.alias, label: a.alias + " -> " + (a.engine || a.resolves_to) }));
    return simpleForm({
      fields: [
        { name: "alias", label: "Tier", type: "select", options: tiers, value: "tts-fast" },
        { name: "voice", label: "Voice", type: "select", options: voiceOptions(), value: "alloy" },
        { name: "format", label: "Format", type: "select", options: ["mp3", "wav", "opus", "flac"], value: "mp3" },
        { name: "text", label: "Text", multiline: true, wide: true, value: SAMPLE_TEXT },
      ],
      button: "Speak", typical: 4,
      async run(v, ctx, res) {
        ctx.progress("Synthesizing (an idle engine starts on demand)");
        const t0 = Date.now();
        const blob = await speak(v.alias, v.voice, v.text, v.format, ctx);
        res.media("audio", URL.createObjectURL(blob), "Took " + ((Date.now() - t0) / 1000).toFixed(1) + " s", "speech." + v.format);
      },
    });
  }

  function voicesTable(res) {
    const list = (state.voices && state.voices.voices) || [];
    const play = h("div", { class: "voice-grid" });
    for (const v of list) {
      const out = h("span", { class: "vplay", role: "status", "aria-live": "polite" });
      const b = h("button", { class: "mini", type: "button", text: "Play" });
      b.addEventListener("click", async () => {
        b.disabled = true; b.textContent = "Loading"; out.textContent = "Preparing voice preview.";
        try {
          const blob = await speak("tts-fast", v.voice_id, "Hello, this is the " + v.name + " voice.", "mp3", { signal: undefined });
          out.replaceChildren(h("audio", { src: URL.createObjectURL(blob), controls: true, autoplay: true }));
        } catch (e) { out.replaceChildren(h("span", { class: "err", text: e.message + ". Select Play to retry." })); }
        b.disabled = false; b.textContent = "Play";
      });
      play.append(h("div", { class: "voice" }, h("strong", { text: v.name || v.voice_id }), h("span", { class: "tag", text: [v.language, v.gender].filter(Boolean).join(" / ") }), h("small", { text: v.description || "" }), b, out));
    }
    if (!list.length) return h("p", { class: "hint", text: "No voices loaded. Use the API key button to connect, then reopen this panel." });
    return h("div", {}, h("p", { class: "hint", text: list.length + " built-in voices from GET /v1/voices. Play synthesizes a short line with tts-fast." }), play);
  }

  function tryCustomVoice() {
    return simpleForm({
      fields: [
        { name: "name", label: "Voice name", value: "My narrator" },
        { name: "sample", label: "Audio sample (5 to 60 s of clear speech, your own voice or one you have rights to)", type: "file", accept: "audio/*", wide: true },
        { name: "consent", label: "Consent id (cons_..., only if the gateway requires it)", placeholder: "optional" },
      ],
      button: "Create voice", typical: 15,
      async run(v, ctx, res) {
        if (!v.sample) throw new Error("Choose an audio sample");
        const fd = new FormData();
        fd.append("name", v.name); fd.append("audio_sample", v.sample.blob, v.sample.filename);
        if (v.consent) fd.append("consent", v.consent);
        const r = await apiJson("/v1/audio/voices", { method: "POST", body: fd, signal: ctx.signal });
        await refreshVoices();
        res.text(JSON.stringify(r, null, 2), "Created. It now appears in the Text to speech voice list for your key.");
      },
    });
  }

  function trySTT(card, diarize) {
    const tiers = card.aliases.map((a) => ({ value: a.alias, label: a.alias + " -> " + (a.engine || a.resolves_to) }));
    const exAudio = (state.examplesByCard["tts"] || [])[0];
    const fields = [
      { name: "alias", label: diarize ? "Model" : "Tier", type: "select", options: tiers, value: tiers[0] && tiers[0].value },
      { name: "audio", label: "Audio file (wav, mp3, m4a, flac, ogg, webm)", type: "file", accept: "audio/*", wide: true },
    ];
    const form = simpleForm({
      fields, button: diarize ? "Transcribe with speakers" : "Transcribe", typical: diarize ? 15 : 3,
      async run(v, ctx, res) {
        if (!v.audio) throw new Error("Choose an audio file");
        const fd = new FormData();
        fd.append("model", v.alias);
        fd.append("file", v.audio.blob, v.audio.filename);
        if (diarize) { fd.append("response_format", "diarized_json"); fd.append("chunking_strategy", "auto"); } else fd.append("response_format", "json");
        const t0 = Date.now();
        const r = await apiJson("/v1/audio/transcriptions", { method: "POST", body: fd, signal: ctx.signal });
        const took = "Took " + ((Date.now() - t0) / 1000).toFixed(1) + " s";
        if (diarize && r.segments) res.node(h("div", { class: "segments" }, r.segments.map((s) => h("p", {}, h("strong", { text: "Speaker " + s.speaker + " " }), h("small", { text: (s.start ?? 0).toFixed(1) + "-" + (s.end ?? 0).toFixed(1) + " s " }), s.text))), took);
        else res.text(r.text || JSON.stringify(r, null, 2), took);
      },
    });
    return form;
  }

  const PROTO_BODIES = {
    openai: (p) => ({ model: "default", messages: [{ role: "user", content: p }], max_tokens: 256 }),
    responses: (p) => ({ model: "default", input: p, max_output_tokens: 256 }),
    anthropic: (p) => ({ model: "claude-sonnet-4", max_tokens: 256, messages: [{ role: "user", content: p }] }),
    ollama: (p) => ({ model: "default", stream: false, messages: [{ role: "user", content: p }] }),
  };

  function protoText(flavor, j) {
    if (flavor === "openai") return j.choices && j.choices[0].message.content;
    if (flavor === "responses") return (j.output || []).filter((o) => o.type === "message").flatMap((o) => o.content.map((c) => c.text)).join("");
    if (flavor === "anthropic") return (j.content || []).filter((c) => c.type === "text").map((c) => c.text).join("");
    if (flavor === "ollama") return j.message && j.message.content;
    return "";
  }

  function tryProtocol(card) {
    return simpleForm({
      fields: [{ name: "prompt", label: "Message", multiline: true, wide: true, value: "Reply with one short sentence saying which protocol you arrived on." }],
      button: "Send via " + card.title, typical: 25,
      async run(v, ctx, res) {
        const body = PROTO_BODIES[card.flavor](v.prompt);
        const t0 = Date.now();
        const headers = { "Content-Type": "application/json" };
        if (card.flavor === "anthropic") headers["anthropic-version"] = "2023-06-01";
        const j = await apiJson(card.path, { method: "POST", signal: ctx.signal, headers, body: JSON.stringify(body) });
        const text = protoText(card.flavor, j);
        res.node(h("div", {}, h("pre", { class: "out-text", text: text || "No response returned. Edit your message, then select Send." }), h("details", {}, h("summary", { text: "Raw " + card.title + " response" }), h("pre", { text: JSON.stringify(j, null, 2) }))), "Took " + ((Date.now() - t0) / 1000).toFixed(1) + " s");
      },
    });
  }

  // ------------------------------------------------------------------ snippets

  let nextPanelId = 0;
  function codeTabs(tabs) {
    const id = "code-" + (++nextPanelId);
    const wrap = h("div", { class: "code" });
    const bar = h("div", { class: "tabs", role: "tablist", "aria-label": "Code language" });
    const pre = h("pre", { id, tabindex: "0", role: "tabpanel" });
    let current = tabs[0];
    const buttons = tabs.map((t, i) => h("button", {
      id: id + "-tab-" + i, class: "tab", type: "button", role: "tab",
      "aria-controls": id, text: t.label, onclick: () => show(i),
      onkeydown: (e) => {
        const next = C.tabIndexForKey(e.key, i, tabs.length);
        if (next === null) return;
        e.preventDefault(); show(next); buttons[next].focus();
      },
    }));
    function show(index) {
      current = tabs[index]; pre.textContent = current.code;
      pre.setAttribute("aria-labelledby", buttons[index].id);
      buttons.forEach((button, i) => {
        button.setAttribute("aria-selected", String(i === index));
        button.tabIndex = i === index ? 0 : -1;
      });
    }
    bar.append(...buttons);
    wrap.append(bar, copyButton(() => current.code), pre);
    show(0);
    return wrap;
  }

  const KEYLINE = '-H "Authorization: Bearer $AZIZAM_API_KEY"';

  function cardSnippets(card, ex) {
    const exampleValues = {};
    for (const i of (ex && ex.inputs) || []) if (i.value !== undefined && i.name) exampleValues[i.name] = i.value;
    switch (card.kind) {
      case "workflow": {
        const fields = C.formFields({ inputs_schema: card.ox.inputs_schema, route_inputs: card.ox.route_inputs }, state.data.openapi);
        const values = Object.assign({}, exampleValues);
        for (const f of fields) {
          if (!f.required || values[f.name] !== undefined) continue;
          values[f.name] = f.type === "asset" ? { filename: f.media === "video" ? "input.mp4" : f.media === "audio" ? "input.wav" : "input.png", dataUri: "data:;base64,..." } : f.type === "string" ? "..." : f.min ?? 1;
        }
        const plan = C.planWorkflow({ ox: card.ox }, values);
        return [{ label: "curl", code: C.curlFor(plan, BASE) }, { label: "Python", code: C.pythonFor(plan, BASE) }, { label: "JavaScript", code: C.jsFor(plan, BASE) }];
      }
      case "chat": case "vision":
        return [
          { label: "curl", code: `curl -sS ${BASE}/v1/chat/completions ${KEYLINE} \\\n  -H 'Content-Type: application/json' \\\n  -d '{"model": "${card.aliases[0].alias}", "messages": [{"role": "user", "content": "Explain why the sky is blue."}]}'` },
          { label: "Python (openai)", code: `from openai import OpenAI\nclient = OpenAI(base_url="${BASE}/v1", api_key="$AZIZAM_API_KEY")\n\nr = client.chat.completions.create(\n    model="${card.aliases[0].alias}",\n    messages=[{"role": "user", "content": "Explain why the sky is blue."}],\n)\nprint(r.choices[0].message.content)` + (card.kind === "vision" ? `\n\n# Vision: pass an image part\nmessages=[{"role": "user", "content": [\n    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},\n    {"type": "text", "text": "What is in this image?"},\n]}]` : "") },
          { label: "JavaScript", code: `import OpenAI from "openai";\nconst client = new OpenAI({ baseURL: "${BASE}/v1", apiKey: process.env.AZIZAM_API_KEY });\nconst r = await client.chat.completions.create({\n  model: "${card.aliases[0].alias}",\n  messages: [{ role: "user", content: "Explain why the sky is blue." }],\n});\nconsole.log(r.choices[0].message.content);` },
        ];
      case "embeddings":
        return [
          { label: "curl", code: `curl -sS ${BASE}/v1/embeddings ${KEYLINE} \\\n  -H 'Content-Type: application/json' \\\n  -d '{"model": "embed", "input": ["A cat sat on the mat.", "A kitten rested on the rug."]}'` },
          { label: "Python (openai)", code: `from openai import OpenAI\nclient = OpenAI(base_url="${BASE}/v1", api_key="$AZIZAM_API_KEY")\nr = client.embeddings.create(model="embed", input=["A cat sat on the mat."])\nprint(len(r.data[0].embedding))  # 768` },
        ];
      case "tts":
        return [
          { label: "curl", code: `curl -sS ${BASE}/v1/audio/speech ${KEYLINE} \\\n  -H 'Content-Type: application/json' \\\n  -d '{"model": "tts", "voice": "alloy", "input": "Hello from the gateway.", "response_format": "mp3"}' \\\n  -o speech.mp3` },
          { label: "Python (openai)", code: `from openai import OpenAI\nclient = OpenAI(base_url="${BASE}/v1", api_key="$AZIZAM_API_KEY")\nwith client.audio.speech.with_streaming_response.create(\n    model="tts", voice="alloy", input="Hello from the gateway.",\n) as r:\n    r.stream_to_file("speech.mp3")` },
          { label: "Streaming PCM", code: `# response_format=pcm streams 24 kHz s16le while the engine speaks\ncurl -sN ${BASE}/v1/audio/speech ${KEYLINE} \\\n  -H 'Content-Type: application/json' \\\n  -d '{"model": "tts-fast", "voice": "alloy", "input": "Streaming test.", "response_format": "pcm"}' \\\n  | ffplay -f s16le -ar 24000 -ac 1 -nodisp -autoexit -` },
        ];
      case "voices": case "custom-voices":
        return [
          { label: "curl", code: `# list built-in and your custom voices\ncurl -sS ${BASE}/v1/voices ${KEYLINE}\n\n# create a custom voice from a 5-60 s sample\ncurl -sS ${BASE}/v1/audio/voices ${KEYLINE} \\\n  -F name='My narrator' -F audio_sample=@me.wav\n\n# speak with it\ncurl -sS ${BASE}/v1/audio/speech ${KEYLINE} \\\n  -H 'Content-Type: application/json' \\\n  -d '{"model": "tts", "voice": {"id": "voice_..."}, "input": "Hello there."}' -o out.mp3` },
          { label: "Python (openai)", code: `from openai import OpenAI\nclient = OpenAI(base_url="${BASE}/v1", api_key="$AZIZAM_API_KEY")\nvoice = client.audio.voices.create(name="My narrator", audio_sample=open("me.wav", "rb"))\nwith client.audio.speech.with_streaming_response.create(\n    model="tts", voice={"id": voice.id}, input="Hello there.",\n) as r:\n    r.stream_to_file("out.mp3")` },
        ];
      case "stt": case "stt-diarize": {
        const d = card.kind === "stt-diarize";
        return [
          { label: "curl", code: `curl -sS ${BASE}/v1/audio/transcriptions ${KEYLINE} \\\n  -F model=${card.aliases[0].alias} -F file=@speech.wav` + (d ? ` \\\n  -F response_format=diarized_json -F chunking_strategy=auto` : "") },
          { label: "Python (openai)", code: `from openai import OpenAI\nclient = OpenAI(base_url="${BASE}/v1", api_key="$AZIZAM_API_KEY")\nr = client.audio.transcriptions.create(\n    model="${card.aliases[0].alias}", file=open("speech.wav", "rb"),` + (d ? `\n    response_format="diarized_json", chunking_strategy="auto",\n)\nfor s in r.segments:\n    print(s.speaker, s.start, s.end, s.text)` : `\n)\nprint(r.text)`) },
        ];
      }
      case "protocol": {
        const p = card.flavor;
        const body = JSON.stringify(PROTO_BODIES[p]("Hello"), null, 2);
        const curl = `curl -sS ${BASE}${card.path} ${KEYLINE} \\\n  -H 'Content-Type: application/json'${p === "anthropic" ? " \\\n  -H 'anthropic-version: 2023-06-01'" : ""} \\\n  -d '${body}'`;
        const sdk = {
          openai: `from openai import OpenAI\nclient = OpenAI(base_url="${BASE}/v1", api_key="$AZIZAM_API_KEY")\nr = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "Hello"}])  # gpt-4o resolves to alias default\nprint(r.choices[0].message.content)`,
          responses: `from openai import OpenAI\nclient = OpenAI(base_url="${BASE}/v1", api_key="$AZIZAM_API_KEY")\nr = client.responses.create(model="default", input="Hello")\nprint(r.output_text)`,
          anthropic: `import anthropic\nclient = anthropic.Anthropic(base_url="${BASE}/anthropic", api_key="$AZIZAM_API_KEY")\nm = client.messages.create(model="claude-sonnet-4", max_tokens=256, messages=[{"role": "user", "content": "Hello"}])\nprint(m.content[0].text)`,
          ollama: `from ollama import Client\nclient = Client(host="${BASE}/ollama", headers={"Authorization": "Bearer $AZIZAM_API_KEY"})\nr = client.chat(model="default", messages=[{"role": "user", "content": "Hello"}])\nprint(r.message.content)`,
        }[p];
        return [{ label: "curl", code: curl }, { label: "Python SDK", code: sdk }];
      }
      default: return [{ label: "curl", code: "# see /openapi.json" }];
    }
  }

  // ------------------------------------------------------------------ examples

  const LOAD_FAILED = "This recording could not be loaded.";

  function loadFailed(retry) {
    return h("div", { class: "load-failed" }, h("p", { text: LOAD_FAILED }),
      retry ? h("button", { class: "mini", type: "button", text: "Try again", onclick: retry }) : null);
  }

  /** A recorded file that says so when it fails to load, with Retry. */
  function recordedMedia(kind, src, opts) {
    const box = h("div", { class: "recorded-media" }, mediaEl(kind, src, opts));
    const media = box.querySelector("img, audio, video");
    if (media) media.addEventListener("error", () => {
      box.replaceChildren(loadFailed(() => {
        const fresh = recordedMedia(kind, src, opts);
        box.replaceWith(fresh);
        (fresh.querySelector("a, audio, video") || fresh).focus();
      }));
    }, { once: true });
    return box;
  }

  function inputBlock(i, ex) {
    const label = h("div", { class: "io-label", text: i.label || i.name || i.kind || "Request" });
    if (i.file) return h("div", { class: "io" }, label, recordedMedia(i.kind, ex.base + i.file, { alt: i.label }));
    return h("div", { class: "io" }, label, h("pre", { class: "in-text", text: typeof i.value === "object" ? JSON.stringify(i.value, null, 2) : String(i.value) }));
  }

  function outputBlock(o, ex) {
    const label = o.label ? h("div", { class: "io-label", text: o.label }) : null;
    if (o.kind === "text" || o.kind === "json") return h("div", { class: "io" }, label, h("pre", { class: "out-text", text: typeof o.text === "string" ? o.text : JSON.stringify(o.value ?? o.text, null, 2) }));
    return h("div", { class: "io" }, label, recordedMedia(o.kind, ex.base + o.file, { loop: o.kind === "video" && ex.loop }));
  }

  function recordedDay(iso) {
    const d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "";
  }

  function renderExample(card, ex, form) {
    const meta = [];
    const day = recordedDay(ex.recorded_at);
    if (day) meta.push(h("span", { class: "chip", text: "Recorded " + day }));
    if (ex.seconds != null) meta.push(h("span", { class: "chip time", title: "Wall time of this request through the gateway, when it was recorded", text: "Ran in " + (ex.seconds < 90 ? ex.seconds.toFixed(1) + " s" : Math.round(ex.seconds) + " s") }));
    if (ex.model) meta.push(h("span", { class: "chip model", text: ex.model + (ex.resolves_to && ex.resolves_to !== ex.model ? " -> " + ex.resolves_to : "") }));
    for (const c of ex.chips || []) meta.push(h("span", { class: "chip", text: c }));
    const parts = C.recordedParts(ex);
    const load = form && form._load && parts.inputs.some((i) => i.name)
      ? h("button", { class: "mini", type: "button", text: "Load into Try it", onclick: () => { const panel = form.closest("details"); panel._returnFocus = load; panel.open = true; form._load(ex); focusPanel(panel); } })
      : null;
    return h("div", { class: "example" },
      h("div", { class: "io-row" },
        h("div", { class: "io-col in" }, h("div", { class: "col-head", text: "Input" }),
          parts.inputs.length ? parts.inputs.map((i) => inputBlock(i, ex)) : h("p", { class: "io-none", text: "Input was not recorded." })),
        h("div", { class: "io-arrow", "aria-hidden": "true", text: "\u2192" }),
        h("div", { class: "io-col out" }, h("div", { class: "col-head", text: "Output" }),
          parts.outputs.map((o) => (o ? outputBlock(o, ex) : loadFailed(loadAll))))),
      ex.title ? h("div", { class: "ex-title", text: ex.title }) : null,
      h("div", { class: "ex-meta" }, meta, load));
  }

  // --------------------------------------------------------------------- cards

  function badges(card) {
    const out = [];
    if (card.kind === "workflow") {
      out.push(h("span", { class: "badge intent", text: card.intent }));
      if (card.typicalSeconds != null) out.push(h("span", { class: "badge" + (card.typicalSeconds >= 60 ? " slow" : ""), title: "typical_seconds from discovery", text: fmtSeconds(card.typicalSeconds) + " typical" }));
      if (card.available === false) out.push(h("span", { class: "badge down", text: "unavailable" }));
      if (card.resourceTier) out.push(h("span", { class: "badge", text: card.resourceTier }));
    }
    return out;
  }

  function aliasLine(card) {
    if (card.kind === "workflow") {
      const bits = [h("code", { text: card.model })];
      if (card.aliases.length) bits.unshift("aliases ", card.aliases.map((a, i) => [i ? ", " : "", h("code", { text: a })]), " resolve to ");
      else bits.unshift("model ");
      return h("p", { class: "alias" }, bits);
    }
    if (card.aliases && card.aliases.length) {
      return h("p", { class: "alias" }, card.aliases.map((a, i) => [i ? "  " : "", h("code", { text: a.alias }), " \u2192 ", h("code", { text: a.engine ? a.engine + (a.upstream_model ? " (" + a.upstream_model + ")" : "") : a.resolves_to })]));
    }
    return null;
  }

  function cardDescription(card) {
    if (card.kind === "workflow") return card.description;
    return {
      chat: "OpenAI-compatible chat completions with this gateway's chat model names.",
      vision: "Send an image with a question to a model that accepts images.",
      embeddings: "Turn text into vectors for search and similarity.",
      tts: "Turn text into speech with the voices this gateway offers.",
      voices: "The voices this gateway lists at GET /v1/voices.",
      "custom-voices": "Make a voice from a short sample, then use it with the speech endpoint. A custom voice belongs to the key that made it.",
      stt: "Turn speech into text.",
      "stt-diarize": "Transcribe audio and label who spoke when, in OpenAI's diarized_json shape.",
      protocol: card.note,
    }[card.kind] || "";
  }

  function exampleCardKey(card) {
    return card.id;
  }

  function renderCard(card) {
    const examples = state.examplesByCard[exampleCardKey(card)] || [];
    let form = null;
    try {
      if (card.kind === "workflow" && card.ox) form = tryWorkflow(card, examples);
      else if (card.kind === "chat") form = chatLike(card, false);
      else if (card.kind === "vision") form = chatLike(card, true);
      else if (card.kind === "embeddings") form = tryEmbeddings(card);
      else if (card.kind === "tts") form = tryTts(card);
      else if (card.kind === "custom-voices") form = tryCustomVoice();
      else if (card.kind === "stt") form = trySTT(card, false);
      else if (card.kind === "stt-diarize") form = trySTT(card, true);
      else if (card.kind === "protocol") form = tryProtocol(card);
    } catch (e) { form = h("p", { class: "error", text: "Try-it unavailable: " + e.message }); }

    const exBlocks = examples.length
      ? examples.map((ex) => renderExample(card, ex, form))
      : [h("div", { class: "example pending" }, h("p", { text: "No recorded example for this item. Use Try it live to run one." }))];

    const tryLabel = card.kind === "voices" ? "Browse and play voices" : "Try it live";
    const tryBody = card.kind === "voices" ? voicesTable() : form;
    const node = h("article", { class: "card kind-" + card.kind, id: "card-" + card.id },
      h("div", { class: "examples" }, exBlocks),
      h("header", {}, h("h3", { text: card.title }), h("div", { class: "badges" }, badges(card))),
      card.kind === "workflow" ? h("p", { class: "desc" }, shortDesc(card.description)) : h("p", { class: "desc", text: cardDescription(card) }),
      aliasLine(card),
      tryBody ? h("details", { class: "try" }, h("summary", { text: tryLabel }), tryBody) : null,
      card.kind === "recorded" ? null : snippetsPanel(card, examples[0]));
    const panel = node.querySelector("details.try");
    if (panel) managePanel(panel);
    return node;
  }

  function focusPanel(panel) {
    const target = panel.querySelector("input:not([disabled]), textarea, select, button:not([disabled])");
    if (target) target.focus();
  }

  function managePanel(panel) {
    const summary = panel.querySelector("summary");
    panel.addEventListener("toggle", () => {
      if (panel.open) {
        if (document.activeElement === summary || panel._returnFocus === document.activeElement) focusPanel(panel);
      } else if (panel.contains(document.activeElement)) {
        (panel._returnFocus && panel._returnFocus.isConnected ? panel._returnFocus : summary).focus();
        panel._returnFocus = null;
      }
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !panel.open) return;
      event.preventDefault(); event.stopPropagation(); panel.open = false;
      (panel._returnFocus && panel._returnFocus.isConnected ? panel._returnFocus : summary).focus();
      panel._returnFocus = null;
    });
  }

  /** Build the code tabs only when the panel opens; most are never opened. */
  function snippetsPanel(card, ex) {
    const d = h("details", { class: "snippets" }, h("summary", { text: "Copy-paste code" }));
    let built = false;
    d.addEventListener("toggle", () => {
      if (!d.open || built) return;
      built = true;
      d.append(codeTabs(cardSnippets(card, ex)));
    });
    return d;
  }

  function shortDesc(text) {
    const first = String(text || "").split(/(?<=[.!?])\s+/).slice(0, 2).join(" ");
    return first.length > 300 ? first.slice(0, 297) + "..." : first;
  }

  // ----------------------------------------------------------------- page shell

  function renderPage() {
    const sections = C.buildSections({
      sectionsDoc: state.data.sectionsDoc, workflows: state.data.workflows.workflows,
      openapi: state.data.openapi, capabilities: state.data.capabilities, aliases: state.data.aliases,
      exampleCards: Object.keys(state.examplesByCard), live: state.live,
    });
    state.sections = sections;
    const main = $("#sections");
    main.replaceChildren();
    const nav = $("#nav");
    nav.replaceChildren();
    for (const s of sections) {
      nav.append(h("a", { href: "#sec-" + s.id, text: s.title }));
      main.append(h("section", { id: "sec-" + s.id, class: "section" },
        h("h2", { text: s.title }), h("p", { class: "blurb", text: s.blurb }),
        h("div", { class: "grid" }, s.cards.map(renderCard))));
    }
    const cards = sections.flatMap((s) => s.cards);
    const shown = cards.reduce((n, c) => n + (state.examplesByCard[c.id] || []).length, 0);
    const live = cards.filter((c) => c.kind !== "recorded").length;
    $("#stats").textContent = plural(shown, "recorded example") + " in " + plural(sections.length, "group")
      + (state.live ? ". " + plural(live, "capability", "capabilities") + " you can try live." : ".");
    // Deep link.
    if (location.hash) { const t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); }
  }

  async function refreshVoices() {
    try { state.voices = await apiJson("/v1/voices"); } catch (e) { state.voices = state.voices || null; }
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many || one + "s");
  }

  let loadVersion = 0;
  function discoveryNotice(text, action, handler) {
    const banner = $("#banner");
    banner.hidden = false;
    banner.replaceChildren(h("p", { text }), action ? h("button", { type: "button", class: "mini", text: action, onclick: handler }) : null);
  }

  async function loadAll() {
    const version = ++loadVersion;
    const banner = $("#banner");
    discoveryNotice("Loading live capabilities. Recorded examples remain available.", null);
    $("#sections").setAttribute("aria-busy", "true");
    try {
    const get = (p) => apiJson(p).catch((e) => ({ __error: e }));
    const [workflows, openapi, capabilities, aliases, sectionsDoc, ex, voices] = await Promise.all([
      get("/gateway/v1/workflows"), get("/openapi.json"), get("/gateway/v1/capabilities"), get("/gateway/v1/aliases"),
      fetch("/showcase/static/sections.json").then((r) => r.json()),
      fetch("/showcase/examples/index.json")
        .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .catch((e) => ({ data: [], __error: e })),
      get("/v1/voices"),
    ]);
    if (version !== loadVersion) return;
    const authFail = [workflows, openapi, capabilities, aliases].find((x) => x.__error && [401, 403].includes(x.__error.status));
    if (authFail) {
      discoveryNotice("Live capabilities need an API key from the gateway owner. The key stays in memory for this page only.", "Add API key", () => { const p = $("#keypanel"); if (p.hidden) $("#keytoggle").click(); else $("#apikey").focus(); });
    }
    state.data = {
      workflows: workflows.__error ? { workflows: [] } : workflows,
      openapi: openapi.__error ? {} : openapi,
      capabilities: capabilities.__error ? { capabilities: [] } : capabilities,
      aliases: aliases.__error ? { operations: {} } : aliases,
      sectionsDoc,
    };
    state.voices = voices.__error ? null : voices;
    state.live = !authFail && !workflows.__error;
    state.examples = ex.data || [];
    state.examplesByCard = C.examplesByCard(state.examples);
    renderPage();
    if (!authFail) {
      if ([workflows, openapi, capabilities, aliases].some((x) => x.__error)) discoveryNotice("Some live capabilities could not be loaded.", "Retry discovery", loadAll);
      else if (!state.sections.some((section) => section.cards.length)) discoveryNotice("No live capabilities were returned by this gateway.", "Retry discovery", loadAll);
      else banner.hidden = true;
    }
    if (ex.__error) discoveryNotice("Recorded examples could not be loaded from the gateway.", "Try again", loadAll);
    } catch (error) {
      if (version === loadVersion) discoveryNotice("Could not load live capabilities. " + error.message, "Retry discovery", loadAll);
    } finally {
      if (version === loadVersion) $("#sections").removeAttribute("aria-busy");
    }
  }

  // ---------------------------------------------------------------- status strip

  async function pollStatus() {
    const el = $("#status");
    let health = null, status = null;
    try { health = await (await fetch("/health")).json(); } catch (e) { /* unreachable */ }
    try { status = await apiJson("/showcase/api/status"); } catch (e) { status = null; }
    const sum = C.statusSummary(health, status);
    el.dataset.level = sum.level;
    const statusText = $("#status-text");
    if (statusText.textContent !== sum.text) statusText.textContent = sum.text;
    const chips = $("#engines");
    chips.replaceChildren();
    if (status && status.engines) {
      for (const e of status.engines) {
        chips.append(h("span", { class: "eng " + (e.status === "available" ? "up" : "down"), title: e.name + ": " + e.status + (e.status === "available" ? "" : " (on-demand engines start when first used)"), text: e.id }));
      }
    }
  }

  function initKey() {
    // Remove the legacy entry without reading its value. Storage can be blocked.
    try { localStorage.removeItem(KEY_STORE); } catch (e) { /* memory-only still works */ }
    const input = $("#apikey");
    const panel = $("#keypanel");
    const toggle = $("#keytoggle");
    input.value = "";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");
    toggle.setAttribute("aria-controls", "keypanel");
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
    function close() {
      panel.hidden = true; input.value = "";
      toggle.setAttribute("aria-expanded", "false"); toggle.focus();
    }
    const help = $("#key-help");
    if (help) help.textContent = "Your key stays in memory for this page only. Reloading or closing the page clears it.";
    $("#keysave").textContent = "Use for this page";
    $("#keysave").addEventListener("click", () => {
      pageKey = input.value.trim();
      close(); loadAll(); pollStatus();
    });
    $("#keyclear").addEventListener("click", () => {
      pageKey = ""; close(); loadAll(); pollStatus();
    });
    toggle.addEventListener("click", () => {
      if (!panel.hidden) { close(); return; }
      panel.hidden = false; toggle.setAttribute("aria-expanded", "true"); input.focus();
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      else if (event.key === "Enter" && event.target === input) { event.preventDefault(); $("#keysave").click(); }
    });
    $("#base").textContent = BASE;
    const live = $("#status-text");
    live.setAttribute("role", "status"); live.setAttribute("aria-live", "polite");
    live.setAttribute("aria-atomic", "true");
    $("#status").removeAttribute("aria-live");
    $("#status").removeAttribute("role");
  }

  initKey();
  loadAll();
  pollStatus();
  setInterval(pollStatus, 10000);
})();

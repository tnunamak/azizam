// Docs pages: copy buttons, search over the prebuilt index, and the
// "On this page" highlight. Pages read fine without this script.
(() => {
  "use strict";

  const INDEX_URL = "/showcase/docs/search-index.json";
  const status = document.getElementById("copy-status");

  // Copy buttons. The button copies the code exactly as shown.
  document.querySelectorAll(".codeblock .copy").forEach((button) => {
    button.addEventListener("click", async () => {
      const code = button.closest(".codeblock").querySelector("pre code");
      let ok = true;
      try {
        await navigator.clipboard.writeText(code.textContent);
      } catch {
        ok = false;
      }
      button.textContent = ok ? "Copied" : "Copy failed";
      button.dataset.state = ok ? "done" : "failed";
      if (status) status.textContent = ok ? "Code copied" : "Copy failed. Select the code and copy it.";
      clearTimeout(button._reset);
      button._reset = setTimeout(() => {
        button.textContent = "Copy";
        delete button.dataset.state;
      }, 2000);
    });
  });

  // Search: load the index on first use, then rank pages in memory.
  const dialog = document.getElementById("search");
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  const count = document.getElementById("search-count");
  let index = null;
  let loading = null;

  function loadIndex() {
    if (!loading) {
      loading = fetch(INDEX_URL)
        .then((r) => {
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        })
        .then((data) => {
          index = data.map((page) => ({
            ...page,
            lowTitle: page.title.toLowerCase(),
            lowHeadings: page.headings.map((h) => h.text.toLowerCase()),
            lowText: page.text.toLowerCase(),
          }));
        })
        .catch(() => {
          loading = null;
          count.textContent = "Search is not available. Use the menu to find a page.";
        });
    }
    return loading;
  }

  // Each word matches at the start of a word, so "key" finds "keys" but not "monkey".
  function terms(query) {
    return query
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean)
      .map((word) => ({ word, re: new RegExp(`(^|[^\\p{L}\\p{N}])(${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "iu") }));
  }

  function score(page, words) {
    let total = 0;
    let anchor = null;
    for (const { re } of words) {
      let s = 0;
      if (re.test(page.lowTitle)) s += 10;
      const h = page.lowHeadings.findIndex((text) => re.test(text));
      if (h >= 0) {
        s += 5;
        if (!anchor) anchor = page.headings[h];
      }
      // More mentions rank higher, up to a cap, so one long page cannot win on volume.
      const mentions = page.lowText.match(new RegExp(re.source, "giu"));
      if (mentions) s += Math.min(mentions.length, 5);
      if (s === 0) return null; // every word must match somewhere
      total += s;
    }
    return { total, anchor };
  }

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  // Wrap matches in <mark>. Work on the raw text, then escape each piece.
  function highlight(text, words) {
    const pattern = new RegExp(words.map(({ re }) => re.source).join("|"), "giu");
    let out = "";
    let last = 0;
    for (const m of text.matchAll(pattern)) {
      const lead = m.slice(1).find((g, i) => i % 2 === 0 && g !== undefined) ?? "";
      const hit = m.slice(1).find((g, i) => i % 2 === 1 && g !== undefined) ?? "";
      const start = m.index + lead.length;
      out += escapeHtml(text.slice(last, start)) + `<mark>${escapeHtml(hit)}</mark>`;
      last = start + hit.length;
    }
    return out + escapeHtml(text.slice(last));
  }

  function snippet(page, words) {
    const m = words[0].re.exec(page.text);
    if (!m) return page.summary;
    const at = m.index + m[1].length;
    const start = Math.max(0, page.text.lastIndexOf(" ", Math.max(0, at - 60)));
    const end = Math.min(page.text.length, at + 120);
    return (start > 0 ? "… " : "") + page.text.slice(start, end).trim() + (end < page.text.length ? " …" : "");
  }

  function render() {
    const words = terms(input.value);
    results.replaceChildren();
    if (!words.length) {
      count.textContent = "";
      return;
    }
    if (!index) {
      count.textContent = "Loading search…";
      return;
    }
    const found = index
      .map((page) => ({ page, hit: score(page, words) }))
      .filter((r) => r.hit)
      .sort((a, b) => b.hit.total - a.hit.total)
      .slice(0, 12);
    count.textContent = found.length
      ? `${found.length} ${found.length === 1 ? "page" : "pages"} found`
      : `No pages match “${input.value.trim()}”.`;
    for (const { page, hit } of found) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = page.url + (hit.anchor ? `#${hit.anchor.id}` : "");
      const where = hit.anchor ? ` <small>› ${escapeHtml(hit.anchor.text)}</small>` : "";
      a.innerHTML =
        `<span class="sr-title">${highlight(page.title, words)}${where}</span>` +
        `<span class="sr-snippet">${escapeHtml(page.section)} · ${highlight(snippet(page, words), words)}</span>`;
      a.addEventListener("click", () => dialog.close());
      li.append(a);
      results.append(li);
    }
  }

  function openSearch() {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    input.select();
    loadIndex().then(render);
  }

  if (dialog && typeof dialog.showModal === "function") {
    document.querySelectorAll(".search-open").forEach((b) => b.addEventListener("click", openSearch));
    input.addEventListener("input", render);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const first = results.querySelector("a");
        if (first) first.click();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        results.querySelector("a")?.focus();
      }
    });
    results.addEventListener("keydown", (event) => {
      const links = [...results.querySelectorAll("a")];
      const at = links.indexOf(document.activeElement);
      if (event.key === "ArrowDown" && at < links.length - 1) {
        event.preventDefault();
        links[at + 1].focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        (at > 0 ? links[at - 1] : input).focus();
      }
    });
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close(); // click on the backdrop
    });
    document.addEventListener("keydown", (event) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "") || document.activeElement?.isContentEditable;
      if (event.key === "/" && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        openSearch();
      }
    });
  } else {
    document.querySelectorAll(".search-open").forEach((b) => (b.hidden = true));
  }

  // Close the phone menu after choosing a link on the same page.
  document.querySelectorAll(".docs-menu a").forEach((a) =>
    a.addEventListener("click", () => a.closest("details").removeAttribute("open")),
  );

  // Mark the section being read in the desktop "On this page" list.
  const tocLinks = [...document.querySelectorAll(".toc a")];
  if (tocLinks.length && "IntersectionObserver" in window) {
    const byId = new Map(tocLinks.map((a) => [decodeURIComponent(a.hash.slice(1)), a]));
    const visible = new Set();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) (e.isIntersecting ? visible.add : visible.delete).call(visible, e.target.id);
        const current = [...byId.keys()].find((id) => visible.has(id));
        if (!current) return;
        tocLinks.forEach((a) => a.removeAttribute("aria-current"));
        byId.get(current).setAttribute("aria-current", "true");
      },
      { rootMargin: "-64px 0px -60% 0px" },
    );
    byId.forEach((_, id) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });
  }
})();

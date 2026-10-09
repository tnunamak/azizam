// Runs inside the page (plain browser JS, evaluated as a string so no build
// helper can leak in). Collects what the sweep rules need from one screen:
// overflow, visible text, and controls hidden under the phone bottom nav.
// Every element it reports gets data-sweep-ref so the runner can outline it.
(async function probe(options) {
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  let nextRef = Number(document.body.dataset.sweepNextRef || "0");
  const refOf = (el) => {
    if (!el.dataset.sweepRef) {
      el.dataset.sweepRef = String(nextRef++);
      document.body.dataset.sweepNextRef = String(nextRef);
    }
    return Number(el.dataset.sweepRef);
  };
  const clip = (text, max = 160) =>
    text.length > max ? `${text.slice(0, max - 1)}…` : text;
  const norm = (text) => text.replace(/\s+/g, " ").trim();

  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    if (typeof el.checkVisibility === "function" &&
      !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 1 && rect.height <= 1) return false; // sr-only
    const style = getComputedStyle(el);
    if (style.clipPath === "inset(50%)" || style.clip === "rect(0px, 0px, 0px, 0px)") return false;
    return true;
  };

  const cssPath = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      if (node.id && !/[:]|^radix-|^r\d/.test(node.id)) {
        parts.unshift(`${part}#${CSS.escape(node.id)}`);
        break;
      }
      const slot = node.getAttribute("data-slot");
      const role = node.getAttribute("role");
      const label = node.getAttribute("aria-label");
      if (slot) part += `[data-slot="${slot}"]`;
      else if (role) part += `[role="${role}"]`;
      if (label && label.length <= 40) part += `[aria-label="${label.replace(/"/g, '\\"')}"]`;
      const parent = node.parentElement;
      if (parent && !label) {
        const same = [...parent.children].filter((child) => child.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      if (["MAIN", "NAV", "HEADER", "BODY"].includes(node.tagName) || role === "dialog") break;
      node = parent;
    }
    return parts.join(" > ");
  };

  // Section, card or dialog heading that names where a finding sits.
  const contextOf = (el) => {
    const box = el.closest("[role=dialog], [role=menu], [role=listbox], article, section, li, tr, main, nav, header");
    if (!box) return "";
    const named = box.getAttribute("aria-label");
    if (named) return clip(named, 60);
    const heading = box.querySelector("h1, h2, h3, h4, [data-slot=card-title], th");
    return heading ? clip(norm(heading.textContent || ""), 60) : box.tagName.toLowerCase();
  };

  // ---------- Overflow ----------
  const overflow = [];
  const docWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
  if (docWidth > vw + 1) {
    overflow.push({
      selector: "html",
      ref: -1,
      text: `Page scrolls sideways: content is ${docWidth}px wide in a ${vw}px viewport`,
      right: docWidth,
    });
  }
  const clippedByAncestor = (el) => {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (["hidden", "clip", "auto", "scroll"].includes(style.overflowX)) {
        const rect = node.getBoundingClientRect();
        if (rect.left >= -1 && rect.right <= vw + 1) return true;
      }
    }
    return false;
  };
  const flagged = new Set();
  const all = document.body.querySelectorAll("*");
  for (const el of all) {
    if (el.closest("svg") && el.tagName.toLowerCase() !== "svg") continue;
    if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "BR"].includes(el.tagName)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    if (rect.right <= vw + 1 && rect.left >= -1) continue;
    if (!visible(el)) continue;
    // Report the outermost offender only.
    let parentFlagged = false;
    for (let node = el.parentElement; node; node = node.parentElement) {
      if (flagged.has(node)) { parentFlagged = true; break; }
    }
    if (parentFlagged) continue;
    if (clippedByAncestor(el)) continue;
    flagged.add(el);
    const side = rect.right > vw + 1 ? `right edge at ${Math.round(rect.right)}px` : `left edge at ${Math.round(rect.left)}px`;
    overflow.push({
      selector: cssPath(el),
      ref: refOf(el),
      context: contextOf(el),
      text: `${side} (viewport ${vw}px): ${clip(norm(el.innerText || el.textContent || ""), 100) || `<${el.tagName.toLowerCase()}>`}`,
      right: Math.round(rect.right),
    });
    if (overflow.length > 40) break;
  }
  // Floating layers (popovers, menus, dialogs) must also fit vertically.
  for (const layer of document.querySelectorAll("[data-radix-popper-content-wrapper], [role=dialog], [role=menu], [role=listbox], [data-slot=popover-content], [data-slot=select-content]")) {
    if (!visible(layer)) continue;
    // Only layers that float (portaled popovers, menus, dialogs); inline listboxes scroll with the page.
    const floats = layer.closest("[data-radix-popper-content-wrapper]") ||
      ["fixed", "absolute"].includes(getComputedStyle(layer).position);
    if (!floats) continue;
    const rect = layer.getBoundingClientRect();
    const style = getComputedStyle(layer);
    const scrolls = ["auto", "scroll"].includes(style.overflowY) && layer.scrollHeight > layer.clientHeight;
    if ((rect.bottom > vh + 1 || rect.top < -1) && !scrolls && !flagged.has(layer)) {
      flagged.add(layer);
      overflow.push({
        selector: cssPath(layer),
        ref: refOf(layer),
        context: contextOf(layer),
        text: `Floating layer runs off screen vertically (top ${Math.round(rect.top)}px, bottom ${Math.round(rect.bottom)}px, viewport ${vh}px) and does not scroll`,
        right: Math.round(rect.right),
      });
    }
  }

  // ---------- Visible text ----------
  const texts = [];
  const blockOf = (el) => {
    let node = el;
    while (node.parentElement && node !== document.body) {
      const display = getComputedStyle(node).display;
      if (!display.startsWith("inline") && display !== "contents") return node;
      node = node.parentElement;
    }
    return node;
  };
  const labelOf = (el) => {
    const dd = el.closest("dd");
    if (dd && dd.previousElementSibling) return norm(dd.previousElementSibling.textContent || "");
    for (const node of [el, el.parentElement]) {
      const prev = node && node.previousElementSibling;
      if (prev) {
        const text = norm(prev.textContent || "");
        if (text && text.length <= 30) return text;
      }
    }
    return "";
  };
  const groups = new Map();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const visibleCache = new Map();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.nodeValue || !node.nodeValue.trim()) continue;
    const parent = node.parentElement;
    if (!parent || parent.closest("script, style, noscript, template, option, [data-sweep-ignore]")) continue;
    let ok = visibleCache.get(parent);
    if (ok === undefined) { ok = visible(parent); visibleCache.set(parent, ok); }
    if (!ok) continue;
    const block = blockOf(parent);
    if (!groups.has(block)) groups.set(block, []);
    groups.get(block).push(node.nodeValue);
  }
  for (const [block, parts] of groups) {
    const text = norm(parts.join(" "));
    if (!text) continue;
    texts.push({ text: clip(text, 600), kind: "text", selector: cssPath(block), ref: refOf(block), label: labelOf(block), context: contextOf(block) });
  }
  for (const field of document.querySelectorAll("input, textarea, select, [title]")) {
    if (!visible(field)) continue;
    const type = (field.getAttribute("type") || "text").toLowerCase();
    const base = { selector: cssPath(field), ref: refOf(field), label: labelOf(field), context: contextOf(field) };
    if (field.tagName === "SELECT") {
      const chosen = field.selectedOptions && field.selectedOptions[0];
      if (chosen) texts.push({ ...base, text: norm(chosen.textContent || ""), kind: "value" });
      continue;
    }
    if (["INPUT", "TEXTAREA"].includes(field.tagName)) {
      if (["hidden", "file", "password", "checkbox", "radio", "range", "color"].includes(type)) continue;
      if (field.placeholder) texts.push({ ...base, text: norm(field.placeholder), kind: "placeholder" });
      if (field.value) texts.push({ ...base, text: clip(norm(field.value), 600), kind: "value" });
    }
    const title = field.getAttribute("title");
    if (title) texts.push({ ...base, text: norm(title), kind: "title" });
  }

  // ---------- Phone bottom nav ----------
  const bottomNav = [];
  const nav = document.querySelector('nav[aria-label="Phone navigation"]');
  if (options.phone && nav && visible(nav)) {
    const dialogs = [...document.querySelectorAll("[role=dialog], [role=alertdialog]")].filter(visible);
    const scope = dialogs.length ? dialogs[dialogs.length - 1] : document.querySelector("main");
    const navCovers = (x, y) => {
      const hit = document.elementFromPoint(x, y);
      return hit && nav.contains(hit);
    };
    if (scope) {
      window.scrollTo(0, document.documentElement.scrollHeight);
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const interactive = [...scope.querySelectorAll(
        'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab], [role=switch], [role=checkbox], [role=link], [tabindex]:not([tabindex="-1"])',
      )].filter((el) => !el.closest("[aria-hidden=true]") && !el.disabled && visible(el) && !nav.contains(el));
      const last = interactive[interactive.length - 1];
      if (last) {
        // Scroll a nested scroll box to its end too, as a person would.
        for (let node = last.parentElement; node && node !== scope.parentElement; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (["auto", "scroll"].includes(style.overflowY) && node.scrollHeight > node.clientHeight) {
            node.scrollTop = node.scrollHeight;
          }
        }
        await new Promise((resolve) => requestAnimationFrame(resolve));
      }
      const navTop = nav.getBoundingClientRect().top;
      for (const el of interactive) {
        const rect = el.getBoundingClientRect();
        if (rect.bottom <= navTop + 1 || rect.top >= vh) continue;
        const cx = Math.min(Math.max(rect.left + rect.width / 2, 1), vw - 1);
        const cy = Math.min(Math.max(rect.top + rect.height / 2, 1), vh - 1);
        if (!navCovers(cx, cy)) continue;
        bottomNav.push({
          selector: cssPath(el),
          ref: refOf(el),
          context: contextOf(el),
          text: `${el === last ? "Last control on the page" : "Control"} sits under the bottom nav at full scroll: ${clip(norm(el.innerText || el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.tagName.toLowerCase()), 80)}`,
        });
      }
      window.scrollTo(0, 0);
    }
  }

  return { vw, vh, overflow, texts, bottomNav };
})

#!/usr/bin/env node
// Merge per-test sweep findings into sweep.json and a human report grouped
// surface → rule → finding. Usage: node e2e/sweep/report.mjs [fixtures|live|copy] [--out file.md]
// --out also copies the screenshots the report cites to <file>-shots/ so the
// report survives deleting test-results/.
import { existsSync, readdirSync, readFileSync, writeFileSync, copyFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const targetName = args.find((arg) => ["fixtures", "live", "copy"].includes(arg)) ?? process.env.SWEEP_TARGET ?? "fixtures";
const outIndex = args.indexOf("--out");
const out = outIndex >= 0 ? path.resolve(args[outIndex + 1]) : null;
const dir = path.resolve(here, "../../test-results/sweep", targetName);
const findingsDir = path.join(dir, "findings");
if (!existsSync(findingsDir)) throw new Error(`No sweep results in ${findingsDir}; run the sweep first`);

const RULES = {
  overflow: "Horizontal overflow",
  "bottom-nav": "Hidden under the phone bottom nav",
  path: "Filesystem path rendered",
  "raw-id": "Raw id rendered",
  "research-note": "Internal research note rendered",
  "raw-error": "Raw error text",
  "member-jargon": "Developer jargon on a member screen",
  unknown: "Visible “Unknown” state",
  axe: "axe WCAG AA",
  "page-crash": "Uncaught script error",
};
const RULE_ORDER = Object.keys(RULES);
const SURFACE_ORDER = [
  "Home", "Status", "Library", "Try", "People and invites", "Sign-in and invite acceptance", "Usage",
  "Activity", "Models", "Workflows", "Engines", "Providers", "Apps and keys", "Settings",
  "Setup and first run", "Page not found", "Design reference (dev)",
];

const context = existsSync(path.join(dir, "context.json")) ? JSON.parse(readFileSync(path.join(dir, "context.json"), "utf8")) : {};
const runStats = existsSync(path.join(dir, "playwright.json")) ? JSON.parse(readFileSync(path.join(dir, "playwright.json"), "utf8")).stats : null;
const findings = [];
const states = [];
for (const file of readdirSync(findingsDir).filter((name) => name.endsWith(".json")).sort()) {
  const data = JSON.parse(readFileSync(path.join(findingsDir, file), "utf8"));
  findings.push(...data.findings);
  states.push(...data.states);
}

// One defect usually shows in several views (widths, themes, roles); merge them.
const keyOf = (f) => {
  const text =
    f.rule === "overflow" ? f.text.replace(/^.*?\): /, "").replace(/\d+px/g, "Npx")
    : f.rule === "axe" ? f.text.split(":")[0]
    : f.rule === "bottom-nav" ? f.text
    : f.text;
  return [f.surface, f.rule, f.severity, f.selector.replace(/:nth-of-type\(\d+\)/g, ""), text].join("|");
};
const merged = new Map();
for (const f of findings) {
  const key = keyOf(f);
  let entry = merged.get(key);
  if (!entry) {
    entry = { rule: f.rule, severity: f.severity, surface: f.surface, text: f.text, selector: f.selector, context: f.context, note: f.note, screenshot: f.screenshot, views: new Set(), states: new Set(), routes: new Set(), count: 0 };
    merged.set(key, entry);
  }
  entry.count++;
  entry.views.add(`${f.role} ${f.width} ${f.theme}`);
  entry.states.add(f.state);
  entry.routes.add(f.route);
  if (!entry.screenshot && f.screenshot) entry.screenshot = f.screenshot;
}
const unique = [...merged.values()].map((entry) => ({
  ...entry,
  views: [...entry.views].sort(),
  states: [...entry.states].sort(),
  routes: [...entry.routes].sort(),
}));

const surfaces = [...new Set(unique.map((f) => f.surface).concat(states.map((s) => s.surface)))].sort(
  (a, b) => (SURFACE_ORDER.indexOf(a) + 1 || 99) - (SURFACE_ORDER.indexOf(b) + 1 || 99),
);
const failing = unique.filter((f) => f.severity === "fail");
const counts = {};
for (const surface of surfaces) {
  counts[surface] = {};
  for (const rule of RULE_ORDER) {
    const n = failing.filter((f) => f.surface === surface && f.rule === rule).length;
    if (n) counts[surface][rule] = n;
  }
}

const checked = states.filter((s) => s.status === "checked").length;
const skipped = states.filter((s) => s.status === "skipped");
const errored = states.filter((s) => s.status === "error");
const summary = {
  target: targetName,
  generated_at: new Date().toISOString(),
  started_at: context.startedAt,
  duration_s: runStats ? Math.round(runStats.duration / 1000) : null,
  base_url: context.base,
  picture_workflow: context.pictureWorkflow ?? null,
  pages_checked: checked,
  states_skipped: skipped.length,
  states_errored: errored.length,
  states_still_loading: states.filter((s) => s.status === "checked" && s.loading).length,
  unique_findings: unique.length,
  unique_failing: failing.length,
  raw_findings: findings.length,
  counts,
};
writeFileSync(path.join(dir, "sweep.json"), JSON.stringify({ summary, findings: unique, raw_findings: findings, states }, null, 1));

const esc = (text = "") => String(text).replace(/\|/g, "\\|").replace(/</g, "&lt;").replace(/\n/g, " ");
const viewsLabel = (views) => {
  const roles = [...new Set(views.map((v) => v.split(" ")[0]))];
  const widths = [...new Set(views.map((v) => v.split(" ")[1]))];
  const themes = [...new Set(views.map((v) => v.split(" ")[2]))];
  return `${roles.join("+")} · ${widths.join("/")}px · ${themes.length === 2 ? "both themes" : themes[0]}`;
};

// With --out, cited screenshots move next to the saved report.
const shotDir = out ? out.replace(/\.md$/, "") + "-shots" : null;
const cited = new Set();
const shotPath = (shot) => {
  cited.add(shot);
  return shotDir ? path.join(shotDir, path.basename(shot)) : path.join(dir, shot);
};

const lines = [];
lines.push(`# UI quality sweep — ${targetName}`, "");
lines.push(`Generated ${summary.generated_at} against ${context.base ?? "?"}${summary.duration_s ? `, run took ${Math.floor(summary.duration_s / 60)} min ${summary.duration_s % 60} s` : ""}.`);
lines.push(`${checked} page states checked (${skipped.length} skipped, ${errored.length} could not be swept). ${failing.length} unique failing findings (${unique.length - failing.length} warnings), from ${findings.length} raw hits across widths, themes and roles.`, "");
lines.push("Machine-readable: `sweep.json` beside this report. Screenshots outline flagged elements in magenta.", "");
lines.push("## Counts (unique failing findings)", "");
const usedRules = RULE_ORDER.filter((rule) => surfaces.some((s) => counts[s][rule]));
lines.push(`| Surface | ${usedRules.map((r) => RULES[r]).join(" | ")} | Total |`);
lines.push(`|---|${usedRules.map(() => "---:").join("|")}|---:|`);
for (const surface of surfaces) {
  const row = usedRules.map((rule) => counts[surface][rule] ?? "");
  const total = Object.values(counts[surface]).reduce((a, b) => a + b, 0);
  lines.push(`| ${surface} | ${row.join(" | ")} | ${total} |`);
}
const totals = usedRules.map((rule) => surfaces.reduce((n, s) => n + (counts[s][rule] ?? 0), 0));
lines.push(`| **All** | ${totals.join(" | ")} | ${failing.length} |`, "");

for (const surface of surfaces) {
  const mine = unique.filter((f) => f.surface === surface);
  if (!mine.length) continue;
  lines.push(`## ${surface}`, "");
  for (const rule of RULE_ORDER) {
    const hits = mine.filter((f) => f.rule === rule).sort((a, b) => (a.severity === b.severity ? b.count - a.count : a.severity === "fail" ? -1 : 1));
    if (!hits.length) continue;
    lines.push(`### ${RULES[rule]} (${hits.filter((h) => h.severity === "fail").length} failing${hits.some((h) => h.severity === "warn") ? `, ${hits.filter((h) => h.severity === "warn").length} warnings` : ""})`, "");
    for (const hit of hits.slice(0, 40)) {
      lines.push(`- ${hit.severity === "warn" ? "(warn) " : ""}“${esc(hit.text.slice(0, 260))}”`);
      lines.push(`  - where: ${hit.routes.join(", ")} · states: ${hit.states.join(", ")} · ${viewsLabel(hit.views)}${hit.context ? ` · in “${esc(hit.context)}”` : ""}`);
      lines.push(`  - selector: \`${hit.selector || "(page)"}\``);
      if (hit.note) lines.push(`  - note: ${esc(hit.note)}`);
      if (hit.screenshot) lines.push(`  - screenshot: ${shotPath(hit.screenshot)}`);
    }
    if (hits.length > 40) lines.push(`- … ${hits.length - 40} more in sweep.json`);
    lines.push("");
  }
}

const loading = states.filter((s) => s.status === "checked" && s.loading);
if (loading.length) {
  lines.push("## Checked while still loading", "");
  lines.push("These states still showed a loading indicator when probed; their findings may be incomplete.", "");
  const seen = new Map();
  for (const s of loading) {
    const key = `${s.surface}|${s.route}|${s.state}`;
    const entry = seen.get(key) ?? { ...s, views: new Set() };
    entry.views.add(`${s.role} ${s.width} ${s.theme}`);
    seen.set(key, entry);
  }
  for (const s of seen.values()) lines.push(`- ${s.surface} \`${s.route}\` ${s.state} (${viewsLabel([...s.views])})`);
  lines.push("");
}

if (skipped.length || errored.length) {
  lines.push("## States not swept", "");
  const seen = new Map();
  for (const s of [...errored, ...skipped]) {
    const key = `${s.status}|${s.surface}|${s.state}|${s.reason}`;
    const entry = seen.get(key) ?? { ...s, views: new Set() };
    entry.views.add(`${s.role} ${s.width} ${s.theme}`);
    seen.set(key, entry);
  }
  for (const s of seen.values())
    lines.push(`- ${s.status === "error" ? "**could not sweep**" : "skipped"}: ${s.surface} \`${s.route}\` ${s.state} (${viewsLabel([...s.views])}): ${esc(s.reason)}`);
  lines.push("");
}

writeFileSync(path.join(dir, "report.md"), `${lines.join("\n")}\n`);
if (out) {
  mkdirSync(path.dirname(out), { recursive: true });
  copyFileSync(path.join(dir, "report.md"), out);
  copyFileSync(path.join(dir, "sweep.json"), out.replace(/\.md$/, "") + ".json");
  if (cited.size) mkdirSync(shotDir, { recursive: true });
  for (const shot of cited) if (existsSync(path.join(dir, shot))) copyFileSync(path.join(dir, shot), path.join(shotDir, path.basename(shot)));
}
console.log(`[sweep] ${checked} states, ${failing.length} unique failing findings → ${path.join(dir, "report.md")}${out ? ` and ${out}` : ""}`);

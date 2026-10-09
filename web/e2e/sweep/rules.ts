// Text rules for the quality sweep. Pure functions (no Playwright), so they are
// unit-tested in tests/sweep-rules.spec.ts and tuned without a browser.
import { readFileSync } from "node:fs";
import path from "node:path";

export type Role = "owner" | "member" | "anonymous";
export type Severity = "fail" | "warn";

export type RuleId =
  | "overflow"
  | "path"
  | "raw-id"
  | "research-note"
  | "raw-error"
  | "member-jargon"
  | "unknown"
  | "axe"
  | "bottom-nav"
  | "page-crash";

export const RULES: Record<RuleId, string> = {
  overflow: "Horizontal overflow (page or element past the viewport edge)",
  path: "Filesystem path rendered",
  "raw-id": "Raw id rendered (uuid, req_…, long hex)",
  "research-note": "Internal research note rendered",
  "raw-error": "Raw error text rendered",
  "member-jargon": "Developer jargon on a member screen",
  unknown: "Visible “Unknown” state",
  axe: "axe-core WCAG 2.x AA violation",
  "bottom-nav": "Content hidden under the phone bottom nav",
  "page-crash": "Uncaught script error in the page",
};

/** One piece of visible text, gathered in the page by probe.ts. */
export interface TextItem {
  text: string;
  /** text: rendered text; placeholder/value: form fields; title: tooltip. */
  kind: "text" | "placeholder" | "value" | "title";
  selector: string;
  /** Index into the probe's element list, used to outline it in screenshots. */
  ref: number;
  /** Text of the nearest label-like neighbour (dt, label, th), if any. */
  label?: string;
}

export interface TextFinding {
  rule: RuleId;
  severity: Severity;
  /** Snippet around the match. */
  text: string;
  match: string;
  selector: string;
  ref: number;
  note?: string;
}

const LIST_DIR = path.join(__dirname, "lists");

/** Load a term list: plain words match whole-word, case-insensitive, optional plural. */
export function loadTermList(file: string): Array<{ term: string; re: RegExp }> {
  const raw = readFileSync(path.isAbsolute(file) ? file : path.join(LIST_DIR, file), "utf8");
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      if (line.startsWith("re:")) return { term: line.slice(3), re: new RegExp(line.slice(3), "i") };
      const escaped = line.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/ /g, "\\s+");
      return { term: line, re: new RegExp(`(?<![\\w-])${escaped}s?(?![\\w-])`, "i") };
    });
}

// Filesystem paths. "/home/" must not match app URLs such as /ui/home/.
export const PATH_PATTERNS: RegExp[] = [
  /(?<![\w.\/:-])\/(?:media|home|mnt|srv|opt|Users|tmp|var\/lib|root)\/[^\s]/,
  /\b[A-Za-z]:\\[^\s]/,
  /[\w)\]-]\.(?:gguf|safetensors|ckpt|pth|onnx)\b/i,
  /(?:^|[\s"'(=:@])\.{1,2}\/[\w-]+\.\w+/, // ./source-image.bin
  /(?<![\w\/])~\/[\w.-]+\//,
];

// Raw ids. Hex needs both a digit and a letter so plain numbers pass.
export const ID_PATTERNS: RegExp[] = [
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i,
  /\breq_[A-Za-z0-9_]{6,}/,
  /\b(?:img|aud|vid|usr|svc|job|inv|prof|cred|run|wf|asset)_[A-Za-z0-9]{8,}\b/,
  /\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{12,}\b/,
];

// Raw errors: what the code or the network said, not what a person should read.
export const ERROR_PATTERNS: RegExp[] = [
  /Failed to fetch/i,
  /\bNetworkError\b/,
  /\b(?:Type|Reference|Syntax|Range)Error\b/,
  /\bUnexpected (?:token|end of JSON)/,
  /\[object Object\]/,
  /(?<![\w-])undefined(?![\w-])/,
  /(?<![\w-])NaN(?![\w-])/,
  /^null$/,
  /\bat [\w.$<>]+ \(?[^\s()]+:\d+:\d+\)?/, // JS stack frame
  /File "[^"]+", line \d+/, // Python stack frame
  /Traceback \(most recent call last\)/,
  /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b[^.]{0,40}\bis required\b/, // field_name is required
  /\bInternal Server Error\b/i,
  /\bHTTP [45]\d\d\b/,
  /\b(?:ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND)\b/,
];

const UNKNOWN = /(?<![\w-])unknown(?![\w-])/i;

// An id beside a label like "Request ID" is a deliberate support reference.
const ID_LABEL = /\b(?:request|reference|receipt|job)\s*id\b|\bID\b/;

const snippet = (text: string, index: number, length: number, width = 60) => {
  const start = Math.max(0, index - width);
  const end = Math.min(text.length, index + length + width);
  return `${start ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`;
};

export interface LintOptions {
  role: Role;
  research: Array<{ term: string; re: RegExp }>;
  jargon: Array<{ term: string; re: RegExp }>;
}

export function lintText(items: TextItem[], options: LintOptions): TextFinding[] {
  const out: TextFinding[] = [];
  const seen = new Set<string>();
  const add = (item: TextItem, rule: RuleId, re: RegExp, extra: Partial<TextFinding> = {}) => {
    const match = re.exec(item.text);
    if (!match) return false;
    const key = `${rule}|${item.selector}|${match[0]}`;
    if (seen.has(key)) return true;
    seen.add(key);
    out.push({
      rule,
      severity: "fail",
      text: snippet(item.text, match.index, match[0].length),
      match: match[0],
      selector: item.selector,
      ref: item.ref,
      ...(item.kind !== "text" ? { note: `in ${item.kind}` } : {}),
      ...extra,
    });
    return true;
  };
  for (const item of items) {
    if (!item.text.trim()) continue;
    for (const re of PATH_PATTERNS) if (add(item, "path", re)) break;
    for (const re of ID_PATTERNS) {
      const labelled = options.role === "owner" && ID_LABEL.test(item.label ?? "");
      if (add(item, "raw-id", re, labelled ? { severity: "warn", note: `labelled “${item.label}”` } : {})) break;
    }
    for (const re of ERROR_PATTERNS) if (add(item, "raw-error", re)) break;
    for (const { re } of options.research) if (add(item, "research-note", re)) break;
    if (options.role === "member") {
      for (const { re } of options.jargon) add(item, "member-jargon", re);
    }
    // Every visible Unknown is flagged. A short label ("Unknown", "1 unknown", "Chat unknown")
    // is a state and fails; a sentence that mentions it ("21 requests have unknown cost") warns.
    if (item.kind === "text" && item.text.length <= 160) {
      const label = item.text.length <= 40;
      add(item, "unknown", UNKNOWN, label ? {} : { severity: "warn", note: "in a sentence" });
    }
  }
  return out;
}

/**
 * Request adjustments: the stored `transforms` rows and the typed forms that
 * edit them. Pure functions, so the round trip is testable without a page.
 *
 * Rule: loading a record and saving it without a change sends the same array.
 * Each card keeps the row it came from (`orig`) and overlays only what the form
 * edits, so unknown keys such as `when` and values the form shows as text keep
 * their stored form.
 */
import type { TransformRow } from "@/lib/engines-api";

export type Phase = "request" | "response";
type Stored = Record<string, unknown>;
export interface PairRow {
  a: string;
  b: string;
  /** The stored row or value this pair came from, when it was loaded. */
  orig?: unknown;
}

export type AdjustmentCard =
  | { key: number; kind: "builtin"; name: string }
  | { key: number; kind: "other"; orig: Stored }
  | { key: number; kind: "value-map"; field: string; rows: PairRow[]; phase: Phase; orig: Stored }
  | { key: number; kind: "param-map"; rows: PairRow[]; phase: Phase; orig: Stored }
  | { key: number; kind: "drop-params"; items: string[]; phase: Phase; orig: Stored }
  | { key: number; kind: "default-params"; rows: PairRow[]; orig: Stored }
  | { key: number; kind: "message-inject"; role: string; where: "start" | "end"; text: string; orig: Stored }
  | { key: number; kind: "clamp"; field: string; min: string; max: string; phase: Phase; orig: Stored };

export type PrimitiveKind = Exclude<AdjustmentCard["kind"], "builtin" | "other">;

export const ADJUSTMENT_KINDS: { kind: PrimitiveKind; name: string; description: string }[] = [
  { kind: "value-map", name: "Replace values", description: "Change one value to another before it reaches the server." },
  { kind: "param-map", name: "Rename fields", description: "Send a field under a different name." },
  { kind: "drop-params", name: "Remove fields", description: "Leave out fields the server does not accept." },
  { kind: "default-params", name: "Set defaults", description: "Add a field when the request does not have it." },
  { kind: "message-inject", name: "Add a message", description: "Put a fixed message at the start or end of matching chat requests." },
  { kind: "clamp", name: "Keep a number in range", description: "Limit a number with a minimum, a maximum, or both." },
];

let nextKey = 1;
const key = () => nextKey++;

function phaseOf(row: Stored): Phase {
  return row.phase === "response" ? "response" : "request";
}

/** How a stored value shows in a text box. */
export function valueText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  return JSON.stringify(value);
}

function pairsFrom(map: unknown): PairRow[] {
  if (!map || typeof map !== "object") return [];
  return Object.entries(map as Stored).map(([a, value]) => ({ a, b: valueText(value), orig: value }));
}

export function cardsFromTransforms(rows: readonly TransformRow[] | undefined): AdjustmentCard[] {
  const cards: AdjustmentCard[] = [];
  for (const row of rows ?? []) {
    if (typeof row === "string") {
      cards.push({ key: key(), kind: "builtin", name: row });
      continue;
    }
    const orig = row as Stored;
    switch (row.kind) {
      case "value-map":
        cards.push({ key: key(), kind: "value-map", field: String(row.param ?? ""), rows: pairsFrom(row.map), phase: phaseOf(orig), orig });
        break;
      case "param-map": {
        const pair: PairRow = { a: String(row.from ?? ""), b: String(row.to ?? ""), orig };
        const last = cards[cards.length - 1];
        if (last?.kind === "param-map" && last.phase === phaseOf(orig)) last.rows.push(pair);
        else cards.push({ key: key(), kind: "param-map", rows: [pair], phase: phaseOf(orig), orig });
        break;
      }
      case "drop-params":
        cards.push({ key: key(), kind: "drop-params", items: (row.drop ?? []).map(String), phase: phaseOf(orig), orig });
        break;
      case "default-params":
        cards.push({ key: key(), kind: "default-params", rows: pairsFrom(row.set), orig });
        break;
      case "message-inject":
        cards.push({
          key: key(),
          kind: "message-inject",
          role: String(row.role ?? "system"),
          where: !row.mode || row.mode === "prepend" ? "start" : "end",
          text: String(row.text ?? ""),
          orig,
        });
        break;
      case "clamp":
        cards.push({
          key: key(),
          kind: "clamp",
          field: String(row.param ?? ""),
          min: row.min === undefined ? "" : String(row.min),
          max: row.max === undefined ? "" : String(row.max),
          phase: phaseOf(orig),
          orig,
        });
        break;
      default:
        cards.push({ key: key(), kind: "other", orig });
    }
  }
  return cards;
}

export function newCard(kind: PrimitiveKind): AdjustmentCard {
  const orig = { kind };
  switch (kind) {
    case "value-map":
      return { key: key(), kind, field: "", rows: [{ a: "", b: "" }], phase: "request", orig };
    case "param-map":
      return { key: key(), kind, rows: [{ a: "", b: "" }], phase: "request", orig };
    case "drop-params":
      return { key: key(), kind, items: [""], phase: "request", orig };
    case "default-params":
      return { key: key(), kind, rows: [{ a: "", b: "" }], orig };
    case "message-inject":
      return { key: key(), kind, role: "system", where: "start", text: "", orig };
    case "clamp":
      return { key: key(), kind, field: "", min: "", max: "", phase: "request", orig };
  }
}

/** Keep the stored phase key as it was; add one only when it is not the default. */
function withPhase(out: Stored, phase: Phase, orig: Stored): Stored {
  if ("phase" in orig || phase !== "request") out.phase = phase;
  return out;
}

/** The stored value when the text was not changed; otherwise numbers and true/false from text. */
function typedValue(row: PairRow, parse: boolean): unknown {
  if (row.orig !== undefined && valueText(row.orig) === row.b) return row.orig;
  if (!parse) return row.b;
  const text = row.b.trim();
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text === "true") return true;
  if (text === "false") return false;
  return row.b;
}

function numberText(text: string, stored: unknown): number | undefined {
  if (text.trim() === "") return undefined;
  if (stored !== undefined && String(stored) === text) return stored as number;
  return Number(text);
}

function mapFrom(rows: PairRow[], parse: boolean): Stored {
  const out: Stored = {};
  // A loaded row keeps its key even when it is empty; a new empty row is left out.
  for (const row of rows) if (row.a.trim() || row.orig !== undefined) out[row.a] = typedValue(row, parse);
  return out;
}

const NUMBER = /^-?\d+(\.\d+)?$/;

/** What stops the adjustment from saving, in plain words; null when it can save. */
export function cardProblem(card: AdjustmentCard): string | null {
  if (card.kind === "clamp") {
    if ([card.min, card.max].some((x) => x.trim() !== "" && !NUMBER.test(x.trim()))) return "Type the minimum and the maximum as numbers, for example 0 and 1.";
    if (card.min.trim() === "" && card.max.trim() === "") return "Type a minimum, a maximum, or both.";
  }
  return null;
}

export function transformsFromCards(cards: readonly AdjustmentCard[]): TransformRow[] {
  const out: TransformRow[] = [];
  for (const card of cards) {
    switch (card.kind) {
      case "builtin":
        out.push(card.name);
        break;
      case "other":
        out.push(card.orig as TransformRow);
        break;
      case "value-map":
        out.push(withPhase({ ...card.orig, kind: "value-map", param: card.field, map: mapFrom(card.rows, false) }, card.phase, card.orig) as TransformRow);
        break;
      case "param-map":
        for (const row of card.rows) {
          if (!row.a.trim()) continue;
          const orig = (row.orig as Stored | undefined) ?? { kind: "param-map" };
          out.push(withPhase({ ...orig, kind: "param-map", from: row.a, to: row.b }, card.phase, orig) as TransformRow);
        }
        break;
      case "drop-params":
        out.push(withPhase({ ...card.orig, kind: "drop-params", drop: card.items.filter((x) => x.trim()) }, card.phase, card.orig) as TransformRow);
        break;
      case "default-params":
        out.push({ ...card.orig, kind: "default-params", set: mapFrom(card.rows, true) } as TransformRow);
        break;
      case "message-inject": {
        const row: Stored = { ...card.orig, kind: "message-inject", role: card.role, text: card.text };
        const was = card.orig.mode;
        const wasWhere = !was || was === "prepend" ? "start" : "end";
        if (card.where !== wasWhere || was === undefined) {
          if (card.where === "start") {
            if (was !== undefined) row.mode = "prepend";
          } else row.mode = "append";
        }
        out.push(row as TransformRow);
        break;
      }
      case "clamp": {
        const row: Stored = { ...card.orig, kind: "clamp", param: card.field };
        const min = numberText(card.min, card.orig.min);
        const max = numberText(card.max, card.orig.max);
        if (min === undefined) delete row.min;
        else row.min = min;
        if (max === undefined) delete row.max;
        else row.max = max;
        out.push(withPhase(row, card.phase, card.orig) as TransformRow);
        break;
      }
    }
  }
  return out;
}

export function kindName(card: AdjustmentCard): string {
  if (card.kind === "builtin") return "Built-in adjustment";
  if (card.kind === "other") return "Other adjustment";
  return ADJUSTMENT_KINDS.find((k) => k.kind === card.kind)?.name ?? card.kind;
}

const q = (text: string) => `“${text || "…"}”`;

// Fields fully represented by the editable controls. Everything else remains
// visible as stored data; this UI does not guess what unfamiliar options mean.
const EDITABLE_KEYS: Record<PrimitiveKind, readonly string[]> = {
  "value-map": ["kind", "param", "map", "phase"],
  "param-map": ["kind", "from", "to", "phase"],
  "drop-params": ["kind", "drop", "phase"],
  "default-params": ["kind", "set"],
  "message-inject": ["kind", "role", "text"],
  clamp: ["kind", "param", "min", "max", "phase"],
};

function plainStored(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.map(plainStored).join(", ") : "(empty list)";
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value);
    return entries.length ? entries.map(([name, item]) => `${name}: ${plainStored(item)}`).join("; ") : "(empty object)";
  }
  return value === "" ? "(empty text)" : String(value);
}

function preservedOptions(card: AdjustmentCard): string[] {
  if (card.kind === "builtin" || card.kind === "other") return [];
  const rows = card.kind === "param-map" ? card.rows.map((row) => row.orig ?? {}) : [card.orig];
  return rows.flatMap((row, index) => {
    const extras = Object.entries(row as Stored).filter(([name, value]) => {
      if (name === "phase" && value !== "request" && value !== "response") return true;
      if (card.kind === "message-inject" && name === "mode" && (value === "prepend" || value === "append")) return false;
      return !EDITABLE_KEYS[card.kind as PrimitiveKind].includes(name);
    });
    if (!extras.length) return [];
    const text = extras.map(([name, value]) => `${name}: ${plainStored(value)}`).join("; ");
    return [card.kind === "param-map" ? `Row ${index + 1}: ${text}` : text];
  });
}

/** One plain sentence that says what the adjustment does. */
export function sayAdjustment(card: AdjustmentCard): string {
  const extras = preservedOptions(card);
  if (extras.length) return `Configured adjustment: ${kindName(card)}. Preserved conditions and options (read-only, not interpreted here): ${extras.join(" | ")}.`;
  const where = "phase" in card && card.phase === "response" ? "replies" : "requests";
  switch (card.kind) {
    case "builtin":
      return `Built-in adjustment: ${card.name}. Its behavior is not described here. Read-only.`;
    case "other":
      return `Unsupported adjustment type: ${String(card.orig.kind ?? "unknown")}. Kept as stored, read-only.`;
    case "value-map": {
      const pairs = card.rows.filter((r) => r.a);
      return `In ${where}, Azizam changes the value of ${q(card.field)}${
        pairs.length ? `: ${pairs.map((r) => `${q(r.a)} becomes ${q(r.b)}`).join("; ")}.` : "."
      }`;
    }
    case "param-map": {
      const pairs = card.rows.filter((r) => r.a).map((r) => `${q(r.a)} to ${q(r.b)}`);
      return `In ${where}, Azizam renames ${pairs.join(" and ") || q("")}.`;
    }
    case "drop-params": {
      const items = card.items.filter(Boolean);
      return `Azizam leaves ${items.length ? items.map(q).join(", ") : q("")} out of ${where}.`;
    }
    case "default-params": {
      const pairs = card.rows.filter((r) => r.a).map((r) => `${q(r.a)} = ${q(r.b)}`);
      return `Azizam adds ${pairs.join(" and ") || q("")} to requests that do not set it.`;
    }
    case "message-inject":
      return `Azizam adds ${card.role === "system" ? "instructions" : "a user message"} at the ${card.where} of matching chat requests: ${q(card.text)}.`;
    case "clamp": {
      const min = card.min.trim();
      const max = card.max.trim();
      if (!min && !max) return `The range for ${q(card.field)} needs a minimum, a maximum, or both.`;
      if (!max) return `In ${where}, Azizam keeps ${q(card.field)} at or above ${min}. A lower value moves to the minimum.`;
      if (!min) return `In ${where}, Azizam keeps ${q(card.field)} at or below ${max}. A higher value moves to the maximum.`;
      return `In ${where}, Azizam keeps ${q(card.field)} between ${min} and ${max}. A value outside moves to the nearest limit.`;
    }
  }
}

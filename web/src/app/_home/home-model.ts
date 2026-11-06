import {
  isInFlight,
  isSuccess,
  requestCapability,
  type CapabilityView,
} from "@/lib/capability-state";
import { knownCapabilityLabel } from "@/components/product/readiness";
import { appPlumbingCapabilities } from "@/lib/terms";
import type { HomeCapability, HomeChat, HomeCreation, HomeReceipt } from "./home-api";

/*
 * What Home shows, as pure functions of what the gateway returned. Nothing here names a
 * capability, model or engine: the rows are the data. The copy below is per kind of work
 * (a placeholder, a verb), keyed by the kind the rows carry, with a plain default.
 */

export type MakingKind = "image" | "video" | "audio";

/** The kind a capability row makes: its icon key, the same word the URL and Try use. */
export function kindOf(row: HomeCapability): string {
  return row.icon_key ?? row.capability;
}

/** The chooser offers what a person makes; apps' plumbing (search, decisions) never appears. */
export function chooserRows(rows: HomeCapability[]): HomeCapability[] {
  return rows.filter((row) => !appPlumbingCapabilities.has(kindOf(row)));
}

/** Use the gateway's label when present; older or partial rows use the shared term. */
export function capabilityLabel(row: HomeCapability): string {
  return row.capability_title ?? knownCapabilityLabel(kindOf(row)) ?? row.capability;
}

/** How often, and when last, this person got a result from each kind of work. */
function taskUse(receipts: HomeReceipt[]) {
  const uses = new Map<string, { count: number; last: number }>();
  for (const receipt of receipts) {
    if (!isSuccess(receipt.status)) continue;
    const kind = requestCapability(receipt.intent, receipt.path);
    if (!kind) continue;
    const at = receipt.accepted_at_ms
      ? receipt.accepted_at_ms / 1000
      : (receipt._stored_at ?? 0);
    const use = uses.get(kind) ?? { count: 0, last: 0 };
    uses.set(kind, { count: use.count + 1, last: Math.max(use.last, at) });
  }
  return uses;
}

/** The person's most-used kinds first, then the gateway's own order. */
export function orderChooser(rows: HomeCapability[], receipts: HomeReceipt[]): HomeCapability[] {
  const uses = taskUse(receipts);
  return rows
    .map((row, index) => ({ row, index, use: uses.get(kindOf(row)) }))
    .sort(
      (a, b) =>
        (b.use?.count ?? 0) - (a.use?.count ?? 0) ||
        (b.use?.last ?? 0) - (a.use?.last ?? 0) ||
        a.index - b.index,
    )
    .map(({ row }) => row);
}

/** "about 9 s", "about 3 min"; null when no data says how long. Never "unknown". */
export function formatWait(seconds: number | null | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds < 60) return `about ${Math.max(1, Math.round(seconds))} s`;
  return `about ${Math.round(seconds / 60)} min`;
}

function formatUsd(amount: number): string {
  return amount < 0.01 ? amount.toFixed(3) : amount.toFixed(2);
}

/** The cost words for a row, in the order the composer shows them. Empty when nothing is known. */
function costParts(row: HomeCapability): string[] {
  const hint = row.cost_hint;
  if (!hint || hint.kind === "unknown") return [];
  if (hint.kind === "free") return ["No charge"];
  const amount = hint.price_usd;
  return amount == null ? ["Paid model"] : ["Paid model", `about USD ${formatUsd(amount)}`];
}

/**
 * The facts line under the composer: cost, then wait. A chat starts in about a second, so its
 * wait reads "starts in". The first word is capitalised; a missing fact is left out.
 */
export function runFacts(row: HomeCapability): string {
  const wait = formatWait(row.typical_seconds);
  const waitText = wait && kindOf(row) === "chat" ? `starts in ${wait}` : wait;
  const line = [...costParts(row), ...(waitText ? [waitText] : [])].join(" · ");
  return line.charAt(0).toUpperCase() + line.slice(1);
}

/**
 * The line under a card in "What you can make here": the wait, then the cost in lower case. Every kind has the same
 * shape ("About 1 s · no charge"), so no caption wraps at 768 px; the composer's facts line keeps "starts in".
 */
export function listFacts(row: HomeCapability): string {
  const wait = formatWait(row.typical_seconds);
  const cost = row.cost_hint?.kind === "free" ? "no charge" : row.cost_hint?.kind === "paid" ? "paid model" : null;
  const amount = row.cost_hint?.kind === "paid" && row.cost_hint.price_usd != null
    ? `about USD ${formatUsd(row.cost_hint.price_usd)}`
    : null;
  const line = [wait, cost, amount].filter(Boolean).join(" · ");
  return line.charAt(0).toUpperCase() + line.slice(1);
}

/** The composer's placeholder: the capability's own `prompt_hint` (from its manifest), else a plain default. */
export function promptPlaceholder(row: HomeCapability | null): string {
  return row?.prompt_hint?.trim() || "Describe what you want";
}

/**
 * The word on the button that starts a run. The gateway sends it per capability: the workflow's own
 * (`action_label` in its manifest), else read from what the capability takes and makes. "Make" stands in for a row
 * that carries none.
 */
export function actionLabel(row: HomeCapability | null): string {
  return row?.action_label?.trim() || "Make";
}

/** Whether a person can supply this input modality by recording on the spot, as the capability declares. */
export function isRecordable(row: HomeCapability | null, modality: string): boolean {
  return (row?.recordable_input_modalities ?? []).includes(modality);
}

/**
 * The kinds Try has a tab for. Any other kind is run by Try through a workflow, so Home names the
 * workflow it chose as well as the model (mirrors the tab vocabulary Try reads from the URL).
 */
const KINDS_WITH_TRY_TABS = new Set(["chat", "image", "speech", "transcribe", "decide", "embed", "mesh"]);
export function tryHasOwnTab(kind: string): boolean {
  return KINDS_WITH_TRY_TABS.has(kind);
}

/** One kind of file a capability takes, as its declared input modality says. */
export interface MediaInput {
  /** The modality as the data names it ("image", "audio", or any other word a new engine declares). */
  modality: string;
  /** What the person is asked for, in a word: the gateway's input word for the modality, else the modality itself. */
  word: string;
  /** The file input's accept attribute: the IANA top-level type when the modality is one, else any file. */
  accept: string | undefined;
  /** A request cannot run without it. */
  required: boolean;
}

/** The IANA top-level media types. A modality named by one takes an `accept` filter of that type. */
const IANA_TOP_LEVEL_TYPES: ReadonlySet<string> = new Set([
  "application",
  "audio",
  "font",
  "image",
  "model",
  "text",
  "video",
]);

/** What the composer asks for: text (or not) and each file kind, all from the capability row's declared inputs. */
export interface ComposerInputs {
  text: "required" | "optional" | "none";
  media: MediaInput[];
}

/**
 * What a capability row asks the person for. `input_modalities` lists what it takes and
 * `required_input_modalities` what a request must carry. Without data (an older gateway, a row that declares
 * nothing) it asks for text, as every capability did. A file kind that is optional while text is taken is left to
 * Try, so Home asks only for what the run needs.
 */
export function composerInputs(row: HomeCapability | null): ComposerInputs {
  const takes = (row?.input_modalities ?? []).map((modality) => modality.toLowerCase());
  if (!takes.length) return { text: "required", media: [] };
  const required = new Set(
    (row?.required_input_modalities ?? (takes.includes("text") ? ["text"] : takes)).map((modality) => modality.toLowerCase()),
  );
  const acceptsText = takes.includes("text");
  const media = takes
    .filter((modality) => modality !== "text")
    .map((modality): MediaInput => ({
      modality,
      word: row?.input_words?.[modality] ?? modality,
      accept: IANA_TOP_LEVEL_TYPES.has(modality) ? `${modality}/*` : undefined,
      required: required.has(modality) || !acceptsText,
    }))
    .filter((input) => input.required);
  return { text: !acceptsText ? "none" : required.has("text") ? "required" : "optional", media };
}

/** The input's word with its article, for a sentence: "a picture", "a recording". */
export function mediaPhrase(input: MediaInput): string {
  return `${/^[aeiou]/i.test(input.word) ? "an" : "a"} ${input.word}`;
}

/** Whether a file can be given to a media input: its type fits the accept filter, or the input takes any file. */
export function fitsMediaInput(file: File, input: MediaInput): boolean {
  return !input.accept || file.type.startsWith(input.accept.replace(/\*$/, ""));
}

/** The capability row that can make a picture from words alone, so a typed description can become the picture a
 * capability needs: it takes text, needs no file, and produces an image. Null when none is offered. */
export function wordsToMediaRow(rows: HomeCapability[], modality: string): HomeCapability | null {
  return (
    rows.find((row) => {
      const inputs = composerInputs(row);
      return inputs.text === "required" && inputs.media.length === 0 && (row.output_modalities ?? []).includes(modality);
    }) ?? null
  );
}

export function parseEpochSeconds(value: number | string | null | undefined): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value / 1000 : value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed / 1000;
  }
  return null;
}

function inFlightKind(receipt: HomeReceipt): MakingKind | null {
  const intent = String(receipt.intent ?? "").toLowerCase();
  const path = String(receipt.path ?? "").toLowerCase();
  if (intent.startsWith("image") || path.includes("/images/")) return "image";
  if (intent.startsWith("video") || path.includes("/videos")) return "video";
  if (intent.startsWith("audio") || intent.startsWith("speech") || path.includes("/audio/")) return "audio";
  return null;
}

export type RecentItem =
  | {
      kind: "making";
      key: string;
      making: MakingKind;
      startedAt: number | null;
      typicalSeconds: number | null;
      /** The kind's own capability key, to read its state when it is not Ready. */
      capability: string | null;
    }
  | { kind: "media"; key: string; asset: HomeCreation; at: number | null }
  | { kind: "chat"; key: string; chat: HomeChat; at: number };

/**
 * Recent, newest first, with work still being made ahead of everything. Every source is the
 * person's own: the receipts and media are their requests and files, and chats come only from
 * request history that kept them.
 */
export function recentItems(input: {
  receipts: HomeReceipt[];
  media: HomeCreation[];
  chats: HomeChat[];
  rows: HomeCapability[];
}): RecentItem[] {
  const typical = (kind: string | null) =>
    input.rows.find((row) => kindOf(row) === kind)?.typical_seconds ?? null;
  const making: Extract<RecentItem, { kind: "making" }>[] = [];
  for (const receipt of input.receipts) {
    if (!isInFlight(receipt.status)) continue;
    const kind = inFlightKind(receipt);
    if (!kind) continue;
    const capability = requestCapability(receipt.intent, receipt.path);
    const startedAt = receipt.accepted_at_ms
      ? receipt.accepted_at_ms / 1000
      : (receipt._stored_at ?? null);
    making.push({
      kind: "making",
      key: `making:${receipt.request_id}`,
      making: kind,
      startedAt,
      typicalSeconds: typical(capability),
      capability,
    });
  }
  making.sort((a, b) => (b.startedAt ?? 0) - (a.startedAt ?? 0));
  const done: Exclude<RecentItem, { kind: "making" }>[] = [
    ...input.media
      .filter((asset) => asset.available !== false)
      .map((asset) => ({
        kind: "media" as const,
        key: `media:${asset.asset_id}`,
        asset,
        at: parseEpochSeconds(asset.created_at),
      })),
    ...input.chats.map((chat) => ({
      kind: "chat" as const,
      key: `chat:${chat.record_id}`,
      chat,
      at: chat.created_at,
    })),
  ];
  done.sort((a, b) => (b.at ?? 0) - (a.at ?? 0));
  return [...making, ...done];
}

/**
 * The warning a running make carries when its capability is not working: it may not finish. Null when the
 * capability is ready or busy, or when no view knows it.
 */
export function makingRisk(item: Extract<RecentItem, { kind: "making" }>, views: CapabilityView[]): string | null {
  const view = views.find((candidate) => candidate.key === item.capability);
  if (!view || view.state === "ready" || view.state === "busy") return null;
  return `${view.label} ${view.word.toLowerCase()}: it may not finish`;
}

/**
 * The words a running make was started with, when this tab started it from Home: the newest Make of the
 * same capability pressed up to ten minutes before the run was accepted. Null when no Make matches.
 */
export function makingPrompt(
  item: Extract<RecentItem, { kind: "making" }>,
  made: Array<{ capability: string; prompt: string; at: number }>,
): string | null {
  if (item.startedAt == null || !item.capability) return null;
  const startedAt = item.startedAt;
  const match = made.find(
    (entry) => entry.capability === item.capability && entry.at <= startedAt + 5 && startedAt - entry.at <= 600,
  );
  return match?.prompt.trim() || null;
}

/** How far through a making run the time is, 0 to 1, or null when no typical time is known. */
export function makingProgress(item: Extract<RecentItem, { kind: "making" }>, now: number): number | null {
  if (!item.typicalSeconds || item.startedAt == null) return null;
  return Math.min(0.95, Math.max(0, (now - item.startedAt) / item.typicalSeconds));
}

/** "about 6 s left", or "Taking longer than usual" once the typical time has passed. */
export function makingTimeLeft(item: Extract<RecentItem, { kind: "making" }>, now: number): string | null {
  if (!item.typicalSeconds || item.startedAt == null) return null;
  const left = item.typicalSeconds - (now - item.startedAt);
  if (left <= 0) return "Taking longer than usual";
  return `${formatWait(left)} left`;
}

export interface GatewayNeed {
  key: string;
  /** How bad it is: a fault the owner must fix, or a gap in what is set up. */
  tone: "down" | "degraded" | "off";
  fact: string;
  action: { label: string; href: string };
}

/** "Video is down", "Images are off": a capability's label with the verb its name takes, then the phrase. */
export function capabilityIs(label: string, phrase: string): string {
  return `${label} ${/s$/i.test(label) ? "are" : "is"} ${phrase}`;
}

/** "Chat", "Chat and Images", "Chat, Images and Voice". */
export function joinNames(names: string[]): string {
  if (names.length < 2) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** The capability states the owner must act on. */
const FAULTS = new Set(["down", "degraded"]);
/** States that keep a capability out of the chooser: nobody can use it, so nobody is offered it. */
const NOT_OFFERED = new Set(["off", "blocked"]);

/**
 * The chooser offers only what the person can use now or soon. Off (not set up, or turned off) and
 * Blocked (not in their access plan) never appear; the owner finds those in the gateway panel.
 * Down and Degraded stay, so a temporary fault is explained where it is chosen.
 */
export function offeredRows(rows: HomeCapability[], views: CapabilityView[]): HomeCapability[] {
  const stateOf = new Map(views.map((view) => [view.key, view.state]));
  return rows.filter((row) => !NOT_OFFERED.has(stateOf.get(kindOf(row)) ?? "ready"));
}

/**
 * What the owner must act on, one item each with one place to fix it. Only operational facts: an
 * engine or provider that is failing, no engine yet, a capability that is down or degraded, and
 * one item for every capability that is off. Never content. (A missing access plan is the notice
 * where the composer would be, not an item here.)
 */
export function gatewayNeeds(input: {
  engineFault: string | null;
  providerFault: string | null;
  noEngine: boolean;
  /** The owner's capability views; the ones in a state the owner fixes become items. */
  capabilities: CapabilityView[];
}): GatewayNeed[] {
  const needs: GatewayNeed[] = [];
  const engines = { label: "Open Engines", href: "/engines/" };
  // A capability row that names its failing engine already tells this; one fault is one row.
  const engineNamed = input.capabilities.some((view) => FAULTS.has(view.state) && view.failing.length > 0);
  if (input.engineFault && !engineNamed) needs.push({ key: "engine", tone: "down", fact: input.engineFault, action: engines });
  if (input.providerFault)
    needs.push({
      key: "provider",
      tone: "down",
      fact: input.providerFault,
      action: { label: "Open Providers", href: "/providers/" },
    });
  if (input.noEngine)
    needs.push({ key: "engine-added", tone: "off", fact: "Add an engine to turn something on", action: engines });
  for (const view of input.capabilities) {
    if (!FAULTS.has(view.state)) continue;
    needs.push({
      key: `capability:${view.key}`,
      tone: view.state === "down" ? "down" : "degraded",
      fact: capabilityIs(view.label, view.word.toLowerCase()),
      action: engines,
    });
  }
  const off = input.capabilities.filter((view) => view.state === "off").map((view) => view.label);
  if (off.length && !input.noEngine)
    needs.push({ key: "capability:off", tone: "off", fact: capabilityIs(joinNames(off), "off"), action: engines });
  return needs;
}

/**
 * The owner's one line for what works, counted from the data: "4 of 5 working". What is off or blocked is not on,
 * so it is not counted (the panel's own row names it); a fault is on, so it counts against the working ones.
 * Null when nothing works.
 */
export function workingLine(views: CapabilityView[]): string | null {
  const on = views.filter((view) => !NOT_OFFERED.has(view.state));
  const working = on.filter((view) => view.state === "ready" || view.state === "busy").length;
  if (!working) return null;
  return `${working} of ${on.length} working`;
}

/** Greeting from the time of day, in two parts so the name can be quieter; a first-time member is welcomed instead. */
export function greetingParts(now: Date, firstName: string | null, firstVisit: boolean): { lead: string; name: string | null } {
  const name = firstName?.trim() || null;
  if (firstVisit) return { lead: "Welcome", name };
  const hour = now.getHours();
  return { lead: hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening", name };
}

export function greetingFor(now: Date, firstName: string | null, firstVisit: boolean): string {
  const { lead, name } = greetingParts(now, firstName, firstVisit);
  return name ? `${lead}, ${name}` : lead;
}

/** The first word of a display name, for the greeting. An address is never a name, so it gives none. */
export function firstNameOf(name: string | null | undefined): string | null {
  const trimmed = name?.trim() ?? "";
  if (!trimmed || trimmed.includes("@")) return null;
  return trimmed.split(/\s+/)[0];
}

import {
  isInFlight,
  isSuccess,
  requestCapability,
  type CapabilityView,
} from "@/lib/capability-state";
import { capabilityMeta, knownCapabilityLabel } from "@/components/product/readiness";
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

/** The name a person reads for a kind of work; a kind this UI does not name uses the gateway's title. */
export function capabilityLabel(row: HomeCapability): string {
  return knownCapabilityLabel(kindOf(row)) ?? row.capability_title ?? row.capability;
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

/** The line in "What you can make here": the wait first, and the cost in lower case. */
export function listFacts(row: HomeCapability): string {
  const wait = formatWait(row.typical_seconds);
  const waitText = wait && kindOf(row) === "chat" ? `starts in ${wait}` : wait;
  const cost = row.cost_hint?.kind === "free" ? "no charge" : row.cost_hint?.kind === "paid" ? "paid model" : null;
  const amount = row.cost_hint?.kind === "paid" && row.cost_hint.price_usd != null
    ? `about USD ${formatUsd(row.cost_hint.price_usd)}`
    : null;
  const line = [waitText, cost, amount].filter(Boolean).join(" · ");
  return line.charAt(0).toUpperCase() + line.slice(1);
}

/** A composer's placeholder: what the kind of work asks for. A plain default covers any other kind. */
const PLACEHOLDERS: Record<string, string> = {
  chat: "Ask anything",
  image: "Describe the picture you want",
  video: "Describe the clip you want",
  speech: "Type the words to read aloud",
  music: "Describe the music you want",
  sfx: "Describe the sound you want",
};
export function promptPlaceholder(kind: string): string {
  return PLACEHOLDERS[kind] ?? "Describe what you want";
}

/** Chat sends a message; every other kind makes something. */
export function makeVerb(kind: string): "Send" | "Make" {
  return kind === "chat" ? "Send" : "Make";
}

/**
 * The kinds Try has a tab for. Any other kind is run by Try through a workflow, so Home names the
 * workflow it chose as well as the model (mirrors the tab vocabulary Try reads from the URL).
 */
const KINDS_WITH_TRY_TABS = new Set(["chat", "image", "speech", "transcribe", "decide", "embed"]);
export function tryHasOwnTab(kind: string): boolean {
  return KINDS_WITH_TRY_TABS.has(kind);
}

/** Transcription works on a recording, which Home cannot take: it opens Try instead. */
export function needsRecording(kind: string): boolean {
  return kind === "transcribe";
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
  fact: string;
  action: { label: string; href: string };
}

/**
 * What the owner must act on, one item each with one action. Only operational facts: an engine
 * or provider that is failing, or no engine yet. Never content. (A missing access plan is the
 * notice where the composer would be, not an item here.)
 */
export function gatewayNeeds(input: {
  engineFault: string | null;
  providerFault: string | null;
  noEngine: boolean;
}): GatewayNeed[] {
  const needs: GatewayNeed[] = [];
  if (input.engineFault)
    needs.push({ key: "engine", fact: input.engineFault, action: { label: "Open Engines", href: "/engines/" } });
  if (input.providerFault)
    needs.push({ key: "provider", fact: input.providerFault, action: { label: "Open Providers", href: "/providers/" } });
  if (input.noEngine)
    needs.push({
      key: "engine-added",
      fact: "Add an engine to turn something on.",
      action: { label: "Open Engines", href: "/engines/" },
    });
  return needs;
}

function joinNames(names: string[]): string {
  if (names.length < 2) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** One sentence naming what works, for the owner's panel. Null when nothing does. */
export function workingSentence(views: CapabilityView[]): string | null {
  const names = views
    .filter((view) => view.state === "ready")
    .map((view) => capabilityMeta(view.key).label);
  if (!names.length) return null;
  const verb = names.length > 1 ? "are working" : "is working";
  return `${joinNames(names)} ${verb}.`;
}

/** Greeting from the time of day; a first-time member is welcomed instead. */
export function greetingFor(now: Date, firstName: string | null, firstVisit: boolean): string {
  const name = firstName?.trim() || null;
  if (firstVisit) return name ? `Welcome, ${name}` : "Welcome";
  const hour = now.getHours();
  const part = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  return name ? `${part}, ${name}` : part;
}

export function firstNameOf(name: string | null | undefined): string | null {
  const first = name?.trim().split(/\s+/)[0];
  return first ? first : null;
}

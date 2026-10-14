/**
 * Small pure helpers the Add engine dialog and the engine page share: the
 * address arithmetic behind "Azizam will call …", times, lists, and the
 * sentences that say what happened after an add.
 */
import type { EngineDefinition, EngineField, EngineValues } from "@/lib/engines-api";

export function trimAddress(address: string): string {
  return String(address || "").trim().replace(/\/+$/, "");
}

export function joinUrl(base: string, path: string): string {
  const a = trimAddress(base);
  if (!path) return a;
  return a + (path.startsWith("/") ? path : `/${path}`);
}

export function originOf(address: string): string {
  const match = trimAddress(address).match(/^[a-z][a-z0-9+.-]*:\/\/[^/]+/i);
  return match ? match[0] : trimAddress(address);
}

export function looksLikeAddress(address: string): boolean {
  return /^https?:\/\/[^\s/]+/i.test(address.trim());
}

export function fieldOf(definition: EngineDefinition | undefined, key: string): EngineField | undefined {
  return definition?.fields.find((field) => field.key === key);
}

/** A custom-path kind: the definition asks for the path, and the check calls it. */
export function hasPath(definition: EngineDefinition): boolean {
  return Boolean(fieldOf(definition, "path"));
}

export type CallLine =
  | { kind: "empty" }
  | { kind: "needs-path"; base: string }
  | { kind: "call"; request: string; twice?: string };

/** The exact first request the check makes, from the definition's `check_request`. */
export function callLine(definition: EngineDefinition, values: Pick<EngineValues, "address" | "path" | "path_prefix">): CallLine {
  const address = trimAddress(values.address);
  if (!address) return { kind: "empty" };
  const { method, path, from } = definition.check_request;
  if (path.includes("{path}")) {
    const typed = String(values.path ?? "").trim();
    if (!typed) return { kind: "needs-path", base: address };
    return { kind: "call", request: `${method} ${joinUrl(address, path.replace("{path}", typed))}` };
  }
  const prefix = String(values.path_prefix ?? "").trim().replace(/\/+$/, "");
  const base = from === "origin" ? originOf(address) : joinUrl(address, prefix);
  const twice = from !== "origin" && prefix && address.endsWith(prefix) ? prefix : undefined;
  return { kind: "call", request: `${method} ${joinUrl(base, path)}`, twice };
}

/** "09:23", the clock time of a unix time in seconds. */
export function clockTime(at: number): string {
  return new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function joinList(items: string[], empty = "no abilities yet"): string {
  if (items.length < 2) return items[0] ?? empty;
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** When a kept-off engine is checked. Azizam starts a managed engine itself; any other is checked when turned on. */
export function checkedWhen(managed: boolean): string {
  return managed ? "It checks the first time the engine starts." : "It checks it when you turn it on or press Check now.";
}

export function addedMessage(name: string, labels: string[]): string {
  return `Added “${name}”. It is on, with ${joinList(labels)}.`;
}

export function keptOffMessage(name: string, needKey: boolean): string {
  return needKey
    ? `Added “${name}”, turned off. The server needs a key, and only the owner can add one. Ask the owner, then turn the engine on.`
    : `Added “${name}”, turned off. Azizam has not checked the server yet. ${checkedWhen(false)}`;
}

/** The size cap of a workflow file in whole megabytes, for the drop area. */
export function megabytes(bytes: number | undefined): number {
  return Math.round((bytes ?? 20 * 1048576) / 1048576);
}

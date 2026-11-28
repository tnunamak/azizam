/**
 * The owner's values for an engine Azizam starts, and the dry run that turns them into the exact command.
 * Everything here is keyed on what the definition declares (`process_slots`), never on a kind's name.
 */
import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { enginesApi, type EngineDefinition, type ProcessPreview, type ProcessProblem } from "@/lib/engines-api";

export type SlotValues = Record<string, string>;

/** Where the search for a free port starts. The gateway still checks the port the owner ends up with. */
const FIRST_PORT = 8100;
const PORT_TRIES = 30;
const PREVIEW_DELAY_MS = 300;

/** The slots as the gateway wants them: a port as a number, a path as typed. Empty values are left out. */
export function slotsBody(definition: EngineDefinition, values: SlotValues): Record<string, string | number> {
  const body: Record<string, string | number> = {};
  for (const [name, slot] of Object.entries(definition.process_slots ?? {})) {
    const text = (values[name] ?? "").trim();
    if (!text) continue;
    body[name] = slot.type === "port" && /^\d+$/.test(text) ? Number(text) : text;
  }
  return body;
}

export function portSlotName(definition: EngineDefinition): string | undefined {
  return Object.entries(definition.process_slots ?? {}).find(([, slot]) => slot.type === "port")?.[0];
}

/** "install_folder" becomes "Install folder". */
export function slotLabel(name: string): string {
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The gateway starts a reason with the slot's name ("folder: give a value."). The field already says which. */
export function problemText(problem: ProcessProblem): string {
  const prefix = `${problem.slot}: `;
  const text = problem.slot && problem.why.startsWith(prefix) ? problem.why.slice(prefix.length) : problem.why;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

export interface PreviewState {
  preview: ProcessPreview | undefined;
  /** The owner has typed since the answer on screen was asked for. */
  pending: boolean;
  failed: boolean;
}

/** The dry run for the values on screen, asked for after the owner stops typing. */
export function useProcessPreview(definition: EngineDefinition | null, values: SlotValues, enabled: boolean): PreviewState {
  const active = enabled && Boolean(definition?.process_slots);
  const body = definition ? JSON.stringify(slotsBody(definition, values)) : "";
  const settled = useDebounced(body, PREVIEW_DELAY_MS);
  const query = useQuery({
    queryKey: ["engineProcessPreview", definition?.definition, settled],
    enabled: active,
    retry: false,
    gcTime: 0,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) =>
      enginesApi.processPreview(
        { definition: definition?.definition ?? "", slots: JSON.parse(settled) as Record<string, string | number> },
        signal,
      ),
  });
  return {
    preview: active ? query.data : undefined,
    pending: active && (settled !== body || query.isFetching),
    failed: active && query.isError,
  };
}

/**
 * The first port from FIRST_PORT that the gateway says is free and not taken by another engine. The dry run says
 * so as a problem on the port slot, so no route beyond the preview is needed. Null when none is found.
 */
export async function findFreePort(definition: EngineDefinition, signal?: AbortSignal): Promise<number | null> {
  const portSlot = portSlotName(definition);
  if (!portSlot) return null;
  for (let port = FIRST_PORT; port < FIRST_PORT + PORT_TRIES; port += 1) {
    if (signal?.aborted) return null;
    try {
      const answer = await enginesApi.processPreview({ definition: definition.definition, slots: { [portSlot]: port } }, signal);
      if (!answer.problems.some((problem) => problem.slot === portSlot)) return port;
    } catch {
      return null;
    }
  }
  return null;
}

/** A name to suggest: the last part of the first folder the owner gave, else the kind's title. */
export function suggestedEngineName(definition: EngineDefinition, values: SlotValues): string {
  const pathSlot = Object.entries(definition.process_slots ?? {}).find(([, slot]) => slot.type === "path")?.[0];
  const last = (pathSlot ? (values[pathSlot] ?? "") : "").trim().replace(/\/+$/, "").split("/").pop();
  return last || definition.title;
}

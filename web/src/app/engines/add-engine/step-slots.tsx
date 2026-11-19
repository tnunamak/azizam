"use client";

import Link from "next/link";
import type { RefObject } from "react";
import { Check } from "lucide-react";
import type { EngineDefinition, ProcessProblem } from "@/lib/engines-api";
import { Input } from "@/components/ui/input";
import { FormField, LinkButton, Note } from "../parts/form";
import { problemText, slotLabel, type PreviewState, type SlotValues } from "./process-slots";

/** The help under one slot, from what the definition says about it. */
function slotHelp(slot: NonNullable<EngineDefinition["process_slots"]>[string]): string {
  if (slot.type === "port") return "The port Azizam gives the engine. Azizam filled in one that is free. You can change it.";
  const holds = slot.locate.length ? ` It must hold ${slot.locate.join(", ")}.` : "";
  return `${slot.kind === "file" ? "The full path of a file" : "The full path of a folder"} inside a folder from Engine folders.${holds}`;
}

/** Step 2 of an engine Azizam starts: the values it needs, and the exact command they make. */
export function StepSlots({
  definition,
  values,
  onChange,
  state,
  folders,
  serverErrors,
  onChangeKind,
  headingRef,
}: {
  definition: EngineDefinition;
  values: SlotValues;
  onChange: (name: string, value: string) => void;
  state: PreviewState;
  /** The folders engines may run from. Undefined while they load. */
  folders: string[] | undefined;
  /** A refusal the gateway sent when the engine was saved, by slot. */
  serverErrors: Record<string, string>;
  onChangeKind?: () => void;
  headingRef?: RefObject<HTMLHeadingElement | null>;
}) {
  const slots = Object.entries(definition.process_slots ?? {});
  const problems = state.preview?.problems ?? [];
  const noFolders = folders?.length === 0;
  const bySlot = (name: string): ProcessProblem[] =>
    problems.filter(
      (problem) => problem.slot === name && problem.code !== "missing_slot" && !(noFolders && problem.code === "no_folders"),
    );
  const general = problems.filter((problem) => problem.slot === "" || !definition.process_slots?.[problem.slot]);
  const ready = state.preview !== undefined && problems.length === 0 && !state.pending;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 ref={headingRef} tabIndex={-1} className="text-heading outline-none">
          Where is it installed?
        </h3>
        <p className="text-meta text-muted-foreground mt-0.5">
          Azizam starts this engine itself. Tell it where the engine is. Azizam shows the exact command before it saves anything.
        </p>
      </div>
      <div className="bg-muted rounded-control text-meta flex flex-wrap items-center justify-between gap-2 px-3 py-2">
        <span className="flex items-center gap-2">
          <Check aria-hidden="true" className="text-success size-4" />
          <b className="font-medium">{definition.title}</b>
        </span>
        {onChangeKind && <LinkButton onClick={onChangeKind}>Change kind</LinkButton>}
      </div>
      {noFolders && (
        <Note tone="warn" role="status">
          Azizam may not start engines from any folder yet.{" "}
          <Link href="/settings#engine-folders" target="_blank" className="text-link underline-offset-4 hover:underline">
            Add one in Settings, under Engine folders
          </Link>
          , then come back here.
        </Note>
      )}
      {slots.map(([name, slot]) => {
        const refusal = serverErrors[name] ?? bySlot(name).map(problemText).join(" ");
        return (
          <FormField key={name} label={slotLabel(name)} required help={slotHelp(slot)} error={refusal || undefined}>
            {(control) => (
              <Input
                {...control}
                className={slot.type === "path" ? "font-mono" : undefined}
                inputMode={slot.type === "port" ? "numeric" : undefined}
                value={values[name] ?? ""}
                autoComplete="off"
                spellCheck={false}
                placeholder={slot.type === "path" ? "/srv/engines/my-engine" : undefined}
                onChange={(event) => onChange(name, event.target.value)}
              />
            )}
          </FormField>
        );
      })}
      {general.length > 0 && (
        <Note tone="warn" role="alert">
          {general.map((problem) => (
            <span key={`${problem.code}-${problem.slot}`} className="block">
              {problemText(problem)}
            </span>
          ))}
        </Note>
      )}
      {state.failed && (
        <Note tone="warn" role="alert">
          Azizam could not work out the command. Try again in a moment.
        </Note>
      )}
      <div className="bg-card rounded-panel border p-4" aria-live="polite">
        <h4 className="text-label">Command Azizam will run</h4>
        {ready && state.preview ? (
          <dl data-testid="process-preview" className="text-meta mt-2 grid gap-x-4 gap-y-1.5 sm:grid-cols-[auto_1fr]">
            <dt className="text-muted-foreground">Command</dt>
            <dd className="min-w-0 font-mono [overflow-wrap:anywhere]">{state.preview.argv.join(" ")}</dd>
            <dt className="text-muted-foreground">Working folder</dt>
            <dd className="min-w-0 font-mono [overflow-wrap:anywhere]">{state.preview.cwd ?? "None"}</dd>
            <dt className="text-muted-foreground">Port</dt>
            <dd className="font-mono">{state.preview.port}</dd>
          </dl>
        ) : (
          <p className="text-meta text-muted-foreground mt-1">
            {state.pending
              ? "Working it out…"
              : problems.length > 0
                ? "Fix what is marked above to see the command."
                : "Fill in the values above to see the command."}
          </p>
        )}
      </div>
    </div>
  );
}

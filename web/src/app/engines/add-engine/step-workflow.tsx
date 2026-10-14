"use client";

/**
 * Node-graph kind, steps 3 and 4: pick the workflow file, then say which field
 * in it gets each input an app sends. N1: the name apps use for the workflow.
 */
import { useId, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { CircleCheck } from "lucide-react";
import type { EngineDefinition, EngineField, Finding, WorkflowInputRow, WorkflowRead } from "@/lib/engines-api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FindingCard } from "../parts/finding-card";
import { FormField, NativeSelect, Tag } from "../parts/form";
import { megabytes } from "./draft";

export type WorkflowOk = Extract<WorkflowRead, { ok: true }>;

export function StepWorkflowFile({
  definition,
  read,
  finding,
  reading,
  makes,
  onMakes,
  onFile,
  onReplace,
}: {
  definition: EngineDefinition;
  read: WorkflowOk | null;
  finding: Finding | null;
  reading: boolean;
  makes: "images" | "video";
  onMakes: (makes: "images" | "video") => void;
  onFile: (file: File) => void;
  onReplace: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();
  const [over, setOver] = useState(false);
  const mb = megabytes(definition.workflow_max_bytes);

  function picked(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) onFile(file);
    event.target.value = "";
  }
  function dropped(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setOver(false);
    const file = event.dataTransfer.files?.[0];
    if (file) onFile(file);
  }
  const chooser = (
    <>
      <label htmlFor={inputId} className="sr-only">
        Choose a file
      </label>
      <input ref={inputRef} id={inputId} type="file" accept=".json,application/json" className="sr-only" tabIndex={-1} onChange={picked} />
    </>
  );

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="text-heading">Pick the workflow</h3>
        <p className="text-meta text-muted-foreground mt-0.5">
          A workflow is a file that lists the steps the server runs to make a picture or a video. Azizam fills in the prompt and the
          other inputs each time an app asks.
        </p>
      </div>
      {chooser}
      {read ? (
        <>
          <div className="bg-card rounded-panel flex flex-wrap items-center justify-between gap-3 border p-4">
            <div className="flex min-w-0 items-start gap-2">
              <CircleCheck aria-hidden="true" className="text-success mt-0.5 size-4 shrink-0" />
              <span className="min-w-0">
                <span className="text-meta block font-mono [overflow-wrap:anywhere]">{read.file}</span>
                <span className="text-meta text-muted-foreground mt-0.5 block">Read in API format. {read.node_count} nodes found.</span>
              </span>
            </div>
            <Button type="button" variant="outline" size="sm" onClick={onReplace}>
              Choose another file
            </Button>
          </div>
          <FormField
            label="This workflow makes"
            help={`Azizam found a node that saves ${read.makes === "video" ? "video" : "images"}, so it picked that. Change it if that is wrong.`}
          >
            {(control) => (
              <NativeSelect {...control} value={makes} onChange={(event) => onMakes(event.target.value as "images" | "video")}>
                <option value="images">Images</option>
                <option value="video">Video</option>
              </NativeSelect>
            )}
          </FormField>
        </>
      ) : (
        <>
          <div
            onDragOver={(event) => {
              event.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={dropped}
            className={cn(
              "rounded-panel flex flex-col items-center gap-2 border border-dashed p-6 text-center",
              over && "border-primary bg-muted/40",
            )}
          >
            <p className="text-body font-medium">Drop a workflow file here</p>
            <p className="text-meta text-muted-foreground">A .json file in API format, up to {mb} MB.</p>
            <Button type="button" variant="outline" size="sm" disabled={reading} onClick={() => inputRef.current?.click()}>
              {reading ? "Reading…" : "Choose a file"}
            </Button>
          </div>
          {finding && (
            <>
              <FindingCard
                finding={finding}
                extraActions={
                  <Button type="button" variant="outline" size="sm" onClick={() => inputRef.current?.click()}>
                    Choose another file
                  </Button>
                }
              />
              <p className="text-meta text-muted-foreground">
                “Add but keep off” adds the engine turned off, without a workflow. You choose one later, on the engine page.
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}

function missingSentence(row: WorkflowInputRow) {
  const name = row.label.toLowerCase();
  return `Azizam needs to know where the ${name} goes. Choose the field that holds the ${name} text.`;
}

export function inputsMissing(read: WorkflowOk, inputs: Record<string, string>) {
  return read.inputs.some((row) => row.required && !inputs[row.key]);
}

export function StepInputs({
  read,
  inputs,
  onInputs,
  publicField,
  publicName,
  publicSuggested,
  onPublicName,
}: {
  read: WorkflowOk;
  inputs: Record<string, string>;
  onInputs: (next: Record<string, string>) => void;
  publicField?: EngineField;
  publicName: string;
  publicSuggested: boolean;
  onPublicName: (name: string) => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="text-heading">Match the inputs</h3>
        <p className="text-meta text-muted-foreground mt-0.5">
          Apps send a prompt, a size and a few other inputs. Say which field in{" "}
          <span className="font-mono [overflow-wrap:anywhere]">{read.file}</span> gets each one. Azizam matched what it could.
        </p>
      </div>
      {publicField && (
        <FormField
          label={publicField.label}
          required
          help={publicField.help}
          tag={publicSuggested ? <Tag tone="suggest">Suggested</Tag> : undefined}
          error={publicName.trim() ? undefined : "Give the workflow the name apps use."}
        >
          {(control) => (
            <Input
              {...control}
              value={publicName}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onPublicName(event.target.value)}
            />
          )}
        </FormField>
      )}
      <div role="group" aria-label="Workflow inputs" className="bg-card rounded-panel divide-y border">
        <div aria-hidden="true" className="text-label text-muted-foreground hidden grid-cols-[1.2fr_1.4fr_auto] gap-3 px-4 py-2 sm:grid">
          <span>Input from an app</span>
          <span>Field in the workflow</span>
          <span>Status</span>
        </div>
        {read.inputs.map((row) => (
          <InputRow key={row.key} row={row} value={inputs[row.key] ?? ""} onChange={(v) => onInputs({ ...inputs, [row.key]: v })} />
        ))}
      </div>
      <details>
        <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
          Nodes found in this file ({read.node_count})
        </summary>
        <dl className="text-meta mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {read.nodes.map((node) => (
            <div key={node.id} className="contents">
              <dt className="text-muted-foreground">Node {node.id}</dt>
              <dd className="[overflow-wrap:anywhere]">
                {node.title} · {node.class_type}
                {node.fields.length ? ` · ${node.fields.join(", ")}` : ""}
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}

function InputRow({ row, value, onChange }: { row: WorkflowInputRow; value: string; onChange: (value: string) => void }) {
  const id = useId();
  const badge: [string, string] = !value
    ? row.required
      ? ["bg-danger-tint text-danger", "Choose one"]
      : ["bg-muted text-subtle-foreground", "Not used"]
    : value === row.auto
      ? ["bg-success-tint text-success", "Matched"]
      : ["bg-info-tint text-info", "You chose"];
  const note = !row.options.length || value === row.auto ? row.note : undefined;
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[1.2fr_1.4fr_auto] sm:gap-3">
      <div className="min-w-0">
        <p className="text-meta flex items-center gap-2 font-medium">
          {row.label}
          {row.required && <Tag>Required</Tag>}
        </p>
        <p className="text-meta text-muted-foreground">{row.help}</p>
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor={id} className="sr-only">
          Workflow field that fills {row.label}
        </label>
        <NativeSelect id={id} value={value} disabled={!row.options.length} onChange={(event) => onChange(event.target.value)}>
          <option value="">{row.required ? "Choose a field…" : "Not used"}</option>
          {row.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </NativeSelect>
        {note && <p className="text-meta text-muted-foreground">{note}</p>}
        {row.required && !value && (
          <p role="alert" className="text-meta text-danger">
            {missingSentence(row)}
          </p>
        )}
      </div>
      <div>
        <span className={cn("rounded-badge text-badge inline-flex h-5 items-center px-1.5", badge[0])}>{badge[1]}</span>
      </div>
    </div>
  );
}

"use client";

/**
 * The models a detected server offers: a tick per model, its public name and
 * its context window, prefilled from the server and tagged until edited, and
 * "Add a model by name" for one the server does not list.
 */
import { useId, useRef, useState } from "react";
import { Search } from "lucide-react";
import type { DetectedModel } from "@/lib/engines-api";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { FormField, Tag } from "./form";

export interface ModelDraft {
  id: string;
  public_name: string;
  context_window: string;
  use: string;
  on: boolean;
  /** Still the server's suggestion: shows the Suggested tag. */
  suggestedName: boolean;
  /** Still what the server reported: shows the Reported by server tag. */
  reportedWindow: boolean;
  byName: boolean;
}

export function draftsFromCheck(models: DetectedModel[], on = true): ModelDraft[] {
  return models.map((m) => ({
    id: m.id,
    public_name: m.public_name,
    context_window: m.context_window == null ? "" : String(m.context_window),
    use: m.use,
    on,
    suggestedName: true,
    reportedWindow: m.context_window != null,
    byName: false,
  }));
}

/** The name apps type for a model, and the name that is saved: the public name, or the server's name when it is empty. */
export function effectiveName(model: Pick<ModelDraft, "id" | "public_name">): string {
  return model.public_name.trim() || model.id;
}

/** Public names that two offered models share, as they will be saved. */
export function duplicateNames(models: ModelDraft[]): Set<string> {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const m of models) {
    if (!m.on) continue;
    const name = effectiveName(m);
    if (seen.has(name)) dup.add(name);
    seen.add(name);
  }
  return dup;
}

export function modelsBody(models: ModelDraft[]) {
  return models.map((m) => ({
    id: m.id,
    public_name: effectiveName(m),
    context_window: m.context_window.trim() === "" ? null : Number(m.context_window),
    enabled: m.on,
  }));
}

export function ModelsBlock({
  models,
  onChange,
  emptyText,
  readOnly,
  error,
}: {
  models: ModelDraft[];
  onChange: (next: ModelDraft[]) => void;
  emptyText: string;
  readOnly?: boolean;
  /** A message from the server about the list as a whole, for example after a refused Save. */
  error?: string;
}) {
  const [query, setQuery] = useState("");
  const [byName, setByName] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const addRow = useRef<HTMLDivElement>(null);
  const searchId = useId();
  const errorId = useId();
  const dup = duplicateNames(models);
  const shown = models
    .map((model, index) => ({ model, index }))
    .filter(({ model }) => !query || `${model.id} ${model.public_name}`.toLowerCase().includes(query.toLowerCase()));
  const offered = models.filter((m) => m.on).length;

  function update(index: number, patch: Partial<ModelDraft>) {
    onChange(models.map((m, i) => (i === index ? { ...m, ...patch } : m)));
  }

  function add() {
    const name = byName.trim();
    const problem = !name
      ? "Enter the model name used by the server."
      : models.some((m) => m.id === name)
        ? "This model is already listed. Find it above and tick it to offer it."
        : null;
    if (problem) {
      // Focus the field so the message is read out with it.
      setAddError(problem);
      addRow.current?.querySelector("input")?.focus();
      return;
    }
    setAddError(null);
    onChange([
      ...models,
      { id: name, public_name: name, context_window: "", use: "Not known yet", on: true, suggestedName: false, reportedWindow: false, byName: true },
    ]);
    setByName("");
  }

  return (
    <div
      className="flex flex-col gap-3"
      {...(error ? { role: "group", "aria-label": "Models", "aria-describedby": errorId, tabIndex: -1, "data-field-error": "true" } : {})}
    >
      {error && (
        <p id={errorId} className="text-meta text-danger">
          {error}
        </p>
      )}
      <p className="text-meta text-muted-foreground">
        {offered} of {models.length} models offered
      </p>
      {models.length > 0 && (
        <div className="relative">
          <Search aria-hidden="true" className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
          <label htmlFor={searchId} className="sr-only">
            Search models
          </label>
          <Input
            id={searchId}
            type="search"
            className="pl-9"
            placeholder="Search models"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      )}
      <div className="bg-card rounded-panel divide-y border">
        {shown.length === 0 ? (
          <p className="text-meta text-muted-foreground p-4">{models.length ? `No model matches “${query}”.` : emptyText}</p>
        ) : (
          shown.map(({ model, index }) => (
            <div key={model.id} className="flex flex-col gap-3 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <label className="flex min-h-8 min-w-0 cursor-pointer items-center gap-3 max-sm:min-h-11">
                  <Checkbox
                    aria-label={`Offer ${model.id}`}
                    checked={model.on}
                    disabled={readOnly}
                    onCheckedChange={(value) => update(index, { on: value === true })}
                  />
                  <span className="text-meta min-w-0 font-mono [overflow-wrap:anywhere]">{model.id}</span>
                </label>
                <span className="flex flex-wrap gap-1.5">
                  <Tag>{model.use}</Tag>
                  {model.byName && <Tag>Added by name</Tag>}
                </span>
              </div>
              <div className="grid gap-3 pl-7 sm:grid-cols-2">
                <FormField
                  label="Public name"
                  tag={model.suggestedName ? <Tag tone="suggest">Suggested</Tag> : undefined}
                  error={model.on && dup.has(effectiveName(model)) ? `Two models use the public name “${effectiveName(model)}”. Each needs its own.` : undefined}
                >
                  {(control) => (
                    <Input
                      {...control}
                      value={model.public_name}
                      disabled={readOnly}
                      autoComplete="off"
                      spellCheck={false}
                      onChange={(event) => update(index, { public_name: event.target.value, suggestedName: false })}
                    />
                  )}
                </FormField>
                <FormField
                  label="Context window"
                  help="Leave empty if you don’t know."
                  tag={model.reportedWindow ? <Tag tone="suggest">Reported by server</Tag> : undefined}
                >
                  {(control) => (
                    <div className="flex items-center gap-2">
                      <Input
                        {...control}
                        inputMode="numeric"
                        placeholder="Not reported"
                        value={model.context_window}
                        disabled={readOnly}
                        autoComplete="off"
                        onChange={(event) =>
                          update(index, { context_window: event.target.value.replace(/[^\d]/g, ""), reportedWindow: false })
                        }
                      />
                      <span className="text-meta text-muted-foreground">tokens</span>
                    </div>
                  )}
                </FormField>
              </div>
              {model.byName && !readOnly && (
                <Button
                  type="button"
                  variant="link"
                  size="sm"
                  className="ml-7 w-fit px-0 max-sm:min-h-11"
                  onClick={() => onChange(models.filter((_, i) => i !== index))}
                >
                  Remove model
                  <span className="sr-only"> {model.id}</span>
                </Button>
              )}
            </div>
          ))
        )}
      </div>
      {!readOnly && (
        <div ref={addRow} className="flex flex-wrap items-end gap-2">
          <FormField
            className="min-w-0 flex-1"
            label="Add a model by name"
            help="Use this when the server does not list a model, for example one it loads on demand."
            error={addError ?? undefined}
          >
            {(control) => (
              <Input
                {...control}
                placeholder="The name the server uses"
                value={byName}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => {
                  setByName(event.target.value);
                  setAddError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    add();
                  }
                }}
              />
            )}
          </FormField>
          <Button type="button" variant="outline" size="md" className="mb-6" onClick={add}>
            Add model
          </Button>
        </div>
      )}
    </div>
  );
}

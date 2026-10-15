"use client";

/**
 * The engine page for an engine the model router does not call at its own
 * address. Changing its address, key, models or options would save a setting
 * the gateway ignores, so none of that can change here: the settings show as
 * plain text, with the server's reason. The name and Enabled still change,
 * through the plain service edit (no definition, no values).
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CircleCheck, Loader2 } from "lucide-react";
import { api, queryKeys, type ServiceRegistry } from "@/lib/api";
import type { EngineDefinition, EngineField, EngineView } from "@/lib/engines-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RelativeTime } from "@/components/ui/relative-time";
import { Switch } from "@/components/ui/switch";
import { Callout, StateWord } from "@/components/product/status-ui";
import { AccessAnswer } from "../parts/access-answer";
import { FormField, Tag } from "../parts/form";
import { optionFields } from "./more-options";
import { RemoveSection, StoredRecordSection, errorText } from "./record-sections";

/** One setting as a sentence part: "600 seconds", "Azizam leaves it running", "None". */
function shown(field: EngineField, raw: unknown): string {
  const value = raw ?? field.default;
  if (field.type === "keyvalue") {
    const rows = Array.isArray(value) ? (value as { name: string; value: string }[]) : [];
    return rows.length ? rows.map((r) => `${r.name}: ${r.value}`).join(", ") : "None";
  }
  if (field.items === "transform") {
    const rows = Array.isArray(value) ? value : [];
    return rows.length ? `${rows.length} ${rows.length === 1 ? "adjustment" : "adjustments"}` : "None";
  }
  if (value === undefined || value === null || value === "") return "None";
  if (field.type === "select") return field.options?.find((o) => o.value === value)?.label ?? String(value);
  if (field.type === "duration" || field.type === "number") {
    const unit = field.unit === "minutes" ? "minutes" : field.unit === "tokens" ? "tokens" : "seconds";
    return `${value} ${unit}`;
  }
  return String(value);
}

export function NotEditableRecord({
  view,
  definition,
  onRefresh,
  onSaved,
  onRemove,
}: {
  view: EngineView;
  definition: EngineDefinition;
  onRefresh: () => Promise<void>;
  onSaved: (message: string) => Promise<void>;
  onRemove: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(view.name);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [enabling, setEnabling] = useState<boolean | null>(null);
  const [enableError, setEnableError] = useState<string | null>(null);
  const values = view.values as unknown as Record<string, unknown>;
  const renamed = name.trim() !== view.name && name.trim() !== "";
  const needKey = view.check.reason === "needkey" && view.key.state === "none";

  async function setEnabled(on: boolean) {
    setEnabling(on);
    setEnableError(null);
    try {
      const registry: ServiceRegistry = await api.services.edit(view.service_id, { enabled: on });
      queryClient.setQueryData(queryKeys.serviceRegistry, registry);
      await onRefresh();
    } catch (error) {
      setEnableError(errorText(error));
    } finally {
      setEnabling(null);
    }
  }

  async function saveName() {
    setSaving(true);
    setSaveError(null);
    try {
      const registry = await api.services.edit(view.service_id, { name: name.trim() });
      queryClient.setQueryData(queryKeys.serviceRegistry, registry);
      await onSaved("Saved.");
    } catch (error) {
      setSaveError(errorText(error));
    } finally {
      setSaving(false);
    }
  }

  const rows: [string, string][] = [["Address", view.values.address ?? ""]];
  rows.push([
    definition.fields.find((f) => f.key === "api_key")?.label ?? "API key",
    view.key.state === "configured"
      ? view.key.source === "env" && view.key.env_name
        ? `Configured. Read from ${view.key.env_name} on the gateway’s computer.`
        : "Configured"
      : "None",
  ]);
  for (const field of optionFields(definition)) {
    if (field.show_if && String(values[field.show_if.field] ?? field.default ?? "") !== String(field.show_if.equals)) continue;
    rows.push([field.label, shown(field, values[field.key])]);
  }
  const abilities = definition.capabilities.filter((c) => view.capabilities.includes(c.id)).map((c) => c.label);

  return (
    <>
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-title [overflow-wrap:anywhere]">{view.name}</h1>
          <div className="text-meta text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            {view.enabled ? <StateWord tone="ok" word="Ready" /> : <StateWord tone="off" word="Off" />}
            {view.check.checked_at ? (
              <span>
                Last checked <RelativeTime ts={view.check.checked_at} fallback="" />
              </span>
            ) : null}
          </div>
        </div>
        <label className="text-meta flex min-h-8 items-center gap-3 max-sm:min-h-11">
          <Switch
            aria-label="Enabled"
            checked={enabling ?? view.enabled}
            disabled={enabling !== null || needKey}
            onCheckedChange={(on) => void setEnabled(on)}
          />
          Enabled
        </label>
      </header>
      {enableError && <Callout as="div" severity="critical" title="Enabled could not change" meta={enableError} />}
      <Callout as="div" severity="attention" title="These settings can’t be changed here" meta={view.not_editable_reason} />
      <nav aria-label="On this page" className="text-meta flex flex-wrap gap-x-4 gap-y-1">
        <a className="text-link hover:underline" href="#engine-connection">Connection</a>
        <a className="text-link hover:underline" href="#engine-abilities">What it can do</a>
        <a className="text-link hover:underline" href="#engine-models">Models</a>
      </nav>

      <section aria-labelledby="engine-connection" className="flex flex-col gap-2">
        <h2 id="engine-connection" className="text-heading scroll-mt-20">Connection</h2>
        <div className="bg-card rounded-panel flex flex-col gap-4 border p-4">
          <FormField label="Name">
            {(control) => <Input {...control} value={name} autoComplete="off" onChange={(event) => setName(event.target.value)} />}
          </FormField>
          {(renamed || saveError) && (
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="md" disabled={!renamed || saving} onClick={() => void saveName()}>
                {saving && <Loader2 aria-hidden="true" className="animate-spin" />}
                Save changes
              </Button>
              <Button type="button" variant="ghost" size="md" disabled={saving} onClick={() => setName(view.name)}>
                Discard
              </Button>
              {saveError && (
                <span role="alert" className="text-meta text-danger">
                  {saveError}
                </span>
              )}
            </div>
          )}
          <dl data-testid="engine-values" className="text-meta grid gap-x-4 gap-y-1.5 sm:grid-cols-[auto_1fr]">
            {rows.map(([term, value]) => (
              <div key={term} className="contents">
                <dt className="text-muted-foreground">{term}</dt>
                <dd className="min-w-0 [overflow-wrap:anywhere]">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      <section aria-labelledby="engine-abilities" className="flex flex-col gap-2">
        <h2 id="engine-abilities" className="text-heading scroll-mt-20">What it can do</h2>
        <p className="text-meta">{abilities.length ? abilities.join(", ") : "Nothing is turned on."}</p>
      </section>

      <section aria-labelledby="engine-models" className="flex flex-col gap-2">
        <h2 id="engine-models" className="text-heading scroll-mt-20">Models</h2>
        {view.models.length === 0 ? (
          <p className="text-meta text-muted-foreground">No models yet.</p>
        ) : (
          <ul className="bg-card rounded-panel divide-y border">
            {view.models.map((model) => (
              <li key={model.id} className="text-meta flex flex-wrap items-center justify-between gap-x-4 gap-y-1 p-4">
                <span className="min-w-0">
                  <span className="block font-mono [overflow-wrap:anywhere]">{model.id}</span>
                  <span className="text-muted-foreground block">
                    Apps use the name “{model.public_name}”
                    {model.context_window != null ? `. Context window ${model.context_window} tokens.` : "."}
                  </span>
                </span>
                {model.enabled !== false ? (
                  <Tag>
                    <CircleCheck aria-hidden="true" className="size-3" />
                    Offered
                  </Tag>
                ) : (
                  <Tag>Not offered</Tag>
                )}
              </li>
            ))}
          </ul>
        )}
        <AccessAnswer access={view.access} />
      </section>

      <StoredRecordSection view={view} />
      {!view.locked && <RemoveSection onRemove={onRemove} />}
    </>
  );
}

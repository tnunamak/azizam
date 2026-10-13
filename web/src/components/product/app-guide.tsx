"use client";

import { useId, useState } from "react";
import { Check, Copy, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { copyText } from "@/components/ui/copy-button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  keyAppById,
  keyApps,
  resolveKeyGuide,
  type GuideValues,
  type ResolvedField,
} from "@/lib/app-guides";
import { CodeBlock } from "./code-block";

/**
 * Step-by-step setup for one app (lib/app-guides.json): where to click, the
 * exact fields the app asks for with the values to paste, and a snippet when
 * the app reads a file or code. Every app renders through this one component.
 */
export function AppGuide({
  appId,
  onAppChange,
  values,
  keyHint,
  label = "Your app",
  loading = false,
}: {
  appId: string;
  onAppChange: (id: string) => void;
  values: GuideValues;
  /** What the key field says: where the key is ("Copy it from above"). */
  keyHint: string;
  label?: string;
  /** True while model names are still loading: no "none available" yet. */
  loading?: boolean;
}) {
  const selectId = useId();
  const app = keyAppById(appId) ?? keyApps[0];
  const guide = resolveKeyGuide(app.key, values);
  const groups = [...new Set(guide.fields.map((field) => field.group ?? ""))];
  const kinds = [...new Set(keyApps.map((item) => item.kind))];
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-col gap-2">
        <label htmlFor={selectId} className="text-label">
          {label}
        </label>
        <Select value={app.id} onValueChange={onAppChange}>
          <SelectTrigger id={selectId} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {kinds.map((kind) => (
              <SelectGroup key={kind}>
                <SelectLabel>{kind}</SelectLabel>
                {keyApps
                  .filter((item) => item.kind === kind)
                  .map((item) => (
                    <SelectItem key={item.id} value={item.id}>
                      {item.name}
                    </SelectItem>
                  ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
      </div>
      <ol
        aria-label={`How to connect ${app.name}`}
        className="text-body list-decimal space-y-1 pl-5"
      >
        {guide.steps.map((step) => (
          <li key={step} className="break-words">
            {step}
          </li>
        ))}
      </ol>
      {!loading && guide.missingTasks.length > 0 && (
        <p role="note" className="text-meta flex items-start gap-2">
          <TriangleAlert aria-hidden="true" className="text-warning mt-0.5 size-4 shrink-0" />
          {app.name} needs a {guide.missingTasks.join(" and a ")} model, and
          none is available to you. Ask the owner to add one.
        </p>
      )}
      {groups.map((group) => (
        <div key={group} className="flex flex-col gap-3">
          {group ? <p className="text-body font-medium">{group}</p> : null}
          {guide.fields
            .filter((field) => (field.group ?? "") === group)
            .map((field) => (
              <GuideField
                key={`${group}-${field.label}`}
                field={field}
                keyHint={keyHint}
                loading={loading}
              />
            ))}
        </div>
      ))}
      {guide.snippet ? (
        <div className="[&_pre]:break-all [&_pre]:whitespace-pre-wrap">
          <CodeBlock snippets={[guide.snippet]} />
        </div>
      ) : null}
      {guide.note ? (
        <p className="text-muted-foreground text-meta">{guide.note}</p>
      ) : null}
      <p className="text-meta flex flex-wrap items-center gap-2">
        <a
          href={guide.docs}
          target="_blank"
          rel="noreferrer noopener"
          className="underline underline-offset-4"
        >
          {app.name} documentation
        </a>
        {guide.status === "unverified" ? (
          <Badge variant="outline">From third-party guides</Badge>
        ) : null}
      </p>
    </div>
  );
}

/** One field the app asks for: its exact label, and the value to paste. */
function GuideField({
  field,
  keyHint,
  loading,
}: {
  field: ResolvedField;
  keyHint: string;
  loading: boolean;
}) {
  const id = useId();
  return (
    <div className="grid min-w-0 items-center gap-x-3 gap-y-1 sm:grid-cols-[11rem_minmax(0,1fr)]">
      <label htmlFor={id} className="text-label min-w-0 break-words">
        {field.label}
      </label>
      {field.isKey ? (
        <p id={id} className="text-muted-foreground text-meta">
          {keyHint}
        </p>
      ) : field.missing ? (
        <p id={id} className="text-muted-foreground text-meta">
          {loading ? "Loading model names…" : "No model available."}
        </p>
      ) : (
        <CopyValue id={id} label={field.label} value={field.value} />
      )}
      {field.note ? (
        <p className="text-muted-foreground text-meta sm:col-start-2">{field.note}</p>
      ) : null}
    </div>
  );
}

function CopyValue({ id, label, value }: { id: string; label: string; value: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <div className="min-w-0">
      <div className="border-input bg-card focus-within:border-ring flex min-w-0 items-center rounded-control border">
        <Input
          id={id}
          value={value}
          readOnly
          onFocus={(event) => event.currentTarget.select()}
          className="h-8 min-w-0 truncate border-0 bg-transparent font-mono text-sm focus-visible:-outline-offset-2 sm:text-sm"
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="text-muted-foreground hover:text-foreground mr-0.5"
          aria-label={`Copy ${label}`}
          onClick={async () => setState((await copyText(value)) ? "copied" : "failed")}
        >
          {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        </Button>
      </div>
      <span role="status" className="sr-only">
        {state === "copied" ? `${label} copied.` : ""}
      </span>
      {state === "failed" ? (
        <p role="alert" className="text-destructive text-meta mt-1">
          Copy failed. Select the value and copy it yourself.
        </p>
      ) : null}
    </div>
  );
}

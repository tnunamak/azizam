"use client";

import { useId, useState, type ReactNode } from "react";
import { Check, Copy, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { copyText } from "@/components/ui/copy-button";
import { Badge } from "@/components/ui/badge";
import {
  appGuides,
  clientById,
  resolveKeyGuide,
  type AppGuide as ClientGuide,
  type GuideValues,
  type McpGuide,
  type ResolvedField,
} from "@/lib/app-guides";
import { clientDocsUrl, MCP_DOCS_URL } from "@/lib/docs";

/**
 * The values one client needs (lib/app-guides.json): the addresses, the key's
 * place and a model name, each to copy. The steps and notes live on the public
 * docs page, one link away. Every client renders through this one component.
 */
export function AppGuide({
  appId,
  onAppChange,
  values,
  keyHint,
  loading = false,
}: {
  appId: string;
  onAppChange: (id: string) => void;
  values: GuideValues;
  /** Where the key goes, in words. The key itself is never shown here. */
  keyHint: ReactNode;
  /** True while model names are still loading: no "none available" yet. */
  loading?: boolean;
}) {
  const app = clientById(appId) ?? appGuides.apps[0];
  return (
    <div className="flex min-w-0 flex-col gap-5">
      <ClientPicker selectedId={app.id} onSelect={onAppChange} />
      <ClientValues
        app={app}
        values={values}
        keyHint={keyHint}
        loading={loading}
      />
    </div>
  );
}

/**
 * Every client, key or MCP, as a toggle, so none hides behind a menu. The
 * search narrows the list by name or kind; the chosen client stays selected.
 */
function ClientPicker({
  selectedId,
  onSelect,
}: {
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const labelId = useId();
  const [query, setQuery] = useState("");
  const words = query.trim().toLowerCase();
  const shown = appGuides.apps.filter((app) =>
    `${app.name} ${app.kind}`.toLowerCase().includes(words),
  );
  return (
    <div
      role="group"
      aria-labelledby={labelId}
      className="flex min-w-0 flex-col gap-2"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p id={labelId} className="text-label">
          Setting up in
        </p>
        <Input
          type="search"
          aria-label="Find an app"
          placeholder="Find an app"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="h-9 w-full sm:w-56"
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {shown.map((app) => {
          const selected = app.id === selectedId;
          return (
            <Button
              key={app.id}
              type="button"
              size="sm"
              variant={selected ? "default" : "outline"}
              aria-pressed={selected}
              onClick={() => onSelect(app.id)}
            >
              {app.name}
            </Button>
          );
        })}
      </div>
      {shown.length === 0 ? (
        <p className="text-muted-foreground text-meta">
          No app matches “{query.trim()}”.
        </p>
      ) : null}
    </div>
  );
}

function ClientValues({
  app,
  values,
  keyHint,
  loading,
}: {
  app: ClientGuide;
  values: GuideValues;
  keyHint: ReactNode;
  loading: boolean;
}) {
  const key = app.key ? resolveKeyGuide(app.key, values) : null;
  const groups = key
    ? [...new Set(key.fields.map((field) => field.group ?? ""))]
    : [];
  const unverified =
    app.key?.status === "unverified" || app.mcp?.status === "unverified";
  return (
    <section
      aria-label={`Values for ${app.name}`}
      className="flex min-w-0 flex-col gap-4"
    >
      {key
        ? groups.map((group) => (
            <div key={group} className="flex flex-col gap-3">
              {group ? <p className="text-body font-medium">{group}</p> : null}
              {key.fields
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
          ))
        : null}
      {key && !loading && key.missingTasks.length > 0 ? (
        <p role="note" className="text-meta flex items-start gap-2">
          <TriangleAlert
            aria-hidden="true"
            className="text-warning mt-0.5 size-4 shrink-0"
          />
          {app.name} needs a {key.missingTasks.join(" and a ")} model, and none
          is available to you. Ask the owner to add one.
        </p>
      ) : null}
      {app.mcp ? (
        <McpRow auth={app.mcp.auth} appName={app.name} url={values.mcp_url} />
      ) : null}
      <p className="text-meta flex flex-wrap items-center gap-x-4 gap-y-2">
        <a
          href={clientDocsUrl(app.id)}
          target="_blank"
          rel="noreferrer noopener"
          className="underline underline-offset-4"
        >
          Full steps for {app.name} →
        </a>
        {app.mcp ? (
          <a
            href={MCP_DOCS_URL}
            target="_blank"
            rel="noreferrer noopener"
            className="underline underline-offset-4"
          >
            MCP docs →
          </a>
        ) : null}
        {unverified ? (
          <Badge variant="outline">From third-party guides</Badge>
        ) : null}
      </p>
    </section>
  );
}

/** The MCP server address, and how the client connects to it: sign-in, or a key too. */
function McpRow({
  auth,
  appName,
  url,
}: {
  auth: McpGuide["auth"];
  appName: string;
  url: string;
}) {
  const id = useId();
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <FieldRow id={id} label="MCP server">
        <CopyValue id={id} label="MCP server" value={url} />
      </FieldRow>
      <p className="text-muted-foreground text-meta">
        {auth === "sign-in"
          ? `${appName} signs in with your account. It takes no key.`
          : `${appName} signs in with your account, or takes a key from Apps & keys.`}
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
  keyHint: ReactNode;
  loading: boolean;
}) {
  const id = useId();
  return (
    <FieldRow id={id} label={field.label}>
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
    </FieldRow>
  );
}

/** A label beside its value on wide screens, above it on a phone. */
function FieldRow({
  id,
  label,
  children,
}: {
  id: string;
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="grid min-w-0 items-center gap-x-3 gap-y-1 sm:grid-cols-[11rem_minmax(0,1fr)]">
      <label htmlFor={id} className="text-label min-w-0 break-words">
        {label}
      </label>
      {children}
    </div>
  );
}

function CopyValue({
  id,
  label,
  value,
}: {
  id: string;
  label: string;
  value: string;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <div className="min-w-0">
      <div className="border-input bg-card focus-within:border-ring rounded-control flex min-w-0 items-center border">
        <Input
          id={id}
          value={value}
          readOnly
          onFocus={(event) => event.currentTarget.select()}
          className="h-8 min-w-0 border-0 bg-transparent font-mono text-sm focus-visible:-outline-offset-2 sm:text-sm"
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="text-muted-foreground hover:text-foreground mr-0.5"
          aria-label={`Copy ${label}`}
          onClick={async () =>
            setState((await copyText(value)) ? "copied" : "failed")
          }
        >
          {state === "copied" ? (
            <Check aria-hidden="true" />
          ) : (
            <Copy aria-hidden="true" />
          )}
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

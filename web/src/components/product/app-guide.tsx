"use client";

import { useId, useState, type ReactNode } from "react";
import Link from "next/link";
import { Check, Copy, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { copyText } from "@/components/ui/copy-button";
import { Badge } from "@/components/ui/badge";
import {
  anyOpenAIGuide,
  appGuides,
  clientById,
  resolveKeyGuide,
  type AppGuide as ClientGuide,
  type GuideValues,
  type McpGuide,
  type ResolvedField,
} from "@/lib/app-guides";
import { clientDocsUrl, MCP_DOCS_URL } from "@/lib/docs";

/** The first clients the picker shows; the rest sit behind "More clients" until a search or that button shows them. */
const shownFirst = 6;

/**
 * The values one client needs (lib/app-guides.json): the addresses, the key's
 * place and a model name, each to copy. The steps and notes live on the public
 * docs page, one link away. With no client picked, the generic recipe shows.
 */
export function AppGuide({
  clientId,
  onClientChange,
  values,
  keyHint,
  owner,
  loading = false,
}: {
  /** The picked client's id, or null when none is picked. */
  clientId: string | null;
  /** Picks a client, or clears the pick (null) to show the generic recipe. */
  onClientChange: (id: string | null) => void;
  values: GuideValues;
  /** Where the key goes, in words. The key itself is never shown here. */
  keyHint: ReactNode;
  /** An owner adds a missing model in Models; anyone else asks the owner. */
  owner: boolean;
  /** True while model names are still loading: no "none available" yet. */
  loading?: boolean;
}) {
  const client = clientById(clientId) ?? null;
  return (
    <div className="flex min-w-0 flex-col gap-5">
      <ClientPicker selected={client} onSelect={onClientChange} />
      <ClientValues
        recipe={client ?? anyOpenAIGuide}
        generic={client === null}
        values={values}
        keyHint={keyHint}
        owner={owner}
        loading={loading}
      />
    </div>
  );
}

/**
 * The clients, as toggles. The first few show in data order, then "More
 * clients"; the search finds any of them. Pressing the picked client again
 * clears the pick.
 */
function ClientPicker({
  selected,
  onSelect,
}: {
  selected: ClientGuide | null;
  onSelect: (id: string | null) => void;
}) {
  const labelId = useId();
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const words = query.trim().toLowerCase();
  const matching = appGuides.apps.filter((app) =>
    `${app.name} ${app.kind}`.toLowerCase().includes(words),
  );
  const shown = words || expanded ? matching : matching.slice(0, shownFirst);
  const collapsible = !words && appGuides.apps.length > shownFirst;
  return (
    <div
      role="group"
      aria-labelledby={labelId}
      className="flex min-w-0 flex-col gap-2"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p id={labelId} className="text-label">
          {selected ? `Setting up: ${selected.name}` : "Clients"}
        </p>
        <Input
          type="search"
          aria-label="Find a client"
          placeholder="Find a client"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="h-9 w-full sm:w-56"
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {shown.map((app) => {
          const isSelected = app.id === selected?.id;
          return (
            <Button
              key={app.id}
              type="button"
              size="sm"
              variant={isSelected ? "default" : "outline"}
              aria-pressed={isSelected}
              onClick={() => onSelect(isSelected ? null : app.id)}
            >
              {app.name}
            </Button>
          );
        })}
        {collapsible ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "Fewer clients" : "More clients"}
          </Button>
        ) : null}
      </div>
      {shown.length === 0 ? (
        <p className="text-muted-foreground text-meta">
          No client matches “{query.trim()}”.
        </p>
      ) : null}
    </div>
  );
}

function ClientValues({
  recipe,
  generic,
  values,
  keyHint,
  owner,
  loading,
}: {
  recipe: ClientGuide;
  /** True for the recipe with no client picked: it is titled, since no picker names it. */
  generic: boolean;
  values: GuideValues;
  keyHint: ReactNode;
  owner: boolean;
  loading: boolean;
}) {
  const key = recipe.key ? resolveKeyGuide(recipe.key, values) : null;
  const groups = key
    ? [...new Set(key.fields.map((field) => field.group ?? ""))]
    : [];
  const unverified =
    recipe.key?.status === "unverified" || recipe.mcp?.status === "unverified";
  const hasDocs = Boolean(recipe.key?.docs);
  return (
    <section
      aria-label={`Values for ${recipe.name}`}
      className="flex min-w-0 flex-col gap-4"
    >
      {generic ? <p className="text-body font-medium">{recipe.name}</p> : null}
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
                    owner={owner}
                    loading={loading}
                  />
                ))}
            </div>
          ))
        : null}
      {recipe.mcp ? (
        <McpRow auth={recipe.mcp.auth} appName={recipe.name} url={values.mcp_url} />
      ) : null}
      {hasDocs || recipe.mcp || unverified ? (
        <p className="text-meta flex flex-wrap items-center gap-x-4 gap-y-2">
          {hasDocs ? (
            <a
              href={clientDocsUrl(recipe.id)}
              target="_blank"
              rel="noreferrer noopener"
              className="underline underline-offset-4"
            >
              Full steps for {recipe.name} →
            </a>
          ) : null}
          {recipe.mcp ? (
            <a
              href={MCP_DOCS_URL}
              target="_blank"
              rel="noreferrer noopener"
              className="underline underline-offset-4"
            >
              MCP docs →
            </a>
          ) : null}
          {unverified ? <Badge variant="outline">From third-party guides</Badge> : null}
        </p>
      ) : null}
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
  owner,
  loading,
}: {
  field: ResolvedField;
  keyHint: ReactNode;
  owner: boolean;
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
        <MissingModel id={id} task={field.missingTask} owner={owner} loading={loading} />
      ) : (
        <CopyValue id={id} label={field.label} value={field.value} />
      )}
    </FieldRow>
  );
}

/** The warning sits in the row it explains, and says what this person can do about it. */
function MissingModel({
  id,
  task,
  owner,
  loading,
}: {
  id: string;
  task?: string;
  owner: boolean;
  loading: boolean;
}) {
  if (loading)
    return (
      <p id={id} className="text-muted-foreground text-meta">
        Loading model names…
      </p>
    );
  return (
    <p id={id} className="text-meta flex items-start gap-2">
      <TriangleAlert
        aria-hidden="true"
        className="text-warning mt-0.5 size-4 shrink-0"
      />
      <span>
        No {task ? `${task} ` : ""}model available.{" "}
        {owner ? (
          <>
            Add a model in{" "}
            <Link prefetch={false} href="/models" className="underline underline-offset-4">
              Models
            </Link>
            .
          </>
        ) : (
          "Ask the person who set up this gateway to add one."
        )}
      </span>
    </p>
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

/** A value to copy. A textarea, so a long address wraps on a phone instead of clipping its end. */
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
        <textarea
          id={id}
          value={value}
          readOnly
          rows={1}
          onFocus={(event) => event.currentTarget.select()}
          className="field-sizing-content min-h-8 max-sm:min-h-11 max-sm:py-3 min-w-0 flex-1 resize-none px-3 py-1.5 font-mono text-sm break-all bg-transparent outline-none"
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

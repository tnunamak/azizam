"use client";

/**
 * The read-only parts of the engine page that come from the engine record
 * (engine.v2): where requests go and where the key goes, which offering a
 * request reaches, who owns the process, what Azizam can check and the
 * resources. Nothing here names an engine: it reads the record's own blocks.
 */
import type { ReactNode } from "react";
import { CircleCheck, CircleHelp, CircleX } from "lucide-react";
import type {
  EngineDefinition,
  EngineField,
  EngineRecordV2,
  EngineView,
  ObservationV2,
  ProcessV2,
} from "@/lib/engines-api";
import { RelativeTime } from "@/components/ui/relative-time";
import { Note, Tag } from "../parts/form";

export const OWNER_ONLY = "Only the owner can change this.";
export const DERIVED_REASON =
  "Azizam builds this record itself from your aliases and engines, so it stays in step with them. Change those instead.";
export const UNSUPPORTED_REASON =
  "This page can’t change this setting yet. Azizam keeps it exactly as stored.";
export const ARGV_REASON =
  "These commands are stored as exact argument lists. This page edits a command as one line, which could change them, so it shows them as stored.";

/** The record part a definition field edits. A definition can say; otherwise owner-only fields are the process and adjustments the offerings. */
export function fieldPart(
  field: EngineField,
): "connection" | "offerings" | "process" {
  if (field.part) return field.part;
  if (field.owner_only) return "process";
  if (field.items === "transform") return "offerings";
  // The model list and its per-model numbers are offerings, not connection settings.
  if (
    !field.advanced &&
    (field.type === "multiselect" || field.type === "number")
  )
    return "offerings";
  return "connection";
}

/** Why a section is read-only for this person, or null when they can change it. */
export function lockReason(view: EngineView, owner: boolean): string | null {
  if (view.locked || view.v2?.derived) return DERIVED_REASON;
  if (!owner) return OWNER_ONLY;
  return null;
}

/** A read-only note with the reason, and the source when the values are derived. */
export function LockedNote({
  lead,
  reason,
  view,
}: {
  lead?: string;
  reason: string;
  view: EngineView;
}) {
  return (
    <Note tone="lock">
      {lead && <span className="block">{lead}</span>}
      {reason}
      {reason === DERIVED_REASON && (
        <span className="text-muted-foreground block">
          Source: {sourceWords(view.v2)}.
        </span>
      )}
    </Note>
  );
}

function sourceWords(v2: EngineRecordV2 | undefined): string {
  if (!v2) return "the stored record";
  if (v2.record_type === "system") return "derived by Azizam (system record)";
  return (
    {
      ui: "added on this page",
      provisioned: "the gateway’s setup files",
      imported: "an import",
      builtin: "built into Azizam",
    }[v2.provenance?.source] ??
    v2.provenance?.source ??
    "the stored record"
  );
}

function Facts({
  rows,
  testId,
}: {
  rows: [string, ReactNode][];
  testId?: string;
}) {
  return (
    <dl
      data-testid={testId}
      className="text-meta grid gap-x-4 gap-y-1.5 sm:grid-cols-[minmax(0,auto)_minmax(0,1fr)]"
    >
      {rows.map(([term, value]) => (
        <div key={term} className="contents">
          <dt className="text-muted-foreground">{term}</dt>
          <dd className="min-w-0 [overflow-wrap:anywhere]">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

const mono = (text: string) => <span className="font-mono">{text}</span>;
/** An argument list exactly as stored: an argument with a space or a quote shows in quotes, so the boundaries stay visible. */
export function argvText(list: string[]): string {
  return list
    .map((arg) =>
      arg === "" || /[\s"'\\]/.test(arg) ? JSON.stringify(arg) : arg,
    )
    .join(" ");
}
const argv = (list: string[] | undefined) =>
  list?.length ? mono(argvText(list)) : "None";

export function credentialOrigin(view: EngineView): string {
  const dest = view.credential_destination;
  if (typeof dest === "string" && dest) return dest;
  if (dest && typeof dest === "object" && dest.origin) return dest.origin;
  return (
    view.v2?.connection.credentials.inference?.origin || view.key.origin || ""
  );
}

/** Connection: the request path and the key's destination, as saved. */
export function ConnectionFacts({
  view,
  definition,
}: {
  view: EngineView;
  definition: EngineDefinition;
}) {
  const rows: [string, ReactNode][] = [];
  const path = view.request_path;
  if (path?.url)
    rows.push([
      "Requests go to",
      <span key="path">
        {mono(path.url)}
        <span className="text-muted-foreground block">
          {path.mode === "gateway_lifecycle"
            ? "Through the gateway’s start-and-stop hop, which then calls this address."
            : "Straight from the model router to this address."}{" "}
          Source: {path.source}.
        </span>
      </span>,
    ]);
  else
    rows.push([
      "Requests go to",
      <span key="path" className="text-muted-foreground">
        Unknown. The gateway did not say.
      </span>,
    ]);
  if (definition.fields.some((f) => f.key === "api_key")) {
    const origin = credentialOrigin(view);
    rows.push([
      "Key is sent to",
      view.key.state === "configured" && origin ? (
        <span key="dest">
          {mono(origin)} only. Azizam never sends it to another address.
        </span>
      ) : (
        "No key is sent."
      ),
    ]);
  }
  const protocol = view.v2?.connection.protocol;
  if (protocol) rows.push(["Protocol", mono(protocol)]);
  return <Facts rows={rows} testId="connection-facts" />;
}

const selectionWords = {
  request_field: "The model field of each request picks it.",
  switch: "Azizam switches the engine to it, then checks the switch.",
  whole_graph: "Each request runs the whole workflow.",
} as const;

/** What it offers: how a request reaches each offering, and which one is selected now. */
export function OfferingFacts({ v2 }: { v2: EngineRecordV2 | undefined }) {
  const offerings = v2?.offerings ?? [];
  if (!offerings.length)
    return (
      <p className="text-meta text-muted-foreground">
        The record lists no offerings yet.
      </p>
    );
  const switching = offerings.some((o) => o.selection?.mode === "switch");
  return (
    <div className="flex flex-col gap-2" data-testid="offering-facts">
      {switching && (
        <p className="text-meta">
          Selected now:{" "}
          {v2?.selected_offering ? (
            mono(v2.selected_offering)
          ) : (
            <span className="text-muted-foreground">
              Unknown. Azizam has not observed it.
            </span>
          )}
        </p>
      )}
      <ul className="bg-card rounded-panel divide-y border">
        {offerings.map((o) => (
          <li key={o.id} className="text-meta flex flex-col gap-0.5 p-3">
            <span className="flex flex-wrap items-center gap-2">
              {mono(o.id)}
              <Tag>{o.kind === "workflow" ? "Workflow" : "Model"}</Tag>
            </span>
            {o.upstream && o.upstream !== o.id && (
              <span className="text-muted-foreground">
                Sent as {mono(o.upstream)}
              </span>
            )}
            <span className="text-muted-foreground">
              {o.selection
                ? selectionWords[o.selection.mode]
                : "How a request reaches it is not recorded."}
              {o.selection?.mode === "switch" && o.selection.switch?.call ? (
                <> Switch call: {mono(o.selection.switch.call)}.</>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function processOwner(process: ProcessV2 | null | undefined): string {
  if (!process || process.driver === "none")
    return "Not Azizam. Something outside Azizam keeps this server running; Azizam only sends it requests.";
  if (process.driver === "command")
    return "An outside manager. Azizam runs the start and stop commands; it never claims the process.";
  return "Azizam. It starts the process itself and owns it until it stops.";
}

/** How it runs: who owns the process, and the parts of it this page can't edit. */
export function ProcessFacts({
  view,
  lifecycleEditable,
  note,
}: {
  view: EngineView;
  lifecycleEditable: boolean;
  note?: string;
}) {
  const process = view.v2?.process;
  const rows: [string, ReactNode][] = [["Who runs it", processOwner(process)]];
  if (process && process.driver !== "none") {
    if (!lifecycleEditable) {
      rows.push(["Start", argv(process.start)], ["Stop", argv(process.stop)]);
    }
    if (process.status?.length) rows.push(["Status", argv(process.status)]);
    if (!lifecycleEditable && process.cwd)
      rows.push(["Runs in", mono(process.cwd)]);
    if (!lifecycleEditable && process.env && Object.keys(process.env).length)
      rows.push(["Environment", mono(Object.keys(process.env).join(", "))]);
    if (process.port?.fixed) rows.push(["Port", String(process.port.fixed)]);
  }
  const apply = process?.profile_apply;
  if (apply?.file)
    rows.push([
      "Settings file",
      <span key="file">
        {mono(apply.file)}
        <span className="text-muted-foreground block">
          This file belongs to your system. Azizam writes it before a start
          {apply.restore === "on_failure"
            ? " and restores it if the start fails"
            : ""}
          . Edit its location in your system, not here.
        </span>
      </span>,
    ]);
  const residency = view.v2?.residency ?? {};
  const idleOwner = residency.idle_owner;
  if (typeof idleOwner === "string")
    rows.push([
      "Stops when idle",
      idleOwner === "gateway"
        ? `Azizam stops it after ${residency.idle_stop_s ?? "?"} seconds idle.`
        : "The engine decides.",
    ]);
  const legacy = Boolean(
    process?.needs_owner_conversion || residency.needs_owner_conversion,
  );
  return (
    <div className="flex flex-col gap-2">
      <Facts rows={rows} testId="process-facts" />
      {note && !legacy && <Note tone="lock">{note}</Note>}
      {legacy && (
        <Note tone="warn">
          This engine still uses an older start-and-stop setup (
          {String(
            process?.legacy_driver ?? residency.legacy_driver ?? "unknown",
          )}
          ). {UNSUPPORTED_REASON}
        </Note>
      )}
    </div>
  );
}

const OBSERVATIONS: [string, string][] = [
  ["process_ready", "The server is up"],
  ["inventory", "Which models it has"],
  ["offering_ready", "A model is ready"],
  ["selected", "Which model is selected"],
  ["resident", "Which models are in memory"],
  ["sleep_state", "Whether it is asleep"],
];

function howWords(o: ObservationV2): ReactNode {
  const select = typeof o.select === "string" ? o.select : o.http?.select;
  if (o.http?.path)
    return (
      <>
        Asks {mono(`GET ${o.http.path}`)}
        {o.http.expect ? `, expects ${o.http.expect}` : ""}
        {select ? <>, reads {mono(select)}</> : null}.
      </>
    );
  return "Declared in the record.";
}

/** What Azizam can check: each observation, how it looks and what it saw. Missing is unknown, never green. */
export function ObservationList({ view }: { view: EngineView }) {
  const observations = view.v2?.observations ?? {};
  const known = new Set(OBSERVATIONS.map(([key]) => key));
  const rows: [string, string, ObservationV2 | undefined][] = [
    ...OBSERVATIONS.map(
      ([key, label]) =>
        [key, label, observations[key]] as [
          string,
          string,
          ObservationV2 | undefined,
        ],
    ),
    ...Object.entries(observations)
      .filter(([key]) => !known.has(key))
      .map(([key, o]) => [key, key, o] as [string, string, ObservationV2]),
  ];
  return (
    <ul
      className="bg-card rounded-panel divide-y border"
      data-testid="observations"
    >
      <li className="text-meta flex items-start gap-2 p-3">
        <Evidence
          ok={
            view.check.status === "ok"
              ? true
              : view.check.status === "failed"
                ? false
                : undefined
          }
        />
        <span className="min-w-0 [overflow-wrap:anywhere]">
          <span className="block font-medium">Reaches the server</span>
          <span className="text-muted-foreground block">
            The connection check asks {mono(view.calls)}.{" "}
            {view.check.checked_at ? (
              <>
                Last result:{" "}
                {view.check.status === "ok" ? "reached" : "a problem"},{" "}
                <RelativeTime ts={view.check.checked_at} fallback="" />.
              </>
            ) : (
              "Not checked yet."
            )}
          </span>
        </span>
      </li>
      {rows.map(([key, label, o]) => (
        <li key={key} className="text-meta flex items-start gap-2 p-3">
          <Evidence ok={o?.last?.ok} />
          <span className="min-w-0 [overflow-wrap:anywhere]">
            <span className="block font-medium">{label}</span>
            <span className="text-muted-foreground block">
              {o ? (
                <>
                  {howWords(o)}{" "}
                  {o.last ? (
                    <>
                      Last result:{" "}
                      {o.last.detail ??
                        (o.last.ok ? "as expected" : "not as expected")}
                      {o.last.at ? (
                        <>
                          , <RelativeTime ts={o.last.at} fallback="" />
                        </>
                      ) : null}
                      .
                    </>
                  ) : (
                    "No result yet, so it is unknown."
                  )}
                </>
              ) : (
                "Not declared, so Azizam can’t tell. It shows as unknown."
              )}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function Evidence({ ok }: { ok: boolean | undefined }) {
  if (ok === true)
    return (
      <CircleCheck
        aria-label="Seen"
        className="text-success mt-0.5 size-4 shrink-0"
      />
    );
  if (ok === false)
    return (
      <CircleX
        aria-label="Problem"
        className="text-danger mt-0.5 size-4 shrink-0"
      />
    );
  return (
    <CircleHelp
      aria-label="Unknown"
      className="text-muted-foreground mt-0.5 size-4 shrink-0"
    />
  );
}

/** Resources: what the record says the engine uses. Advisory unless enforced. */
export function ResourceFacts({ v2 }: { v2: EngineRecordV2 | undefined }) {
  const resources = v2?.resources ?? {};
  const entries = Object.entries(resources);
  if (!entries.length)
    return <p className="text-meta text-muted-foreground">None recorded.</p>;
  const rows: [string, ReactNode][] = entries.map(([key, value]) => {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const v = value as Record<string, unknown>;
      const parts = Object.entries(v)
        .filter(([k]) => k !== "enforced")
        .map(
          ([k, x]) =>
            `${k.replace(/_/g, " ")}: ${Array.isArray(x) ? x.join(", ") : String(x)}`,
        );
      const enforced =
        v.enforced === true
          ? "Enforced."
          : "Advisory: Azizam plans with it but does not enforce it.";
      return [
        key.toUpperCase() === "GPU" ? "GPU" : key,
        `${parts.join("; ")}. ${enforced}`,
      ];
    }
    return [key, String(value)];
  });
  return <Facts rows={rows} testId="resource-facts" />;
}

/** The settings of one section this record can't use, with the reasons. Field-level: the rest of the section still edits. */
export function UnsupportedNote({
  fields,
  unsupported,
}: {
  fields: EngineField[];
  unsupported: Record<string, string>;
}) {
  const rows = fields.filter((f) => f.key in unsupported);
  if (!rows.length) return null;
  const reasons = [...new Set(rows.map((f) => unsupported[f.key]))];
  return (
    <Note tone="lock">
      <span data-testid="unsupported-note">
        {reasons.length === 1 ? (
          <>
            {reasons[0]}{" "}
            <span className="text-muted-foreground">
              Not shown: {rows.map((f) => f.label).join(", ")}.
            </span>
          </>
        ) : (
          <ul className="flex flex-col gap-1">
            {rows.map((f) => (
              <li key={f.key}>
                <b className="font-medium">{f.label}</b>: {unsupported[f.key]}
              </li>
            ))}
          </ul>
        )}
      </span>
    </Note>
  );
}

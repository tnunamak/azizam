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
  CheckResult,
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
  "Azizam creates these settings from your public model names and engines. Change those sources to update this record.";
export const UNSUPPORTED_REASON =
  "This page cannot safely edit this saved setting yet.";
export const ARGV_REASON =
  "This page cannot safely edit this server’s process setup yet. Its saved command arguments are shown above.";

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

const mono = (text: string) => <span className="font-mono [overflow-wrap:anywhere]">{text}</span>;
/** Keep unrecognized values readable without inventing semantics. */
function storedText(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value) ?? "Unknown";
}
function SavedFields({ value }: { value: unknown }) {
  if (Array.isArray(value)) return <ul className="flex flex-col gap-1">{value.map((item, i) => <li key={i}><SavedFields value={item} /></li>)}</ul>;
  if (value !== null && typeof value === "object") return <dl className="flex flex-col gap-1">{Object.entries(value).map(([key, item]) => <div key={key}>
    <dt className="font-medium">{key === "required" ? "Required" : key === "type" ? "Value type" : key.replace(/_/g, " ")}</dt>
    <dd className="pl-3"><SavedFields value={item} /></dd>
  </div>)}</dl>;
  return <>{value === true ? "Yes" : value === false ? "No" : value == null ? "Not recorded" : String(value)}</>;
}
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

/** The origin of a URL as the HTTP client reads it: the default port of the scheme is dropped. */
export function originOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

export function credentialOrigin(view: EngineView): string {
  const dest = view.credential_destination;
  if (typeof dest === "string" && dest) return dest;
  if (dest && typeof dest === "object" && dest.origin) return dest.origin;
  return (
    view.v2?.connection.credentials.inference?.origin || view.key.origin || ""
  );
}

/** What the gateway says wants a look on this engine, in its own words. */
export function AttentionNotes({ view }: { view: EngineView }) {
  if (!view.attention?.length) return null;
  return (
    <div className="flex flex-col gap-2" data-testid="engine-attention">
      {view.attention.map((item) => (
        <Note key={item.code + item.message} tone="warn" role="alert">
          {item.message}
        </Note>
      ))}
    </div>
  );
}

/** Connection: the request path and the key's destination, as saved. */
export function ConnectionFacts({
  view,
}: {
  view: EngineView;
  definition: EngineDefinition;
}) {
  const rows: [string, ReactNode][] = [];
  const path = view.request_path;
  rows.push(["Saved request destinations", <span key="destinations">
    {path?.operations?.length ? <ul>{path.operations.map((op, index) => <li key={index}>{op.capability}: {mono(`${op.method} ${op.url}`)}</li>)}</ul>
      : path?.url ? mono(path.url) : "The request destination is unknown. Azizam did not return it."}
    {path?.url && <span className="text-muted-foreground block">
      {path.mode === "gateway_lifecycle" ? "Azizam starts the server when needed, then forwards the request to this address." : "Azizam sends requests directly to this address."}
      {" "}From this engine’s saved connection settings.
    </span>}
    <span className="text-muted-foreground block">Draft edits do not change these destinations until saved.</span>
  </span>]);
  {
    const origin = credentialOrigin(view);
    const requestOrigin = path?.url ? originOf(path.url) : "";
    const inference = view.v2?.connection.credentials.inference;
    const keyConfigured = view.key.state === "configured" || Boolean(inference?.secret_ref || inference?.env);
    const keyHere = keyConfigured && origin;
    const elsewhere = keyHere && requestOrigin && originOf(origin) !== requestOrigin;
    const unavailable = view.attention?.some((item) => item.code === "engine_credential_unavailable");
    rows.push([
      "Inference key is sent to",
      elsewhere ? (
        <span key="dest">
          No inference key is sent: the key belongs to {mono(originOf(origin) || origin)} but this engine’s address is {mono(requestOrigin)}.
        </span>
      ) : keyHere && unavailable ? (
        "No inference key is sent. The configured key is not available."
      ) : !origin && (keyConfigured || inference) ? (
        "The saved key’s destination is unknown."
      ) : keyHere && !requestOrigin ? (
        "Unknown. The request address is unavailable."
      ) : keyHere ? (
        <span key="dest">
          {mono(origin)} only. Azizam never sends it to another address.
        </span>
      ) : (
        "No inference key is configured."
      ),
    ]);
  }
  const control = view.v2?.connection.credentials.control;
  if (control != null) {
    const destination = typeof control === "object" && "origin" in control && typeof control.origin === "string" ? control.origin : "";
    rows.push(["Control key destination", destination ? mono(destination) : "The saved key’s destination is unknown."]);
  }
  if (view.reply_timeout)
    rows.push([
      "Time to wait for a reply",
      view.reply_timeout.seconds == null
        ? `${view.reply_timeout.source}.`
        : `${view.reply_timeout.seconds} seconds. Source: ${view.reply_timeout.source}.`,
    ]);
  const protocol = view.v2?.connection.protocol;
  if (protocol) rows.push(["Protocol", mono(protocol)]);
  if (view.ignored_headers?.length)
    rows.push([
      "Headers not sent",
      `${view.ignored_headers.join(", ")}. These stored headers cannot override credentials or HTTP transport settings. Use the API key field for a credential.`,
    ]);
  return <Facts rows={rows} testId="connection-facts" />;
}

const selectionWords = {
  request_field: "Apps choose this model with the request’s model field.",
  switch: "Azizam switches the engine to this model, then checks the selection.",
  whole_graph: "Each request runs the whole workflow.",
} as const;

/** What it offers: how a request reaches each offering, and which one is selected now. */
export function OfferingFacts({ v2 }: { v2: EngineRecordV2 | undefined }) {
  const offerings = v2?.offerings ?? [];
  if (!offerings.length)
    return (
      <p className="text-meta text-muted-foreground">
        No models or workflows are listed yet.
      </p>
    );
  const switching = offerings.some((o) => o.selection?.mode === "switch");
  return (
    <div className="flex flex-col gap-2" data-testid="offering-facts">
      {switching && (
        <p className="text-meta">
          Selected now:{" "}
          {v2?.selected_offering ? (
            <>{mono(v2.selected_offering)} <span className="text-muted-foreground">
              {v2.observations.selected?.last?.ok === true ? "Verified by a check." : "Not verified by a check."}
              {v2.observations.selected?.last?.at ? <> <RelativeTime ts={v2.observations.selected.last.at} fallback="" />.</> : " Check time unknown."}
              {" "}Source: {v2.observations.selected?.last?.source ?? "saved record"}.
            </span></>
          ) : (
            <span className="text-muted-foreground">
              Unknown. The selected model has not been verified.
            </span>
          )}
        </p>
      )}
      <ul className="bg-card rounded-panel divide-y border">
        {offerings.map((o) => (
          <li key={o.id} className="text-meta min-w-0 flex flex-col gap-0.5 p-3 [overflow-wrap:anywhere]">
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
                ? selectionWords[o.selection.mode] ?? "The saved selection method is not explained."
                : "How a request reaches it is not recorded."}
              {o.selection?.mode === "switch" && o.selection.switch?.call ? (
                <> Operation used to switch models: {mono(o.selection.switch.call)}.</>
              ) : null}
            </span>
            <Facts rows={[
              ...(o.tasks ? [["Tasks and evidence", <ul>{o.tasks.map((task, index) => <li key={index}>{task.task}: {task.evidence === "declared" ? "declared in the saved settings" : task.evidence === "observed" ? "reported by a server check" : task.evidence === "tested" ? "verified by a test" : task.evidence}</li>)}</ul>] as [string, ReactNode]] : []),
              ...(o.required_inputs != null ? [["Required inputs", <SavedFields value={o.required_inputs} />] as [string, ReactNode]] : []),
              ...(o.options != null ? [["Options", <SavedFields value={o.options} />] as [string, ReactNode]] : []),
              ...(o.start_profiles != null ? [["Start settings", <SavedFields value={o.start_profiles} />] as [string, ReactNode]] : []),
              ...(o.adjustments != null ? [["Adjustments", <SavedFields value={o.adjustments} />] as [string, ReactNode]] : []),
              ...(o.inputs != null ? [["Inputs", <SavedFields key="inputs" value={o.inputs} />] as [string, ReactNode]] : []),
              ...(o.kind === "model" ? [["Loading", o.selection?.implicit_load === true ? "Loads when first requested." : o.selection?.implicit_load === false ? "Does not load automatically on first request." : "Whether this model loads on first request is unknown."] as [string, ReactNode]] : []),
            ]} />
            <details><summary className="text-link cursor-pointer max-sm:min-h-11">Technical details</summary>
              <pre className="whitespace-pre-wrap [overflow-wrap:anywhere]">{JSON.stringify(o, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

function processOwner(process: ProcessV2 | null | undefined): string {
  if (!process || process.driver === "none")
    return !process ? "Who runs this server is unknown." : "Not Azizam. Something outside Azizam keeps this server running; Azizam only sends it requests.";
  if (process.driver === "command")
    return "An outside process manager owns this server. Azizam asks it to start and stop using the saved commands.";
  if (process.driver === "process") return "Azizam. It starts the process itself and owns it until it stops.";
  return "Who runs this server is unknown.";
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
  if (apply) {
    rows.push(["Saved file operation", mono(apply.kind)]);
    if (apply.reload?.length) rows.push(["Reload", argv(apply.reload)]);
    if (apply.file) rows.push(["Settings file", <span key="file">
      {mono(apply.file)}
      <span className="text-muted-foreground block">
        The saved process settings choose this path. Moving the file does not update those settings.
        {apply.kind === "write_env_file_then_restart" ? " Azizam writes this environment settings file before restarting." : " The file's role is specified by the saved setting above."}
        {apply.restore === "on_failure" ? " The saved setting calls for restoring it if starting fails." : ""}
      </span>
    </span>]);
  }
  const residency = view.v2?.residency ?? {};
  const idleOwner = residency.idle_owner;
  if (typeof idleOwner === "string")
    rows.push([
      "Stops when idle",
      idleOwner === "gateway"
        ? typeof residency.idle_stop_s === "number" ? `Azizam stops it after ${residency.idle_stop_s} seconds idle.` : "Azizam stops it when idle. The idle delay is unknown."
        : idleOwner === "engine" ? "The engine decides." : "Who stops it when idle is unknown.",
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
          This server uses an older start-and-stop configuration. It is read-only here.
          <details><summary>Technical details</summary>{String(process?.legacy_driver ?? residency.legacy_driver ?? "unknown")}</details>
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
        Asks {mono(`${o.http.method ?? "GET"} ${o.http.path}`)}
        {o.http.expect ? `, expects ${o.http.expect}` : ""}
        {select ? <>, reads {mono(select)}</> : null}.
      </>
    );
  return "A check is configured, but its method is not explained.";
}

/** What Azizam can check: each observation, how it looks and what it saw. Missing is unknown, never green. */
export function ObservationList({ view, latestCheck }: { view: EngineView; latestCheck?: CheckResult | null }) {
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
            view.check.checked_at && view.check.status === "ok"
              ? true
              : view.check.checked_at && view.check.status === "failed"
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
                Saved result:{" "}
                {view.check.status === "ok" ? "reached" : "a problem"},{" "}
                <RelativeTime ts={view.check.checked_at} fallback="" />. Source: {view.check.origin || "saved connection check"}.
              </>
            ) : (
              "Not checked yet."
            )}
          </span>
        </span>
      </li>
      {latestCheck && <li className="text-meta p-3">
        Latest unsaved connection check: {latestCheck.reachable ? "reached the server" : "could not reach the server"}.
        {" "}<RelativeTime ts={latestCheck.checked_at} fallback="" />. Source: {mono(latestCheck.calls)}.
        {" "}This result does not replace the saved results below.
      </li>}
      {rows.map(([key, label, o]) => (
        <li key={key} className="text-meta flex items-start gap-2 p-3">
          <Evidence ok={o?.last?.ok} />
          <span className="min-w-0 [overflow-wrap:anywhere]">
            <span className="block font-medium">{label}</span>
            <span className="text-muted-foreground block">
              {o ? (
                <>
                  {howWords(o)}{" "}
                  {key === "inventory" && (o.means === "loaded" ? "Models loaded in memory. " : o.means === "available" ? "Models available to use. " : "Whether this lists available or loaded models is unknown. ")}

                  {o.last ? (
                    <>
                      Saved result:{" "}
                      {o.last.detail ??
                        (o.last.ok ? "as expected" : "not as expected")}
                      {o.last.at ? (
                        <>
                          , <RelativeTime ts={o.last.at} fallback="" />
                        </>
                      ) : null}
                      . Source: {o.last.source ?? "saved check"}. {o.last.at ? "" : "Check time unknown."}
                    </>
                  ) : (
                    "No result yet, so it is unknown."
                  )}
                </>
              ) : (
                "No check is configured, so this is unknown."
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
        aria-label="Verified by a check"
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
  const labels: Record<string, string> = { gpu: "GPU", cpu: "CPU", memory: "Memory", ram: "Memory", devices: "Devices", vram_gb_estimate: "Estimated video memory (GB)", ram_gb_estimate: "Estimated memory (GB)", cores: "Cores" };
  const label = (key: string) => labels[key] ?? key.replace(/_/g, " ");
  const rows: [string, ReactNode][] = entries.map(([key, value]) => {
    const object = value != null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
    const detail = object ? Object.entries(object).filter(([k]) => k !== "enforced").map(([k, v]) => `${label(k)}: ${storedText(v)}`).join("; ") : storedText(value);
    const enforcement = object?.enforced === true ? "Enforced." : object?.enforced === false ? "Resource estimate only; not enforced." : "Enforcement unknown.";
    return [label(key), `${detail}. ${enforcement}`];
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

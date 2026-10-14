"use client";

/**
 * The engine record page, opened at /engines/?engine=<id>: Connection, What it
 * can do, Models (or Model, or Workflow), More options, the stored record and
 * Remove. Reads GET /admin/api/engines/{id}; saves through the edit route,
 * which re-checks a changed connection on the server.
 */
import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, CircleAlert, CircleCheck, CircleX, Loader2, Lock, RefreshCw } from "lucide-react";
import { api, ApiError, queryKeys, type ServiceRegistry } from "@/lib/api";
import {
  checkFromError,
  enginesApi,
  type CheckResult,
  type EngineDefinition,
  type EngineView,
  type Finding,
  type UpdateEngineBody,
} from "@/lib/engines-api";
import { cn } from "@/lib/cn";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Input } from "@/components/ui/input";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Callout, StateWord } from "@/components/product/status-ui";
import { engineDefinitionsKey } from "../add-engine/add-engine-dialog";
import { checkedWhen, clockTime, fieldOf, hasPath, originOf, trimAddress } from "../add-engine/draft";
import { CallLine } from "../parts/call-line";
import { CapabilityList, rowsFromCheck, type CapabilityRow, type TestState } from "../parts/capabilities";
import { FindingCard } from "../parts/finding-card";
import { FormField, LinkButton, Note, Tag } from "../parts/form";
import { ModelsBlock, draftsFromCheck, duplicateNames, modelsBody, type ModelDraft } from "../parts/models-block";
import { SpeechFields, speechDraft, speechValues, type SpeechDraft } from "../parts/speech-fields";
import { cardProblem, cardsFromTransforms, transformsFromCards, type AdjustmentCard } from "./adjustments";
import {
  MoreOptions,
  changedCount,
  headersBody,
  headersFrom,
  optionFields,
  type HeaderDraft,
  type OptionValues,
} from "./more-options";

export const engineViewKey = (serviceId: string) => ["engineView", serviceId] as const;

const SAVED_TEST_NOTE = "Ran on the gateway’s computer with the saved settings. It is not in Activity.";
const GATEWAY_TEST_NOTE = "Sent through the gateway, the way an app would.";
const DRAFT_TEST_NOTE = "Ran on the gateway’s computer with the values on this screen. Nothing was saved, and it is not in Activity.";
const STORED_TEST_NOTE = "Ran on the gateway’s computer when the engine was added. It is not in Activity.";

export function EngineRecordPage({
  serviceId,
  notice,
  onDismissNotice,
  onBack,
  onRemove,
}: {
  serviceId: string;
  /** "Added …" from the dialog, shown once at the top. */
  notice?: string | null;
  onDismissNotice?: () => void;
  onBack: () => void;
  onRemove: (serviceId: string) => void;
}) {
  const defs = useQuery({ queryKey: engineDefinitionsKey, queryFn: enginesApi.definitions, staleTime: 60_000 });
  const view = useQuery({ queryKey: engineViewKey(serviceId), queryFn: () => enginesApi.view(serviceId) });
  const [version, setVersion] = useState(0);
  const [ownNotice, setOwnNotice] = useState<string | null>(null);

  const crumb = (
    <Button type="button" variant="link" size="sm" className="w-fit px-0" onClick={onBack}>
      <ChevronLeft aria-hidden="true" />
      Engines
    </Button>
  );
  if (view.isPending || defs.isPending)
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        {crumb}
        <Skeleton aria-hidden="true" className="h-10 w-64" />
        <Skeleton aria-hidden="true" className="h-72 w-full" />
      </div>
    );
  const definition = defs.data?.definitions.find((d) => d.definition === view.data?.definition);
  if (view.isError || defs.isError || !view.data || !definition)
    return (
      <div className="flex flex-col gap-4">
        {crumb}
        <Callout
          as="div"
          severity="critical"
          title="This engine could not be loaded"
          meta={
            view.error instanceof ApiError && view.error.status === 404
              ? "The engine is no longer on this gateway. Go back to Engines."
              : "Its settings are unknown. Try loading the engine again."
          }
          actions={
            <Button type="button" variant="outline" size="sm" onClick={() => void view.refetch()}>
              Retry
            </Button>
          }
        />
      </div>
    );

  const shownNotice = ownNotice ?? notice;
  return (
    <div className="flex max-w-(--content-max) flex-col gap-4 pb-24">
      {shownNotice && (
        <div role="status" className="bg-success-tint rounded-control text-meta flex items-start gap-2 px-3 py-2">
          <CircleCheck aria-hidden="true" className="text-success mt-0.5 size-4 shrink-0" />
          <span className="flex-1">{shownNotice}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setOwnNotice(null);
              onDismissNotice?.();
            }}
          >
            Dismiss
          </Button>
        </div>
      )}
      {crumb}
      <RecordForm
        key={version}
        view={view.data}
        definition={definition}
        onRefresh={async () => {
          await view.refetch();
        }}
        onSaved={async (message) => {
          await view.refetch();
          setVersion((v) => v + 1);
          setOwnNotice(message);
          onDismissNotice?.();
        }}
        onDiscard={() => setVersion((v) => v + 1)}
        onRemove={() => onRemove(serviceId)}
      />
    </div>
  );
}

interface SessionCheck {
  result: CheckResult;
  address: string;
  path: string;
  /** The key typed when the check ran; the result says nothing about another key. */
  key: string;
}

function optionValuesFrom(definition: EngineDefinition, view: EngineView): OptionValues {
  const out: OptionValues = {};
  const values = view.values as unknown as Record<string, unknown>;
  for (const field of optionFields(definition)) {
    if (field.type === "keyvalue" || field.items === "transform") continue;
    const value = values[field.key] ?? field.default;
    out[field.key] = value === undefined || value === null ? "" : String(value);
  }
  return out;
}

function storedTests(view: EngineView): Record<string, TestState> {
  return Object.fromEntries(Object.entries(view.tests ?? {}).map(([id, stored]) => [id, { status: "stored", stored } as TestState]));
}

function RecordForm({
  view,
  definition,
  onRefresh,
  onSaved,
  onDiscard,
  onRemove,
}: {
  view: EngineView;
  definition: EngineDefinition;
  onRefresh: () => Promise<void>;
  onSaved: (message: string) => Promise<void>;
  onDiscard: () => void;
  onRemove: () => void;
}) {
  const viewer = useViewer();
  const owner = viewer.role === "owner";
  const queryClient = useQueryClient();
  const flow = definition.flow;
  const withPath = hasPath(definition);
  const saved = { address: view.values.address ?? "", path: view.values.path ?? "" };
  const locked = view.locked;

  const [name, setName] = useState(view.name);
  const [address, setAddress] = useState(saved.address);
  const [path, setPath] = useState(saved.path);
  const [newKey, setNewKey] = useState("");
  const [keyEditing, setKeyEditing] = useState(false);
  const [options, setOptions] = useState<OptionValues>(() => optionValuesFrom(definition, view));
  const [headers, setHeaders] = useState<HeaderDraft[]>(() => headersFrom(view.values.headers));
  const [cards, setCards] = useState<AdjustmentCard[]>(() => cardsFromTransforms(view.values.transforms));
  const [ticked, setTicked] = useState<Set<string>>(() => new Set(view.capabilities));
  const [models, setModels] = useState<ModelDraft[]>(() =>
    view.models.map((m) => ({
      id: m.id,
      public_name: m.public_name,
      context_window: m.context_window == null ? "" : String(m.context_window),
      use: m.use ?? view.check.model_use?.[m.id] ?? "",
      on: m.enabled !== false,
      suggestedName: false,
      reportedWindow: false,
      byName: false,
    })),
  );
  const [speech, setSpeech] = useState<SpeechDraft>(() =>
    speechDraft(definition, {
      public_name: view.values.public_name,
      server_model: view.values.server_model,
      voices: view.values.voices,
      default_voice: view.values.default_voice,
    }),
  );
  const [workflowName, setWorkflowName] = useState(view.values.workflow_public_name ?? view.workflow?.public_name ?? "");
  const [moreOpen, setMoreOpen] = useState(() => changedCount(definition, optionValuesFrom(definition, view), headersFrom(view.values.headers), cardsFromTransforms(view.values.transforms)) > 0);
  const [session, setSession] = useState<SessionCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [detectNote, setDetectNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; finding?: Finding } | null>(null);
  const [enabling, setEnabling] = useState<boolean | null>(null);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [enableWarning, setEnableWarning] = useState<Finding | null>(null);

  function bodyValues(): Record<string, unknown> {
    const keys = new Set(definition.fields.map((f) => f.key));
    const values: Record<string, unknown> = { address: address.trim() };
    if (withPath) values.path = path.trim();
    if (newKey) values.api_key = newKey;
    if (view.key.source === "env" && view.key.env_name && keys.has("api_key_env")) values.api_key_env = view.key.env_name;
    for (const field of optionFields(definition)) {
      // Start and stop is the owner's; anyone else leaves the stored block as it is.
      if (field.owner_only && (!owner || !view.lifecycle_editable || locked)) continue;
      if (field.show_if && options[field.show_if.field] !== String(field.show_if.equals)) continue;
      if (field.type === "keyvalue") values[field.key] = headersBody(headers);
      else if (field.items === "transform") values[field.key] = transformsFromCards(cards);
      else if (field.type === "duration" || field.type === "number") {
        const text = (options[field.key] ?? "").trim();
        values[field.key] = /^-?\d+(\.\d+)?$/.test(text) ? Number(text) : text;
      } else values[field.key] = options[field.key] ?? "";
    }
    if (flow === "declared") Object.assign(values, speechValues(definition, speech));
    if (flow === "workflow" && keys.has("workflow_public_name")) values.workflow_public_name = workflowName.trim();
    return values;
  }
  function saveBody(): UpdateEngineBody {
    const body: UpdateEngineBody = {
      definition: definition.definition,
      name: name.trim(),
      values: bodyValues() as unknown as UpdateEngineBody["values"],
      capabilities: definition.capabilities.filter((c) => ticked.has(c.id)).map((c) => c.id),
    };
    if (flow === "detect") body.models = modelsBody(models);
    return body;
  }
  const [initialBody] = useState(() => JSON.stringify(saveBody()));
  const dirty = Boolean(newKey) || JSON.stringify(saveBody()) !== initialBody;

  const addressChanged = trimAddress(address) !== trimAddress(saved.address) || (withPath && path.trim() !== saved.path.trim());
  const keyStored = view.key.state === "configured";
  // The server withholds a stored key from another origin (resolve_key), and drops it on Save.
  const keyOrigin = view.key.origin || originOf(saved.address);
  const n2 = keyStored && originOf(address) !== keyOrigin;
  const fresh =
    session && session.address === trimAddress(address) && session.path === path.trim() && session.key === newKey ? session : null;
  const freshPassed = Boolean(fresh && fresh.result.outcome !== "error");
  const keyAcceptedHere = Boolean(fresh && freshPassed && fresh.result.key_sent && fresh.result.auth === "accepted");
  const blocked = addressChanged && !freshPassed;
  const notStarted = view.check.status === "not_started" && !session;
  const needKey = view.check.reason === "needkey" && view.key.state === "none";
  const managed = options.lifecycle_mode === "managed";
  const hasLifecycle = definition.fields.some((f) => f.owner_only);

  async function runCheck(detectModels = false) {
    setChecking(true);
    setCheckError(null);
    setDetectNote(null);
    try {
      const result = await enginesApi.check({ definition: definition.definition, values: bodyValues() as never, service_id: view.service_id });
      setSession({ result, address: trimAddress(address), path: path.trim(), key: newKey });
      if (result.outcome !== "error") {
        if (view.check.status === "not_started" && ticked.size === 0) setTicked(new Set(result.capabilities.filter((c) => c.ticked).map((c) => c.id)));
        if (flow === "detect" && (detectModels || models.length === 0)) {
          const known = new Set(models.map((m) => m.id));
          const found = draftsFromCheck(result.models.filter((m) => !known.has(m.id)), models.length === 0);
          setModels([...models, ...found]);
          if (detectModels) setDetectNote(found.length ? `Found ${found.length} new ${found.length === 1 ? "model" : "models"}. Tick the ones to offer.` : "No new models.");
        }
      }
    } catch (error) {
      setCheckError(error instanceof Error ? error.message : "The check could not run.");
    } finally {
      setChecking(false);
    }
  }

  async function runTest(capability: string) {
    setTests((t) => ({ ...t, [capability]: { status: "running" } }));
    const label = definition.capabilities.find((c) => c.id === capability)?.label.toLowerCase() ?? "";
    const uses = new Map((session?.result.models ?? []).map((m) => [m.id, m.use]));
    const offered = models.filter((m) => m.on);
    const model =
      flow === "detect"
        ? (offered.find((m) => (m.use || uses.get(m.id) || "").toLowerCase().includes(label)) ?? offered[0])?.id
        : undefined;
    try {
      const result = await enginesApi.test({
        definition: definition.definition,
        values: bodyValues() as never,
        capability,
        ...(model ? { model } : {}),
        service_id: view.service_id,
      });
      setTests((t) => ({ ...t, [capability]: { status: "done", result } }));
      // A pass is stored only when it ran with the saved connection.
      if (result.ok && !locked && !addressChanged && !newKey) {
        const registry = await enginesApi.update(view.service_id, {
          definition: definition.definition,
          tests: { [capability]: { ok: true, at: result.at, seconds: result.seconds } },
        });
        queryClient.setQueryData(queryKeys.serviceRegistry, registry);
      }
    } catch (error) {
      setTests((t) => ({ ...t, [capability]: { status: "error", message: error instanceof Error ? error.message : "" } }));
    }
  }

  async function setEnabled(on: boolean, acknowledge = false) {
    setEnabling(on);
    setEnableError(null);
    setEnableWarning(null);
    try {
      let registry: ServiceRegistry;
      if (locked) registry = await api.services.edit(view.service_id, { enabled: on });
      else
        registry = await enginesApi.update(
          view.service_id,
          on && view.check.status === "not_started"
            ? { definition: definition.definition, enabled: true, recheck: true, ...(acknowledge ? { acknowledge_warnings: true } : {}) }
            : { definition: definition.definition, enabled: on },
        );
      queryClient.setQueryData(queryKeys.serviceRegistry, registry);
      await onRefresh();
    } catch (error) {
      const refused = checkFromError(error);
      if (refused) {
        setSession({ result: refused, address: trimAddress(address), path: path.trim(), key: newKey });
        const warning = refused.findings.find((f) => f.severity === "warning");
        if (warning && !refused.findings.some((f) => f.severity === "error")) setEnableWarning(warning);
      }
      else setEnableError(errorText(error));
    } finally {
      setEnabling(null);
    }
  }

  async function save(options: { keepOff?: boolean; acknowledge?: boolean } = {}) {
    setSaving(true);
    setSaveError(null);
    try {
      let registry: ServiceRegistry;
      if (locked) registry = await api.services.edit(view.service_id, { name: name.trim() });
      else {
        const body = saveBody();
        if (options.keepOff) body.disabled = true;
        if (options.acknowledge) body.acknowledge_warnings = true;
        registry = await enginesApi.update(view.service_id, body);
      }
      queryClient.setQueryData(queryKeys.serviceRegistry, registry);
      await onSaved(
        options.keepOff
          ? `Saved. “${name.trim()}” is off, and Azizam has not checked the new address. ${checkedWhen(managed)}`
          : "Saved.",
      );
    } catch (error) {
      const refused = checkFromError(error);
      const finding = refused?.findings.find((f) => f.severity === "error") ?? refused?.findings[0];
      setSaveError({ message: errorText(error), finding });
    } finally {
      setSaving(false);
    }
  }

  // ---- What it can do
  const rows: CapabilityRow[] = (() => {
    const fromSession = session?.result.capabilities;
    if (fromSession?.length) return rowsFromCheck(fromSession);
    const stored = view.check.capabilities;
    if (stored?.length && !notStarted)
      return definition.capabilities.map((c) => {
        const row = stored.find((x) => x.id === c.id);
        return row
          ? { id: c.id, label: c.label, level: row.level, evidence: row.evidence, why: row.why }
          : { id: c.id, label: c.label, level: "unchecked" as const };
      });
    return definition.capabilities.map((c) => {
      const test = view.tests?.[c.id];
      if (notStarted || !ticked.has(c.id)) return { id: c.id, label: c.label, level: "unchecked" as const };
      if (test?.ok)
        return { id: c.id, label: c.label, level: "likely" as const, evidence: `A real test passed at ${clockTime(test.at)}. That shows the server can do this.` };
      return {
        id: c.id,
        label: c.label,
        level: "declared" as const,
        evidence: "Turned on for this engine. Check again to see what the server says now.",
      };
    });
  })().map((row) => {
    const t = tests[row.id];
    return t?.status === "done" && t.result.ok && row.level !== "declared"
      ? { ...row, level: "likely" as const, evidence: `A real test passed at ${clockTime(t.result.at)}. That shows the server can do this.`, why: undefined }
      : row;
  });
  const shownTests = { ...storedTests(view), ...tests };
  const via = Object.values(tests).find((t) => t.status === "done");
  const testNote =
    via?.status === "done"
      ? (via.result.via as string) === "gateway"
        ? GATEWAY_TEST_NOTE
        : via.result.via === "saved"
          ? SAVED_TEST_NOTE
          : DRAFT_TEST_NOTE
      : SAVED_TEST_NOTE;

  // ---- The check line
  const detail = (count: number) => (flow === "declared" ? "The path answered. " : flow === "detect" ? `${count} models. ` : "");
  let checkLine: ReactNode;
  let checkButton = "Check again";
  if (checking) checkLine = <><Loader2 aria-hidden="true" className="size-4 animate-spin" /> Checking…</>;
  else if (fresh && fresh.result.outcome === "error") {
    const f = fresh.result.findings[0];
    checkLine = (
      <span className="text-danger">
        <CircleX aria-hidden="true" className="mr-1 inline size-4" />
        {f ? `The check found a problem: ${f.title}.` : "The check found a problem."}
        {addressChanged ? " Fix it and check again, or save and keep the engine off." : ""}
      </span>
    );
  } else if (fresh) {
    const r = fresh.result;
    const keyWords = addressChanged
      ? keyAcceptedHere
        ? "Key accepted. "
        : keyStored || r.key_withheld
          ? "No key was sent. "
          : "No key needed. "
      : r.key_sent && r.auth === "accepted"
        ? "Key accepted. "
        : "No key needed. ";
    checkLine = (
      <>
        <CircleCheck aria-hidden="true" className="text-success mr-1 inline size-4" />
        {addressChanged ? "Reached the server at the new address. " : "Reached the server. "}
        {keyWords}
        {detail(r.models.length)}Checked just now.
      </>
    );
  } else if (addressChanged) {
    checkLine = (
      <>
        <CircleAlert aria-hidden="true" className="text-warning mr-1 inline size-4" />
        <b className="font-medium">Out of date. Check again.</b>{" "}
        {view.check.status === "ok" && (
          <span className="text-muted-foreground line-through decoration-1">
            Last result, for the old address: reached the server. {detail(view.check.model_count ?? 0)}
            {view.check.checked_at ? (
              <>
                Checked <RelativeTime ts={view.check.checked_at} fallback="" />.
              </>
            ) : null}
          </span>
        )}
      </>
    );
  } else if (view.check.status === "ok") {
    checkLine = (
      <>
        <CircleCheck aria-hidden="true" className="text-success mr-1 inline size-4" />
        Reached the server. {view.check.key_sent ? "Key accepted. " : "No key needed. "}
        {detail(view.check.model_count ?? 0)}
        {view.check.checked_at ? (
          <>
            Checked <RelativeTime ts={view.check.checked_at} fallback="" />.
          </>
        ) : null}
      </>
    );
  } else if (view.check.status === "not_started") {
    checkButton = "Check now";
    checkLine = (
      <>
        Not checked yet.{" "}
        {view.check.reason === "needkey" ? "The server needs a key, and the owner adds it first." : checkedWhen(managed).replace("It checks", "Azizam checks")}
      </>
    );
  } else if (view.check.status === "failed") checkLine = <>The last check found a problem. Check again to see it.</>;
  else checkLine = <>Not checked from this page yet.</>;
  const checkFinding = fresh && fresh.result.outcome === "error" ? fresh.result.findings[0] : undefined;

  // ---- Key
  let keyArea: ReactNode;
  if (n2) {
    keyArea = owner ? (
      <div className="flex flex-col gap-1.5">
        <p className={cn("text-meta", keyAcceptedHere ? "text-success" : "text-muted-foreground")}>
          {keyAcceptedHere ? "Key accepted at the new address." : "Key not sent to this address yet. Re-enter it to check."}
        </p>
        <Input
          type="password"
          aria-labelledby="engine-key-label"
          autoComplete="new-password"
          placeholder="Paste the key again"
          value={newKey}
          onChange={(event) => setNewKey(event.target.value)}
        />
        <p className="text-meta text-muted-foreground">
          Leave it empty if the server at the new address has no key. Azizam checks it when you press Check again.
        </p>
      </div>
    ) : (
      <div className="flex flex-wrap items-center gap-2">
        <Tag tone="changed">
          <Lock aria-hidden="true" className="size-3" />
          Not sent to the new address
        </Tag>
        <span className="text-meta text-muted-foreground">Only the owner can enter the key again.</span>
      </div>
    );
  } else if (keyEditing && owner) {
    keyArea = (
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <Input
            type="password"
            aria-labelledby="engine-key-label"
            autoComplete="new-password"
            placeholder="Paste the key"
            value={newKey}
            onChange={(event) => setNewKey(event.target.value)}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setKeyEditing(false);
              setNewKey("");
            }}
          >
            Cancel
          </Button>
        </div>
        <p className="text-meta text-muted-foreground">Azizam checks the key against the address when you save.</p>
      </div>
    );
  } else if (needKey) {
    keyArea = (
      <div className="flex flex-wrap items-center gap-2">
        <Tag tone="changed">No key yet</Tag>
        {owner ? (
          <Button type="button" variant="outline" size="sm" onClick={() => setKeyEditing(true)}>
            Add the key
          </Button>
        ) : (
          <span className="text-meta text-muted-foreground">The server needs one. Only the owner can add it.</span>
        )}
      </div>
    );
  } else if (!keyStored) {
    keyArea = (
      <div className="flex flex-wrap items-center gap-2">
        <Tag>No key</Tag>
        {owner ? (
          <Button type="button" variant="outline" size="sm" disabled={locked} onClick={() => setKeyEditing(true)}>
            Add a key
          </Button>
        ) : (
          <span className="text-meta text-muted-foreground">Only the owner can add a key.</span>
        )}
      </div>
    );
  } else {
    keyArea = (
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-badge text-badge bg-success-tint text-success inline-flex h-5 items-center px-1.5">Configured</span>
        {view.key.source === "env" && view.key.env_name && (
          <span className="text-meta text-muted-foreground">
            Read from <span className="font-mono">{view.key.env_name}</span> on the gateway’s computer.
          </span>
        )}
        {owner ? (
          <Button type="button" variant="outline" size="sm" disabled={locked} onClick={() => setKeyEditing(true)}>
            Replace key
          </Button>
        ) : (
          <span className="text-meta text-muted-foreground">Only the owner can replace the key.</span>
        )}
      </div>
    );
  }

  const modelsTitle = flow === "workflow" ? "Workflow" : flow === "declared" ? (fieldOf(definition, "voices") ? "Model and voices" : "Model") : "Models";
  const jumpModels = flow === "workflow" ? "Workflow" : flow === "declared" ? "Model" : "Models";
  const enabledNow = enabling ?? view.enabled;
  const offWord = !view.enabled && notStarted;
  const sessionFailed = !view.enabled && view.check.status === "not_started" && session?.result.outcome === "error";
  const otherSettings = Object.entries(view.other_settings ?? {});
  const switchDisabled = enabling !== null || (needKey && !owner);
  const workflowField = fieldOf(definition, "workflow_public_name");

  return (
    <>
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-title [overflow-wrap:anywhere]">{view.name}</h1>
          <div className="text-meta text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            {view.enabled ? <StateWord tone="ok" word="Ready" /> : <StateWord tone="off" word="Off" />}
            {offWord ? (
              <span>
                {view.check.reason === "needkey"
                  ? "Waiting for the owner to add a key"
                  : managed
                    ? "Not started; checked on first start"
                    : "Not checked yet; checked when you turn it on or press Check now"}
              </span>
            ) : sessionFailed ? (
              <span>The check found a problem. See Connection.</span>
            ) : view.check.checked_at ? (
              <span>
                Last checked <RelativeTime ts={view.check.checked_at} fallback="" />
              </span>
            ) : null}
          </div>
        </div>
        <label className="text-meta flex min-h-8 items-center gap-3 max-sm:min-h-11">
          <Switch aria-label="Enabled" checked={enabledNow} disabled={switchDisabled} onCheckedChange={(on) => void setEnabled(on)} />
          Enabled
        </label>
      </header>
      {enableError && <Callout as="div" severity="critical" title="Enabled could not change" meta={enableError} />}
      {enableWarning && (
        <FindingCard
          finding={enableWarning}
          extraActions={
            <Button type="button" variant="outline" size="sm" onClick={() => void setEnabled(true, true)}>
              Turn on anyway
            </Button>
          }
        />
      )}
      {locked && <Note tone="lock">Azizam runs this engine itself. Only its name and Enabled can change.</Note>}
      {notStarted && hasLifecycle && (
        <Note tone="info">
          {owner ? (
            <>
              If Azizam should start and stop this server itself, add the commands under More options, in Start and stop.
              <br />
              <LinkButton
                onClick={() => {
                  setMoreOpen(true);
                  requestAnimationFrame(() => document.getElementById("engine-group-start-and-stop")?.scrollIntoView({ block: "center" }));
                }}
              >
                Open Start and stop
              </LinkButton>
            </>
          ) : (
            "If Azizam should start this server itself, the owner adds the start and stop commands under More options."
          )}
        </Note>
      )}
      <nav aria-label="On this page" className="text-meta flex flex-wrap gap-x-4 gap-y-1">
        <a className="text-link hover:underline" href="#engine-connection">Connection</a>
        <a className="text-link hover:underline" href="#engine-abilities">What it can do</a>
        <a className="text-link hover:underline" href="#engine-models">{jumpModels}</a>
        <a className="text-link hover:underline" href="#engine-more">More options</a>
      </nav>

      <section aria-labelledby="engine-connection" className="flex flex-col gap-2">
        <h2 id="engine-connection" className="text-heading scroll-mt-20">Connection</h2>
        <div className="bg-card rounded-panel border">
          <div className="flex flex-col gap-4 p-4">
            <FormField label="Name">
              {(control) => <Input {...control} value={name} autoComplete="off" onChange={(event) => setName(event.target.value)} />}
            </FormField>
            <fieldset disabled={locked} className="flex min-w-0 flex-col gap-4">
              <FormField
                label="Address"
                help={fieldOf(definition, "address")?.help}
                after={
                  <>
                    {!withPath && <CallLine definition={definition} values={{ address, path_prefix: options.path_prefix }} />}
                    {n2 && (
                      <div role="alert" className="border-warning/40 bg-warning-tint/50 rounded-control flex flex-col gap-2 border p-3">
                        <p className="text-meta flex items-start gap-2 font-medium">
                          <CircleAlert aria-hidden="true" className="text-warning mt-0.5 size-4 shrink-0" />
                          <span>
                            {owner
                              ? "Your saved key won’t be sent to a new address. Re-enter it, or keep the old address."
                              : "Your saved key won’t be sent to a new address. Only the owner can enter it again. Keep the old address, or ask the owner."}
                          </span>
                        </p>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="w-fit"
                          onClick={() => {
                            setAddress(saved.address);
                            setPath(saved.path);
                            setNewKey("");
                            setSession(null);
                          }}
                        >
                          Keep the old address
                        </Button>
                      </div>
                    )}
                  </>
                }
              >
                {(control) => (
                  <Input {...control} type="url" value={address} autoComplete="off" spellCheck={false} onChange={(event) => {
                      setAddress(event.target.value);
                      setTests({});
                    }} />
                )}
              </FormField>
              {withPath && (
                <FormField
                  label={fieldOf(definition, "path")?.label ?? "Path on the server"}
                  help="The part after the address, starting with /."
                  after={<CallLine definition={definition} values={{ address, path }} />}
                >
                  {(control) => (
                    <Input {...control} className="font-mono" value={path} autoComplete="off" spellCheck={false} onChange={(event) => {
                      setPath(event.target.value);
                      setTests({});
                    }} />
                  )}
                </FormField>
              )}
              {fieldOf(definition, "api_key") && (
                <div role="group" aria-labelledby="engine-key-label" className="flex flex-col gap-1.5">
                  <span id="engine-key-label" className="text-meta font-medium">
                    {fieldOf(definition, "api_key")?.label}
                  </span>
                  {keyArea}
                </div>
              )}
            </fieldset>
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t px-4 py-3">
            <p data-testid="check-line" role="status" className="text-meta min-w-0 flex-1">
              {checkLine}
            </p>
            <Button
              type="button"
              variant={blocked ? "default" : "outline"}
              size="sm"
              disabled={checking || locked || (needKey && !owner && view.check.status === "not_started")}
              onClick={() => void runCheck()}
            >
              <RefreshCw aria-hidden="true" />
              {checkButton}
            </Button>
          </div>
          {(checkFinding || checkError) && (
            <div className="px-4 pb-4">
              {checkFinding ? <FindingCard finding={checkFinding} onRecheck={() => void runCheck()} /> : <Note tone="warn" role="alert">{`Azizam could not run the check. ${checkError}`}</Note>}
            </div>
          )}
        </div>
      </section>

      <fieldset disabled={locked} className="flex min-w-0 flex-col gap-4">
        <section aria-labelledby="engine-abilities" className="flex flex-col gap-2">
          <div>
            <h2 id="engine-abilities" className="text-heading scroll-mt-20">What it can do</h2>
            <p className="text-meta text-muted-foreground">
              {notStarted
                ? "Not known yet. Azizam looks the first time the engine starts, or when you press Check now."
                : "Tick what you want to use. A real test proves it."}
            </p>
          </div>
          <CapabilityList
            rows={rows}
            ticked={ticked}
            onTick={(id, on) =>
              setTicked((s) => {
                const n = new Set(s);
                if (on) n.add(id);
                else n.delete(id);
                return n;
              })
            }
            tests={shownTests}
            onTest={locked ? undefined : (id) => void runTest(id)}
            testNote={testNote}
            storedNote={STORED_TEST_NOTE}
            readOnly={locked}
          />
        </section>

        <section aria-labelledby="engine-models" className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id="engine-models" className="text-heading scroll-mt-20">{modelsTitle}</h2>
            {flow === "detect" && (
              <Button type="button" variant="outline" size="sm" disabled={checking} onClick={() => void runCheck(true)}>
                <RefreshCw aria-hidden="true" />
                Detect models again
              </Button>
            )}
          </div>
          {detectNote && <p role="status" className="text-meta text-muted-foreground">{detectNote}</p>}
          {flow === "detect" && (
            <ModelsBlock
              models={models}
              onChange={setModels}
              readOnly={locked}
              emptyText="No models yet. Azizam lists them the first time the engine starts. You can also add one by name below."
            />
          )}
          {flow === "declared" && (
            <div className="bg-card rounded-panel border p-4">
              <SpeechFields definition={definition} draft={speech} onChange={setSpeech} showSuggested={false} readOnly={locked} />
            </div>
          )}
          {flow === "workflow" && (
            <div className="bg-card rounded-panel flex flex-col gap-4 border p-4">
              {view.workflow ? (
                <>
                  <div className="flex items-start gap-2">
                    <CircleCheck aria-hidden="true" className="text-success mt-0.5 size-4 shrink-0" />
                    <span className="min-w-0">
                      <span className="text-meta block font-mono [overflow-wrap:anywhere]">{view.workflow.file}</span>
                      <span className="text-meta text-muted-foreground block">
                        {view.workflow.node_count} nodes. Makes {view.workflow.makes === "video" ? "video" : "images"}.
                      </span>
                    </span>
                  </div>
                  {workflowField && (
                    <FormField label={workflowField.label} required help={workflowField.help} error={workflowName.trim() ? undefined : "Give the workflow the name apps use."}>
                      {(control) => (
                        <Input {...control} value={workflowName} autoComplete="off" spellCheck={false} onChange={(event) => setWorkflowName(event.target.value)} />
                      )}
                    </FormField>
                  )}
                  <div>
                    <h3 className="text-meta mb-2 font-medium">Which field gets which input</h3>
                    <dl className="text-meta grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                      {(definition.workflow_inputs ?? []).map((input) => {
                        const target = view.workflow?.inputs?.[input.key];
                        const [node, field] = String(target ?? "").split(".");
                        return (
                          <div key={input.key} className="contents">
                            <dt className="text-muted-foreground">{input.label}</dt>
                            <dd className="font-mono">{target ? `Node ${node} · ${field}` : "Not used"}</dd>
                          </div>
                        );
                      })}
                    </dl>
                  </div>
                </>
              ) : (
                <p className="text-meta text-muted-foreground">No workflow yet. Choose one before you turn this engine on.</p>
              )}
            </div>
          )}
        </section>

        <section aria-label="More options">
          <MoreOptions
            definition={definition}
            values={options}
            onValues={(patch) => setOptions((o) => ({ ...o, ...patch }))}
            headers={headers}
            onHeaders={setHeaders}
            cards={cards}
            onCards={setCards}
            owner={owner}
            lifecycleEditable={view.lifecycle_editable}
            locked={locked}
            address={address}
            open={moreOpen}
            onOpenChange={setMoreOpen}
          />
        </section>
      </fieldset>

      <section aria-labelledby="engine-stored" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="engine-stored" className="text-heading">Stored record</h2>
          <CopyButton value={JSON.stringify(view.record, null, 2)} aria-label="Copy the stored record">
            Copy
          </CopyButton>
        </div>
        <p className="text-meta text-muted-foreground">Exactly what the gateway saved. You cannot edit it here. The key is never shown.</p>
        <div className="bg-card rounded-panel flex flex-col gap-4 border p-4">
          <pre
            tabIndex={0}
            aria-label="Stored record, read only"
            className="bg-muted rounded-control text-meta max-h-96 overflow-auto p-3 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]"
          >
            {JSON.stringify(view.record, null, 2)}
          </pre>
          {otherSettings.length > 0 && (
            <div>
              <h3 className="text-meta mb-1 font-medium">Other settings</h3>
              <p className="text-meta text-muted-foreground mb-2">
                Azizam kept these values, but this form has no field for them. They are not lost.
              </p>
              <dl className="text-meta grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                {otherSettings.map(([key, value]) => (
                  <div key={key} className="contents">
                    <dt className="font-mono">{key}</dt>
                    <dd className="[overflow-wrap:anywhere]">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>
      </section>

      {!locked && (
        <section aria-labelledby="engine-remove" className="bg-card rounded-panel flex flex-wrap items-center justify-between gap-3 border p-4">
          <div>
            <h2 id="engine-remove" className="text-heading">Remove this engine</h2>
            <p className="text-meta text-muted-foreground">Apps lose its models at once. You can add it again later.</p>
          </div>
          <Button type="button" variant="destructive" size="sm" onClick={onRemove}>
            Remove engine
          </Button>
        </section>
      )}

      {saveError && (
        <div className="flex flex-col gap-2">
          <Callout as="div" severity="critical" title="The changes could not be saved" meta={saveError.message} />
          {saveError.finding && (
            <FindingCard
              finding={saveError.finding}
              extraActions={
                saveError.finding.severity === "warning" && (
                  <Button type="button" variant="outline" size="sm" onClick={() => void save({ acknowledge: true })}>
                    Save anyway
                  </Button>
                )
              }
            />
          )}
        </div>
      )}

      {(dirty || addressChanged) && (
        <div role="status" className="bg-card rounded-panel shadow-overlay sticky bottom-4 z-10 flex flex-wrap items-center justify-between gap-3 border px-4 py-3">
          <span className="text-meta">
            {blocked ? "The address changed. Check it again before you save, or save and keep the engine off." : "You have unsaved changes."}
            {n2 && !newKey ? " Saving removes the old key from this engine." : ""}
            {originOf(address) !== originOf(saved.address) && headers.some((h) => h.stored)
              ? " Saving also removes the stored secret header values. Type them again for the new address."
              : ""}
          </span>
          <span className="flex flex-wrap gap-2">
            <Button type="button" variant="ghost" size="md" onClick={onDiscard}>
              Discard
            </Button>
            {blocked && (
              <Button type="button" variant="outline" size="md" disabled={saving} onClick={() => void save({ keepOff: true })}>
                Save and keep off
              </Button>
            )}
            <Button
              type="button"
              size="md"
              disabled={blocked || saving || !name.trim() || cards.some((c) => cardProblem(c) !== null) || duplicateNames(models).size > 0 || (flow === "workflow" && Boolean(workflowField) && !workflowName.trim() && Boolean(view.workflow))}
              onClick={() => void save()}
            >
              {saving && <Loader2 aria-hidden="true" className="animate-spin" />}
              Save changes
            </Button>
          </span>
        </div>
      )}
    </>
  );
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412) return "Engine settings changed elsewhere. Refresh and try again.";
    if (error.code === "owner_required") return error.message;
    return error.message;
  }
  return error instanceof Error ? error.message : "The gateway did not answer. Try again.";
}

"use client";

/**
 * The engine record page, opened at /engines/?engine=<id>. It mirrors the
 * engine record: Connection, What it offers, How it runs, What Azizam can
 * check, Resources and the stored record, then Remove. Reads GET
 * /admin/api/engines/{id}; saves through the edit route, which re-checks a
 * changed connection on the server. Every field it shows as editable is one
 * the gateway uses; the rest show read-only with the reason.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, CircleAlert, CircleCheck, CircleX, Loader2, RefreshCw } from "lucide-react";
import { api, ApiError, queryKeys, type ServiceRegistry } from "@/lib/api";
import {
  checkFromError,
  enginesApi,
  fieldFromError,
  type CheckResult,
  type EngineDefinition,
  type EngineView,
  type Finding,
  type FindingAction,
  type UpdateEngineBody,
} from "@/lib/engines-api";
import { ProtocolChoice, type ProtocolDraft } from "../parts/protocol-choice";
import { SecretFields } from "./secret-fields";
import { secretDrafts, secretCommands, secretProblem, endpointProblem } from "./secret-edits";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Callout, StateWord } from "@/components/product/status-ui";
import { engineDefinitionsKey } from "../add-engine/add-engine-dialog";
import { checkedWhen, fieldOf, originOf, trimAddress } from "../add-engine/draft";
import { protocolDescriptionsApi } from "@/lib/protocol-descriptions-api";
import { AccessAnswer, AccessAsk, accessModels } from "../parts/access-answer";
import { CallLine } from "../parts/call-line";
import { CapabilityList, rowsFromCheck, type CapabilityRow, type TestState } from "../parts/capabilities";
import { FindingCard } from "../parts/finding-card";
import { FormField, LinkButton, Note, describedBy, focusFirstInvalid } from "../parts/form";
import { ModelsBlock, draftsFromCheck, duplicateNames, modelsBody, type ModelDraft } from "../parts/models-block";
import {
  ARGV_REASON,
  AttentionNotes,
  ConnectionFacts,
  DERIVED_REASON,
  LockedNote,
  ObservationList,
  OfferingFacts,
  ProcessFacts,
  ResourceFacts,
  UNSUPPORTED_REASON,
  UnsupportedNote,
  fieldPart,
  lockReason,
} from "./contract-sections";
import { LongAudioControls } from "./long-audio-controls";
import { LongAudioSection, RemoveSection, StoredRecordSection, errorText } from "./record-sections";
import { RetentionLedgerPanel } from "@/components/product/retention-ledger";
import { cardProblem, cardsFromTransforms, transformsFromCards, type AdjustmentCard } from "./adjustments";
import {
  FieldGroups,
  MoreOptions,
  changedCount,
  headersFrom,
  optionFields,
  type HeaderDraft,
  type OptionValues,
} from "./more-options";

export const engineViewKey = (serviceId: string) => ["engineView", serviceId] as const;

/** Section links: 44 px tall on a phone, with room to tap. */
const NAV_LINK = "text-link inline-flex items-center px-2 hover:underline max-sm:min-h-11";

const SAVED_TEST_NOTE = "Tested the saved settings from the gateway’s computer. This test is not listed in Activity.";
const DRAFT_TEST_NOTE = "Tested your unsaved changes from the gateway’s computer. The test did not save your changes and is not listed in Activity.";
const STORED_TEST_NOTE = "A previous test passed using the settings saved at that time. It is not listed in Activity.";

export function EngineRecordPage({
  serviceId,
  notice,
  onDismissNotice,
  onBack,
  onRemove,
  onOpenEngine,
}: {
  serviceId: string;
  /** "Added …" from the dialog, shown once at the top. */
  notice?: string | null;
  onDismissNotice?: () => void;
  onBack: () => void;
  onRemove: (serviceId: string) => void;
  /** Opens another engine, for a finding that says an engine already uses this address. */
  onOpenEngine?: (serviceId: string) => void;
}) {
  const {
    data: definitions,
    isPending: definitionsPending,
    isError: definitionsFailed,
    refetch: refetchDefinitions,
  } = useQuery({ queryKey: engineDefinitionsKey, queryFn: enginesApi.definitions, staleTime: 60_000 });
  const {
    data: engine,
    isPending: enginePending,
    isError: engineFailed,
    error: engineError,
    refetch: refetchEngine,
  } = useQuery({ queryKey: engineViewKey(serviceId), queryFn: () => enginesApi.view(serviceId) });
  const [version, setVersion] = useState(0);
  const [ownNotice, setOwnNotice] = useState<string | null>(null);

  const crumb = (
    <Button type="button" variant="link" size="sm" className="w-fit px-0" onClick={onBack}>
      <ChevronLeft aria-hidden="true" />
      Engines
    </Button>
  );
  if (enginePending || definitionsPending)
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        {crumb}
        <Skeleton aria-hidden="true" className="h-10 w-64" />
        <Skeleton aria-hidden="true" className="h-72 w-full" />
      </div>
    );
  const definition = definitions?.definitions.find((d) => d.definition === engine?.definition);
  if (engineFailed || definitionsFailed || !engine || !definition)
    return (
      <div className="flex flex-col gap-4">
        {crumb}
        <Callout
          as="div"
          severity="critical"
          title="This engine could not be loaded"
          meta={
            engineError instanceof ApiError && engineError.status === 404
              ? "The engine is no longer on this gateway. Go back to Engines."
              : "Azizam couldn’t load this engine’s settings. Try again."
          }
          actions={
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                // Ask again for whatever failed. When both loaded but the kind is unknown, ask for both.
                const both = !engineFailed && !definitionsFailed;
                if (engineFailed || both) void refetchEngine();
                if (definitionsFailed || both) void refetchDefinitions();
              }}
            >
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
          view={engine}
          definition={definition}
          onRefresh={async () => {
            const refreshed = await refetchEngine();
            return refreshed.isSuccess ? refreshed.data?.revision : undefined;
          }}
          onSaved={async (message) => {
            await refetchEngine();
            setVersion((v) => v + 1);
            setOwnNotice(message);
            onDismissNotice?.();
          }}
          onDiscard={async () => {
            const refreshed = await refetchEngine();
            if (refreshed.isSuccess) setVersion((v) => v + 1);
          }}
          onRemove={() => onRemove(serviceId)}
          onOpenEngine={onOpenEngine}
        />
    </div>
  );
}

/** A refused Save: what the server said, the check behind it, and the field it is about. */
interface SaveError {
  message: string;
  stale?: boolean;
  finding?: Finding;
  /** The form field the message belongs to: name, address, key, models or one of More options' keys. */
  field: string | null;
  /** The form as it was saved. The message shows only while the form still matches it. */
  body: string;
}

/** The server calls the key api_key or api_key_env; the page has one place for it. */
function saveField(raw: string | null): string | null {
  return raw === "api_key" || raw === "api_key_env" ? "key" : raw;
}

interface SessionCheck {
  result: CheckResult;
  address: string;
  snapshot?: string;
}

function optionValuesFrom(definition: EngineDefinition, view: EngineView): OptionValues {
  const out: OptionValues = {};
  const values = view.values as unknown as Record<string, unknown>;
  for (const field of optionFields(definition)) {
    if (field.type === "keyvalue" || field.items === "transform") continue;
    const value = values[field.key] ?? field.default;
    out[field.key] = Array.isArray(value) ? [...value] : value === undefined || value === null ? "" : String(value);
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
  onOpenEngine,
}: {
  view: EngineView;
  definition: EngineDefinition;
  onRefresh: () => Promise<string | undefined>;
  onSaved: (message: string) => Promise<void>;
  onDiscard: () => Promise<void>;
  onRemove: () => void;
  onOpenEngine?: (serviceId: string) => void;
}) {
  const viewer = useViewer();
  const owner = viewer.role === "owner";
  const queryClient = useQueryClient();
  const saved = { address: view.values.address ?? "" };
  const locked = view.locked;
  // Why the engine's settings can't change for this person: the system record, or not the owner.
  // Name and Enabled still change.
  const reason = lockReason(view, owner);
  const settingsLocked = reason !== null;
  // Settings of the definition this record can't use (its protocol differs): never a control, never sent.
  const unsupported = view.unsupported_fields ?? {};
  const addressUnsupported = "address" in unsupported;
  const [protocol, setProtocol] = useState<ProtocolDraft>(() => ({ dispatch: view.values.dispatch ?? "", protocol: view.values.protocol_description ?? "" }));
  const nativeMode = protocol.dispatch === "protocol";
  const { data: protocolCatalog, isSuccess: protocolCatalogReady, isFetching: protocolCatalogFetching, isError: protocolCatalogError } = useQuery({
    queryKey: ["engineProtocolDescriptions"],
    queryFn: protocolDescriptionsApi.list,
    staleTime: 0,
    enabled: nativeMode,
  });
  const selectedProtocol = protocolCatalog?.protocols.find((row) => row.id === protocol.protocol);
  const protocolValid = !nativeMode || Boolean(protocolCatalogReady && !protocolCatalogFetching && !protocolCatalogError && selectedProtocol?.dispatch_supported && selectedProtocol.validation.valid);
  const protocolDormantFields = nativeMode ? new Set(["headers", "path_prefix", "reply_timeout_s", "transforms"]) : null;
  const mismatched = addressUnsupported;
  // Name and Enabled go through the plain service edit, which sends no definition and no values:
  // a full engine payload would carry settings this person or this record can't change.
  const metadataOnly = settingsLocked || mismatched || nativeMode;
  const allOptions = optionFields(definition).filter((f) => !(f.key in unsupported) && !protocolDormantFields?.has(f.key));
  const connectionFields = allOptions.filter((f) => fieldPart(f) === "connection" && f.type !== "keyvalue");
  const offeringFields = allOptions.filter((f) => fieldPart(f) === "offerings");
  const processFields = allOptions.filter((f) => fieldPart(f) === "process" && !["lifecycle_start", "lifecycle_stop"].includes(f.key) && (f.key !== "lifecycle_mode" || view.values.lifecycle_mode === "managed"));
  const offeringsEditable = view.can_edit_offerings !== false;
  const fieldsOf = (part: ReturnType<typeof fieldPart>) => definition.fields.filter((f) => fieldPart(f) === part);

  const [name, setName] = useState(view.name);
  const [address, setAddress] = useState(saved.address);
  // Background refetches do not authorize an old secret draft against a newer registry.
  const [draftRevision, setDraftRevision] = useState(view.revision);
  async function refreshAfterOwnWrite() {
    // A background refresh may have changed the record without rebuilding this draft.
    // In that case, retain its old revision so Save still gets a 412.
    const currentRevision = queryClient.getQueryData<EngineView>(engineViewKey(view.service_id))?.revision;
    const refreshedRevision = await onRefresh();
    if (refreshedRevision !== undefined && currentRevision === draftRevision) setDraftRevision(refreshedRevision);
  }
  const [options, setOptions] = useState<OptionValues>(() => optionValuesFrom(definition, view));
  const [headers, setHeaders] = useState<HeaderDraft[]>([]);
  const [secrets, setSecrets] = useState(() => secretDrafts(view.record, view.key));
  const commands = secretCommands(secrets);
  const commandProblem = secretProblem(secrets);
  const addressProblem = endpointProblem(address);
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
  const [moreOpen, setMoreOpen] = useState(
    () =>
      changedCount(
        definition,
        optionValuesFrom(definition, view),
        headersFrom(view.values.headers),
        cardsFromTransforms(view.values.transforms),
        connectionFields,
      ) > 0,
  );
  const [session, setSession] = useState<SessionCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [detectNote, setDetectNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<SaveError | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const checkText = useId();
  const saveText = useId();
  const [enabling, setEnabling] = useState<boolean | null>(null);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [enableWarning, setEnableWarning] = useState<Finding | null>(null);

  function bodyValues(at = address): Record<string, unknown> {
    const values: Record<string, unknown> = { address: at.trim() };
    if (nativeMode || protocol.dispatch !== (view.values.dispatch ?? "") || protocol.protocol !== (view.values.protocol_description ?? "")) { values.dispatch = protocol.dispatch; values.protocol_description = protocol.protocol; }
    for (const field of optionFields(definition)) {
      if (field.key in unsupported) continue;
      if (protocolDormantFields?.has(field.key)) continue;
      if (!offeringsEditable && fieldPart(field) === "offerings") continue;
      // Start and stop is the owner's; anyone else leaves the stored block as it is.
      if (field.owner_only && (!owner || !view.lifecycle_editable || locked)) continue;
      if (field.show_if && options[field.show_if.field] !== String(field.show_if.equals)) continue;
      if (field.type === "keyvalue" || ["lifecycle_start", "lifecycle_stop"].includes(field.key)) continue;
      else if (field.items === "transform") values[field.key] = transformsFromCards(cards);
      else if (field.type === "duration" || field.type === "number") {
        const text = String(options[field.key] ?? "").trim();
        values[field.key] = /^-?\d+(\.\d+)?$/.test(text) ? Number(text) : text;
      } else values[field.key] = options[field.key] ?? "";
    }
    return values;
  }
  function saveBody(): UpdateEngineBody {
    const body: UpdateEngineBody = {
      definition: definition.definition,
      name: name.trim(),
      values: bodyValues() as unknown as UpdateEngineBody["values"],
    };
    if ("address" in unsupported) delete (body.values as unknown as Record<string, unknown>).address;
    if (offeringsEditable) {
      body.capabilities = definition.capabilities.filter((c) => ticked.has(c.id)).map((c) => c.id);
      body.models = modelsBody(models);
    }
    return body;
  }
  const [initialBody] = useState(() => JSON.stringify(saveBody()));
  const bodyNow = JSON.stringify(saveBody());
  const publicDirty = bodyNow !== initialBody;
  const configurationSnapshot = JSON.stringify({ ...saveBody(), name: undefined, secretCommands: commands });
  const [initialConfiguration] = useState(configurationSnapshot);
  const currentTests = Object.fromEntries(Object.entries(tests).filter(([, t]) => !("snapshot" in t) || t.snapshot === configurationSnapshot));
  // Without the right to change settings, only the name can be unsaved.
  const dirty = settingsLocked ? name.trim() !== view.name : commands.length > 0 || bodyNow !== initialBody;
  // Only the name changed: it saves through the plain service edit when the record needs that.
  const nameOnly = commands.length === 0 && JSON.stringify({ ...saveBody(), name: view.name }) === initialBody;
  const optionKeys = new Set(optionFields(definition).map((f) => f.key));
  // A refusal belongs to the form that was sent. Once the owner edits it, the message steps aside.
  const refusal = saveError && saveError.body === JSON.stringify({ body: saveBody(), commands }) ? saveError : null;
  const refusedField = refusal?.field ?? null;
  const refusedFinding = refusal?.finding;
  const findingInline = Boolean(refusedFinding && (refusedField === "address" || refusedField === "key"));
  const refusedHere = (field: string) => (refusedField === field ? refusal?.message : undefined);
  const optionErrors: Record<string, string> = refusedField && optionKeys.has(refusedField) && refusal ? { [refusedField]: refusal.message } : {};
  useEffect(() => {
    if (saveError) focusFirstInvalid(root.current);
  }, [saveError]);

  const addressChanged = trimAddress(address) !== trimAddress(saved.address);
  const protocolChanged = protocol.dispatch !== (view.values.dispatch ?? "") || protocol.protocol !== (view.values.protocol_description ?? "");
  const connectionChanged = addressChanged || protocolChanged;
  const keyStored = view.key.state === "configured";
  // The server withholds a stored key from another origin (resolve_key), and drops it on Save.
  const keyOrigin = view.key.origin || originOf(saved.address);
  const n2 = keyStored && originOf(address) !== keyOrigin;
  const fresh = session && session.address === trimAddress(address) && (!session.snapshot || session.snapshot === JSON.stringify({ values: bodyValues(), commands })) ? session : null;
  const freshPassed = Boolean(fresh && fresh.result.outcome !== "error");
  const blocked = connectionChanged && !freshPassed;
  const notStarted = view.check.status === "not_started" && !session;
  const needKey = view.check.reason === "needkey" && view.key.state === "none";
  const managed = options.lifecycle_mode === "managed";
  const hasLifecycle = processFields.length > 0 && view.lifecycle_editable;
  const storedArgv = Boolean(view.v2?.process?.start?.length || view.v2?.process?.stop?.length);
  const processReason = unsupported.lifecycle_mode ?? (storedArgv ? ARGV_REASON : UNSUPPORTED_REASON);

  async function runCheck(detectModels = false, at = address) {
    if (addressProblem) { setCheckError(addressProblem); return; }
    if (commands.length) { setCheckError("Save the secret changes before checking this engine."); return; }
    setChecking(true);
    setCheckError(null);
    setDetectNote(null);
    try {
      const result = await enginesApi.check({ definition: definition.definition, values: bodyValues(at) as never, service_id: view.service_id });
      setSession({ result, address: trimAddress(at), snapshot: JSON.stringify({ values: bodyValues(at), commands }) });
      // A check never changes what someone who can't edit the settings sees saved.
      if (result.outcome !== "error" && !settingsLocked) {
        if (view.check.status === "not_started" && ticked.size === 0) setTicked(new Set(result.capabilities.filter((c) => c.ticked).map((c) => c.id)));
        if (detectModels || models.length === 0) {
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
    if (addressProblem) { setCheckError(addressProblem); return; }
    if (commands.length) { setCheckError("Save the secret changes before running a test."); return; }
    const snapshot = configurationSnapshot;
    const values = bodyValues();
    setTests((t) => ({ ...t, [capability]: { status: "running", snapshot } }));
    const label = definition.capabilities.find((c) => c.id === capability)?.label.toLowerCase() ?? "";
    const uses = new Map((session?.result.models ?? []).map((m) => [m.id, m.use]));
    const offered = models.filter((m) => m.on);
    const model = (offered.find((m) => (m.use || uses.get(m.id) || "").toLowerCase().includes(label)) ?? offered[0])?.id;
    try {
      const result = await enginesApi.test({
        definition: definition.definition,
        values: values as never,
        capability,
        ...(offeringsEditable ? { models: modelsBody(models) } : {}),
        ...(model ? { model } : {}),
        service_id: view.service_id,
      });
      setTests((t) => ({ ...t, [capability]: { status: "done", result, snapshot } }));
      // A pass is stored only when it ran with the saved connection.
      if (result.ok && result.via === "saved" && result.evidence_token && !metadataOnly) {
        // Keeping the pass is a convenience: the test itself passed even if this is refused.
        try {
          const registry = await enginesApi.update(view.service_id, {
            definition: definition.definition,
            tests: { [capability]: { ok: true, at: result.at, seconds: result.seconds, evidence_token: result.evidence_token } },
          });
          queryClient.setQueryData(queryKeys.serviceRegistry, registry);
          await refreshAfterOwnWrite();
        } catch {
          // The page still shows the pass; it is not stored.
        }
      }
    } catch (error) {
      setTests((t) => ({ ...t, [capability]: { status: "error", message: error instanceof Error ? error.message : "", snapshot } }));
    }
  }

  async function setEnabled(on: boolean, acknowledge = false) {
    setEnabling(on);
    setEnableError(null);
    setEnableWarning(null);
    try {
      let registry: ServiceRegistry;
      if (metadataOnly) {
        // Read-only settings still need the existing check before first use.
        // Check the saved connection without rebuilding any owner fields.
        if (on && !locked && !mismatched && !nativeMode && view.check.status === "not_started") {
          const result = await enginesApi.check({ definition: definition.definition, service_id: view.service_id, values: bodyValues() as never });
          const warning = result.findings.find((f) => f.severity === "warning");
          if (result.outcome === "error" || (warning && !acknowledge)) {
            setSession({ result, address: trimAddress(address) });
            if (warning && result.outcome !== "error") setEnableWarning(warning);
            else setEnableError(result.findings[0]?.what ?? "The check found a problem.");
            return;
          }
        }
        registry = await api.services.edit(view.service_id, { enabled: on });
      }
      else
        registry = await enginesApi.update(
          view.service_id,
          on && view.check.status === "not_started"
            ? { definition: definition.definition, enabled: true, recheck: true, ...(acknowledge ? { acknowledge_warnings: true } : {}) }
            : { definition: definition.definition, enabled: on },
        );
      queryClient.setQueryData(queryKeys.serviceRegistry, registry);
      await refreshAfterOwnWrite();
    } catch (error) {
      const refused = checkFromError(error);
      if (refused) {
        setSession({ result: refused, address: trimAddress(address) });
        const warning = refused.findings.find((f) => f.severity === "warning");
        if (warning && !refused.findings.some((f) => f.severity === "error")) setEnableWarning(warning);
      }
      else setEnableError(errorText(error));
    } finally {
      setEnabling(null);
    }
  }

  /** "Add /v1 and check again": change the address as the finding says, then check it. */
  function fixPath(action: FindingAction) {
    let next = trimAddress(address);
    if (action.address_append) next += action.address_append;
    if (action.address_strip && next.endsWith(action.address_strip)) next = next.slice(0, -action.address_strip.length);
    setAddress(next);
    setTests({});
    void runCheck(false, next);
  }

  async function save(options: { keepOff?: boolean; acknowledge?: boolean } = {}) {
    setSaving(true);
    setSaveError(null);
    try {
      let registry: ServiceRegistry;
      if (commandProblem || addressProblem) throw new Error(commandProblem || addressProblem || "");
      if (commands.length && publicDirty) throw new Error("Save secret changes separately from other settings. Save or discard one group first.");
      if (commands.length) registry = await enginesApi.secretEntries(view.service_id, commands, draftRevision);
      else if (settingsLocked || nameOnly) registry = await api.services.edit(view.service_id, { name: name.trim() });
      else {
        const body = saveBody();
        if (options.keepOff) body.disabled = true;
        if (options.acknowledge) body.acknowledge_warnings = true;
        registry = await enginesApi.update(view.service_id, body);

      }
      queryClient.setQueryData(queryKeys.serviceRegistry, registry);
      await onSaved(
          options.keepOff
          ? `Saved. “${name.trim()}” is off. ${protocolChanged ? "Protocol dispatch has not been checked from this page." : "Azizam has not checked the new address."}`
          : "Saved.",
      );
    } catch (error) {
      const refused = checkFromError(error);
      const finding = refused?.findings.find((f) => f.severity === "error") ?? refused?.findings[0];
      const field = saveField(fieldFromError(error) ?? finding?.field ?? null);
      setSaveError({ message: errorText(error), finding, field, body: JSON.stringify({ body: saveBody(), commands }), stale: error instanceof ApiError && error.status === 412 });
      // Show the field the server named: open what hides it, and let the effect below move focus.
      if (field && connectionFields.some((f) => f.key === field)) setMoreOpen(true);
    } finally {
      setSaving(false);
    }
  }

  // ---- What it can do
  const rows: CapabilityRow[] = (() => {
    if (nativeMode)
      return definition.capabilities.map((c) => ticked.has(c.id)
        ? { id: c.id, label: c.label, level: "declared" as const, evidence: "Declared for this engine. This page has not checked a dispatched request." }
        : { id: c.id, label: c.label, level: "unchecked" as const });
    const fromSession = fresh?.result.capabilities;
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
        return { id: c.id, label: c.label, level: "likely" as const, evidence: `A previous test of ${c.label.toLowerCase()}${test.model ? ` with ${test.model}` : ""} passed at ${new Date(test.at * 1000).toLocaleString()} using the settings saved at that time.` };
      return {
        id: c.id,
        label: c.label,
        level: "declared" as const,
        evidence: "Turned on for this engine. Check again to see what the server says now.",
      };
    });
  })().map((row) => {
    if (nativeMode) return row;
    const t = currentTests[row.id];
    return t?.status === "done" && t.result.ok && row.level !== "declared"
      ? { ...row, level: "likely" as const, evidence: `A test of ${row.label.toLowerCase()}${t.result.model ? ` with ${t.result.model}` : ""} passed at ${new Date(t.result.at * 1000).toLocaleString()} using ${t.result.via === "saved" ? "saved" : "unsaved"} settings.`, why: undefined }
      : row;
  });
  const shownTests = nativeMode ? {} : { ...(configurationSnapshot === initialConfiguration ? storedTests(view) : {}), ...currentTests };
  const via = Object.values(currentTests).find((t) => t.status === "done");
  const testNote = nativeMode
    ? "Native protocol dispatch is not tested from this page."
    :
    via?.status === "done"
      ? via.result.via === "saved"
          ? SAVED_TEST_NOTE
          : DRAFT_TEST_NOTE
      : configurationSnapshot === initialConfiguration ? SAVED_TEST_NOTE : DRAFT_TEST_NOTE;

  // ---- The check line
  const detail = (count: number) => `${count} ${count === 1 ? "model" : "models"} found. `;
  let checkLine: ReactNode;
  let checkButton = "Check again";
  if (nativeMode) checkLine = "Protocol dispatch is selected. Static validation only; live request behavior is unknown.";
  else if (checking) checkLine = <><Loader2 aria-hidden="true" className="size-4 animate-spin" /> Checking…</>;
  else if (fresh && fresh.result.outcome === "error") {
    const f = fresh.result.findings[0];
    checkLine = (
      <span className="text-danger">
        <CircleX aria-hidden="true" className="mr-1 inline size-4" />
        {f ? `The check found a problem: ${f.title}.` : "The check found a problem."}
        {addressChanged ? " Fix it and check again, or save and keep the engine off." : ""}
      </span>
    );
  } else if (fresh && fresh.result.outcome === "warning") {
    checkLine = (
      <span className="text-warning">
        <CircleAlert aria-hidden="true" className="mr-1 inline size-4" />
        Reached the server, but the check found a warning. Review it below before saving.
      </span>
    );
  } else if (fresh) {
    const r = fresh.result;
    const keyWords = r.key_sent
      ? r.auth === "accepted" ? "A key was sent and accepted. " : "A key was sent. Acceptance was not established. "
      : "No key was sent for this check. ";
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
        Reached the server. {view.check.key_sent ? "A key was sent. Acceptance was not recorded. " : "No key was sent for this check. "}
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
  const checkFinding =
    fresh?.result.outcome === "error"
      ? fresh.result.findings[0]
      : fresh?.result.outcome === "warning"
        ? (fresh.result.findings.find((f) => f.severity === "warning") ?? fresh.result.findings[0])
        : undefined;
  const checkFindingFor = (field: Finding["field"]) => checkFinding?.field === field && checkFinding.severity === "error";
  /** What the key box says is wrong: a refused Save, or a check that found the key at fault. */
  const saveCard = (inline: boolean) =>
    refusedFinding && (inline ? findingInline : !findingInline) ? (
      <FindingCard
        finding={refusedFinding}
        textId={inline ? saveText : undefined}
        onFixPath={fixPath}
        onOpenExisting={onOpenEngine}
        extraActions={
          refusedFinding.severity === "warning" && (
            <Button type="button" variant="outline" size="sm" onClick={() => void save({ acknowledge: true })}>
              Save anyway
            </Button>
          )
        }
      />
    ) : null;

  const keyArea = <div className="text-meta flex flex-col gap-1">
    <span>{keyStored ? "Configured" : "No key configured"}</span>
    {view.key.source === "env" && view.key.env_name && <span>Read from <code>{view.key.env_name}</code> on the gateway’s computer.</span>}
    {n2 && <p>The configured key belongs to the previous address. Save the address before replacing the key for this origin.</p>}
  </div>;

  const capabilityIds = definition.capabilities.filter((c) => ticked.has(c.id)).map((c) => c.id);
  const modelNames = models.filter((m) => m.on).map((m) => m.public_name.trim() || m.id);
  const savedCapabilities = definition.capabilities.filter((c) => view.capabilities.includes(c.id)).map((c) => c.id);
  const savedNames = view.models.filter((m) => m.enabled !== false).map((m) => m.public_name.trim() || m.id);
  const sameList = (a: string[], b: string[]) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
  const modelsChanged = !sameList(capabilityIds, savedCapabilities) || !sameList(modelNames, savedNames);
  const enabledNow = enabling ?? view.enabled;
  const offWord = !view.enabled && notStarted;
  const sessionFailed = !view.enabled && view.check.status === "not_started" && session?.result.outcome === "error";
  const switchDisabled = enabling !== null || saving || dirty || (needKey && !owner);

  return (
    <div ref={root} className="contents">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-title [overflow-wrap:anywhere]">{view.name}</h1>
          <div className="text-meta text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            {view.enabled ? <StateWord tone="off" word="On" /> : <StateWord tone="off" word="Off" />}
            <span data-testid="engine-health">{nativeMode ? "Protocol dispatch; runtime status is unknown" : fresh ? fresh.result.outcome === "error" ? "Latest check failed" : fresh.result.outcome === "warning" ? "Latest check has a warning" : "Latest connection check passed" : view.check.checked_at && view.check.status === "ok" ? "Saved connection check passed" : view.check.checked_at && view.check.status === "failed" ? "Saved check failed" : "Unknown: no connection check result"}</span>
              {nativeMode ? (
                <span>{protocolValid ? "Installed protocol description is valid. Requests are not checked from this page." : "Choose a valid installed protocol description before saving."}</span>
              ) : offWord ? (
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
      {enableError && <Callout as="div" severity="critical" title={`Couldn’t turn this engine ${enabledNow ? "off" : "on"}.`} meta={enableError} />}
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
      {locked && <LockedNote lead="Azizam runs this engine itself. Only its name and Enabled can change." reason={DERIVED_REASON} view={view} />}
      {notStarted && hasLifecycle && secrets.some(d => d.kind === "argv") && (
        <Note tone="info">
          {owner ? (
            <>
              If Azizam should start and stop this server itself, add the commands under How it runs, in Start and stop.
              <br />
              <LinkButton
                onClick={() => {
                  requestAnimationFrame(() => document.getElementById("engine-group-start-and-stop")?.scrollIntoView({ block: "center" }));
                }}
              >
                Open Start and stop
              </LinkButton>
            </>
          ) : (
            "If Azizam should start this server itself, the owner adds the start and stop commands under How it runs."
          )}
        </Note>
      )}
      <nav aria-label="On this page" className="text-meta -mx-2 flex flex-wrap gap-x-2 gap-y-1">
        <a className={NAV_LINK} href="#engine-connection">Connection</a>
        <a className={NAV_LINK} href="#engine-offers">What it offers</a>
        <a className={NAV_LINK} href="#engine-runs">How it runs</a>
        <a className={NAV_LINK} href="#engine-checks">What Azizam can check</a>
        <a className={NAV_LINK} href="#engine-resources">Resources</a>
        <a className={NAV_LINK} href="#engine-retention">Who could see a request</a>
        <a className={NAV_LINK} href="#engine-stored">Stored record</a>
      </nav>

      <section aria-labelledby="engine-connection" className="flex flex-col gap-2">
        <h2 id="engine-connection" className="text-heading scroll-mt-20">Connection</h2>
        {reason && !locked && <LockedNote reason={reason} view={view} />}
        <div className="bg-card rounded-panel border">
          <div className="flex flex-col gap-4 p-4">
            <FormField label="Name" error={refusedHere("name")}>
              {(control) => <Input {...control} value={name} disabled={commands.length > 0 || saving} autoComplete="off" onChange={(event) => setName(event.target.value)} />}
            </FormField>
            <fieldset disabled={settingsLocked || commands.length > 0 || saving} className="flex min-w-0 flex-col gap-4">
              {"address" in unsupported ? (
                <div className="flex flex-col gap-1.5">
                  <span className="text-meta font-medium">Address</span>
                  <p data-testid="address-read-only" className="text-meta font-mono [overflow-wrap:anywhere]">
                    {view.v2?.connection.address || saved.address}
                  </p>
                </div>
              ) : (
              <FormField
                label="Address"
                help={fieldOf(definition, "address")?.help}
                error={refusedFinding ? undefined : refusedHere("address")}
                extraDescribedBy={describedBy(
                  findingInline && refusedField === "address" && saveText,
                  checkFinding?.field === "address" && checkText,
                )}
                after={
                  <>
                    {!nativeMode && <CallLine definition={definition} values={{ address, path_prefix: typeof options.path_prefix === "string" ? options.path_prefix : undefined }} />}
                    {refusedField === "address" && saveCard(true)}
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
                  <Input
                    {...control}
                    aria-invalid={
                      control["aria-invalid"] ??
                      ((findingInline && refusedField === "address" && refusedFinding?.severity === "error") || checkFindingFor("address")
                        ? true
                        : undefined)
                    }
                    type="url"
                    disabled={commands.length > 0}
                    value={address}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => {
                      setAddress(event.target.value);
                      setTests({});
                    }}
                  />
                )}
              </FormField>
              )}
              {fieldOf(definition, "api_key") && !("api_key" in unsupported) && (
                <div role="group" aria-label="Saved key" className="flex flex-col gap-1.5">
                  <span id="engine-key-label" className="text-meta font-medium">
                    Saved key
                  </span>
                  {keyArea}
                  {refusedField === "key" &&
                    (refusedFinding ? (
                      saveCard(true)
                    ) : (
                      <p id="engine-key-error" className="text-meta text-danger">
                        {refusal?.message}
                      </p>
                    ))}
                </div>
              )}
            </fieldset>
            <AttentionNotes view={view} />
            <ConnectionFacts view={view} definition={definition} />
            <UnsupportedNote fields={fieldsOf("connection")} unsupported={unsupported} />
          </div>
          {nativeMode ? (
            <p data-testid="check-line" role="status" className="text-meta border-t px-4 py-3">{checkLine}</p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-3 border-t px-4 py-3">
                <p data-testid="check-line" role="status" className="text-meta min-w-0 flex-1">{checkLine}</p>
                <Button
                  type="button"
                  variant={blocked ? "default" : "outline"}
                  size="sm"
                  disabled={checking || locked || mismatched || (needKey && !owner && view.check.status === "not_started")}
                  onClick={() => void runCheck()}
                >
                  <RefreshCw aria-hidden="true" />
                  {checkButton}
                </Button>
              </div>
              {mismatched && <p className="text-meta text-muted-foreground px-4 pb-3">This protocol cannot be checked from this page yet.</p>}
              {(checkFinding || checkError) && (
            <div className="px-4 pb-4">
              {checkFinding ? (
                <FindingCard
                  finding={checkFinding}
                  textId={checkText}
                  onRecheck={() => void runCheck()}
                  onFixPath={fixPath}
                  onOpenExisting={onOpenEngine}
                />
              ) : <Note tone="warn" role="alert">{`Azizam could not run the check. ${checkError}`}</Note>}
            </div>
              )}
            </>
          )}
        </div>
      </section>

      {publicDirty && <p className="text-meta text-muted-foreground">Save or discard other settings before editing secrets. Address changes must be saved before a key can be bound to the new origin.</p>}
      {owner && (view.capabilities.includes("audio.transcriptions") || (Array.isArray(view.record.capabilities) && view.record.capabilities.includes("transcriptions"))) && <LongAudioControls serviceId={view.service_id} locked={locked || dirty || saving} />}

      {commands.length > 0 && <p className="text-meta text-muted-foreground">Save or discard the secret changes before changing other settings. The secret batch is saved together.</p>}
      <SecretFields drafts={secrets} onChange={setSecrets} part="connection" excludeKinds={nativeMode ? ["headers"] : []} disabled={settingsLocked || publicDirty || saving} />

      <div className="bg-card rounded-panel border p-4"><ProtocolChoice value={protocol} onChange={setProtocol} disabled={settingsLocked || commands.length > 0 || saving} /></div>

      <section aria-label="More options">
        <MoreOptions
          definition={definition}
          fields={connectionFields}
          values={options}
          onValues={(patch) => setOptions((o) => ({ ...o, ...patch }))}
          headers={headers}
          onHeaders={setHeaders}
          cards={cards}
          onCards={setCards}
          owner={owner}
          lifecycleEditable={view.lifecycle_editable}
          locked={settingsLocked || commands.length > 0 || saving}
          address={address}
          open={moreOpen}
          onOpenChange={setMoreOpen}
          errors={optionErrors}
        />
      </section>

      <section aria-labelledby="engine-offers" className="flex flex-col gap-3">
        <h2 id="engine-offers" className="text-heading scroll-mt-20">What it offers</h2>
        {reason && !locked && <LockedNote reason={reason} view={view} />}
        <UnsupportedNote fields={fieldsOf("offerings")} unsupported={unsupported} />
        {!offeringsEditable && !fieldsOf("offerings").some((f) => f.key in unsupported) && <Note tone="lock">{view.offering_edit_reason ?? UNSUPPORTED_REASON}</Note>}
        <div className="flex min-w-0 flex-col gap-4">
          {offeringsEditable ? (
            <>
              <div className="flex flex-col gap-2">
                <div>
                  <h3 id="engine-abilities" className="text-body scroll-mt-20 font-medium">What it can do</h3>
                  <p className="text-meta text-muted-foreground">
                    {nativeMode
                      ? "Capabilities shown here are declared only. Protocol requests are not tested from this page."
                      : notStarted
                      ? "Not known yet. Azizam looks the first time the engine starts, or when you press Check now."
                      : settingsLocked
                        ? "What is turned on. Run a test to check one request with the selected model."
                        : "Tick what you want to use. Run a test to check one request with the selected model."}
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
                  onTest={nativeMode || locked || mismatched ? undefined : (id) => void runTest(id)}
                  testNote={testNote}
                  storedNote={STORED_TEST_NOTE}
                  readOnly={settingsLocked || commands.length > 0 || saving}
                />
              </div>

              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 id="engine-models" className="text-body scroll-mt-20 font-medium">Models</h3>
                  {!settingsLocked && !nativeMode && (
                    <Button type="button" variant="outline" size="sm" disabled={checking} onClick={() => void runCheck(true)}>
                      <RefreshCw aria-hidden="true" />
                      Find new models
                    </Button>
                  )}
                </div>
                {detectNote && <p role="status" className="text-meta text-muted-foreground">{detectNote}</p>}
                <ModelsBlock
                  models={models}
                  onChange={setModels}
                  readOnly={settingsLocked || commands.length > 0 || saving}
                  error={refusedHere("models")}
                  emptyText={nativeMode ? "No models are listed yet. Add a model using the public name the server accepts." : "No models are listed yet. Check the server to find them, or add a model by the name the server uses."}
                />
                {modelsChanged ? (
                  <AccessAsk
                    capabilities={capabilityIds}
                    models={accessModels(models, definition.capabilities)}
                    debounceMs={400}
                    qualifier="After you save"
                    errorText="Azizam couldn’t read the access plans. Try again or open Access plans."
                  />
                ) : (
                  <AccessAnswer access={view.access} />
                )}
              </div>
            </>
          ) : (
            <><AccessAnswer access={view.access} workflows /><p className="text-meta text-muted-foreground">This page cannot run a sample request for this saved workflow or selection setup yet.</p></>
          )}

          <div className="flex flex-col gap-2">
            <h3 className="text-body font-medium">How a request reaches each one</h3>
            <OfferingFacts v2={view.v2} />
          </div>

          {offeringFields.length > 0 && (
            <div className="bg-card rounded-panel border">
              <FieldGroups
                definition={definition}
                fields={offeringFields}
                values={options}
                onValues={(patch) => setOptions((o) => ({ ...o, ...patch }))}
                headers={headers}
                onHeaders={setHeaders}
                cards={cards}
                onCards={setCards}
                owner={owner}
                lifecycleEditable={view.lifecycle_editable}
                locked={settingsLocked || !offeringsEditable || commands.length > 0 || saving}
                address={address}
                errors={optionErrors}
              />
            </div>
          )}
        </div>
      </section>

      <section aria-labelledby="engine-runs" className="flex flex-col gap-2">
        <h2 id="engine-runs" className="text-heading scroll-mt-20">How it runs</h2>
        {reason && !locked && <LockedNote reason={reason} view={view} />}
        <div className="bg-card rounded-panel flex flex-col gap-3 border p-4">
          <ProcessFacts
            view={view}
            lifecycleEditable={view.lifecycle_editable && !locked}
            note={owner && !locked && !view.lifecycle_editable ? secrets.some(d => d.kind === "argv" || d.kind === "env") ? "Command arguments and environment values can change below. Other process settings are read-only here." : processReason : undefined}
          />
          {view.lifecycle_editable && <UnsupportedNote fields={fieldsOf("process")} unsupported={unsupported} />}
        </div>
        {view.values.lifecycle_mode !== "managed" && !secrets.some(d => d.kind === "argv") && <Note>No process is configured. This page can edit existing process settings; it cannot create process control.</Note>}
        {/* Start and stop edits only the kinds this page can save; others show above as stored. */}
        {processFields.length > 0 && !locked && view.lifecycle_editable && (
          <div className="bg-card rounded-panel border">
            <FieldGroups
              definition={definition}
              fields={processFields}
              values={options}
              onValues={(patch) => setOptions((o) => ({ ...o, ...patch }))}
              headers={headers}
              onHeaders={setHeaders}
              cards={cards}
              onCards={setCards}
              owner={owner}
              lifecycleEditable={view.lifecycle_editable}
              locked={locked || commands.length > 0 || saving}
              address={address}
              errors={optionErrors}
            />
          </div>
        )}
        <SecretFields drafts={secrets} onChange={setSecrets} part="process" disabled={settingsLocked || publicDirty || saving} />
      </section>

      <section aria-labelledby="engine-checks" className="flex flex-col gap-2">
        <h2 id="engine-checks" className="text-heading scroll-mt-20">What Azizam can check</h2>
        <p className="text-meta text-muted-foreground">
          {nativeMode
            ? "This page does not check requests dispatched by an installed protocol description. The remaining entries are saved driver observations."
            : "What Azizam can see for itself. Anything it can’t see shows as unknown, never as fine."}
        </p>
        {reason && !locked ? <LockedNote reason={reason} view={view} /> : !locked && <Note tone="lock">These checks are read-only here. This page has no editor for their settings.</Note>}
        <ObservationList view={view} latestCheck={nativeMode ? undefined : fresh?.result} hideConnectionCheck={nativeMode} />
      </section>

      <section aria-labelledby="engine-resources" className="flex flex-col gap-2">
        <h2 id="engine-resources" className="text-heading scroll-mt-20">Resources</h2>
        {reason && !locked ? <LockedNote reason={reason} view={view} /> : !locked && <Note tone="lock">Resource settings are read-only here. This page has no editor for them.</Note>}
        <div className="bg-card rounded-panel border p-4">
          <ResourceFacts v2={view.v2} />
        </div>
      </section>

      {!owner && <LongAudioSection chunking={view.stt_chunking} />}

      <RetentionLedgerPanel id="engine-retention" endpoint={`/admin/api/engines/${encodeURIComponent(view.service_id)}/retention/ledger`} />

      <StoredRecordSection view={view} />

      {!locked && <RemoveSection onRemove={onRemove} />}

      {refusal && (
        <div className="flex flex-col gap-2">
          <Callout as="div" severity="critical" title="The changes could not be saved" meta={refusal.message} actions={refusal.stale ? <Button type="button" variant="outline" size="sm" onClick={() => void onDiscard()}>Refresh engine settings</Button> : undefined} />
          {saveCard(false)}
        </div>
      )}

      {addressProblem && <p role="alert" className="text-meta text-danger">{addressProblem}</p>}
      {commandProblem && <p role="alert" className="text-meta text-danger">{commandProblem}</p>}
      {(dirty || addressChanged || protocolChanged) && (
        <div role="status" className="bg-card rounded-panel shadow-overlay sm:sticky sm:bottom-4 z-10 flex flex-wrap items-center justify-between gap-3 border px-4 py-3">
          <span className="text-meta">
            {blocked
              ? nativeMode
                ? "Protocol dispatch cannot be checked from this page. Save the connection and keep the engine off."
                : protocolChanged
                  ? "The connection changed. Check it again before you save, or save and keep the engine off."
                  : "The address changed. Check it again before you save, or save and keep the engine off."
              : "You have unsaved changes."}
            {n2 ? " Saving removes the old key from this engine." : ""}
          </span>
          <span className="flex flex-wrap gap-2">
            <Button type="button" variant="ghost" size="md" onClick={onDiscard}>
              Discard
            </Button>
            {blocked && (
              <Button type="button" variant="outline" size="md" disabled={saving || !protocolValid || !!commandProblem || !!addressProblem} onClick={() => void save({ keepOff: true })}>
                Save and keep off
              </Button>
            )}
            <Button
              type="button"
              size="md"
              disabled={!!commandProblem || !!addressProblem || !protocolValid || blocked || saving || !name.trim() || cards.some((c) => cardProblem(c) !== null) || duplicateNames(models).size > 0}
              onClick={() => void save()}
            >
              {saving && <Loader2 aria-hidden="true" className="animate-spin" />}
              Save changes
            </Button>
          </span>
        </div>
      )}
    </div>
  );
}

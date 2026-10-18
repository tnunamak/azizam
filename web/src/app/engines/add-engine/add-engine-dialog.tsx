"use client";

/**
 * Add engine: a five-step dialog driven by the server's engine definitions.
 * Kind, address and key, what it can do, models, then name and create. Every
 * field label, help line, check row and capability comes from the definition.
 * The last step also says who will be able to use the models.
 */
import { createContext, useContext, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, Circle, Loader2 } from "lucide-react";
import { ApiError, queryKeys, type ServiceRegistry } from "@/lib/api";
import {
  checkFromError,
  enginesApi,
  fieldFromError,
  type CheckResult,
  type CreateEngineBody,
  type EngineDefinition,
  type EngineValues,
  type Finding,
  type FindingAction,
} from "@/lib/engines-api";
import { cn } from "@/lib/cn";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Callout } from "@/components/product/status-ui";
import {
  CapabilityLegend,
  CapabilityList,
  rowsFromCheck,
  withTests,
  type TestState,
} from "../parts/capabilities";
import { FindingCard } from "../parts/finding-card";
import { FormField, Note, Tag, focusFirstInvalid } from "../parts/form";
import { ModelsBlock, draftsFromCheck, duplicateNames, modelsBody, type ModelDraft } from "../parts/models-block";
import { AccessAsk, accessModels } from "../parts/access-answer";
import { addedMessage, fieldOf, keptOffMessage, looksLikeAddress, trimAddress } from "./draft";
import { ConnectionFields, SummaryLine, type ConnectionDraft, type FieldErrors } from "./step-address";
import { StepKind } from "./step-kind";
import { ProtocolChoice, type ProtocolDraft } from "../parts/protocol-choice";
import { protocolDescriptionsApi } from "@/lib/protocol-descriptions-api";

export const engineDefinitionsKey = ["engineDefinitions"] as const;

export interface AddedEngine {
  serviceId: string;
  message: string;
  registry: ServiceRegistry;
}

type Step = "kind" | "address" | "checking" | "caps" | "detail" | "name";
const STEP_NUMBER: Record<Step, number> = { kind: 1, address: 2, checking: 2, caps: 3, detail: 4, name: 5 };

const DRAFT_TEST_NOTE = "Ran on the gateway’s computer with the values on this screen. Nothing was saved, and it is not in Activity.";

function hostOf(address: string): string {
  return trimAddress(address).replace(/^[a-z][a-z0-9+.-]*:\/\//i, "").split("/")[0] ?? "";
}

/** The id the gateway gave the new engine: the one with this name that was not there before. */
function newServiceId(registry: ServiceRegistry, before: ServiceRegistry | undefined, name: string): string {
  const ids = Object.keys(registry.services ?? {});
  const named = ids.filter((id) => registry.services[id]?.name === name);
  const fresh = named.find((id) => !before?.services?.[id]) ?? named[0];
  return fresh ?? ids.find((id) => !before?.services?.[id]) ?? ids[ids.length - 1] ?? "";
}

export function AddEngineDialog({
  open,
  onOpenChange,
  onAdded,
  onOpenEngine,
  checkRevealMs = 600,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: (added: AddedEngine) => void;
  onOpenEngine: (serviceId: string) => void;
  /** How long the finished check rows stay on screen before step 3. */
  checkRevealMs?: number;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <ReturnFocusScope>
          <Wizard onAdded={onAdded} onOpenEngine={onOpenEngine} checkRevealMs={checkRevealMs} />
        </ReturnFocusScope>
      )}
    </Dialog>
  );
}

/*
 * The steps share one dialog content, so the dialog cannot remember what opened
 * it. The element that had focus when the wizard mounted (the Add engine
 * button) gets it back when the dialog closes. The same scope holds the polite
 * status line, which stays mounted from the first step to the last so a screen
 * reader hears how the check ended, and the body that holds the fields.
 */
interface WizardChrome {
  returnTo: HTMLElement | null;
  announcement: string;
  announce: (text: string) => void;
  bodyRef: RefObject<HTMLDivElement | null>;
}
const Chrome = createContext<WizardChrome>({ returnTo: null, announcement: "", announce: () => undefined, bodyRef: { current: null } });

function ReturnFocusScope({ children }: { children: ReactNode }) {
  const [returnTo] = useState<HTMLElement | null>(() =>
    typeof document === "undefined" ? null : (document.activeElement as HTMLElement | null),
  );
  const [announcement, announce] = useState("");
  const bodyRef = useRef<HTMLDivElement | null>(null);
  return <Chrome.Provider value={{ returnTo, announcement, announce, bodyRef }}>{children}</Chrome.Provider>;
}

function Wizard({
  onAdded,
  onOpenEngine,
  checkRevealMs,
}: {
  onAdded: (added: AddedEngine) => void;
  onOpenEngine: (serviceId: string) => void;
  checkRevealMs: number;
}) {
  const viewer = useViewer();
  const owner = viewer.role === "owner";
  const queryClient = useQueryClient();
  const {
    data: definitionsData,
    isPending: definitionsPending,
    isError: definitionsFailed,
    refetch: refetchDefinitions,
  } = useQuery({ queryKey: engineDefinitionsKey, queryFn: enginesApi.definitions, staleTime: 60_000 });

  const [step, setStep] = useState<Step>("kind");
  const [def, setDef] = useState<EngineDefinition | null>(null);
  const [query, setQuery] = useState("");
  const [conn, setConn] = useState<ConnectionDraft>({ address: "", key: "", envOpen: false, envName: "" });
  const [protocol, setProtocol] = useState<ProtocolDraft>({ dispatch: "", protocol: "" });
  const [protocolError, setProtocolError] = useState<string | null>(null);
  const { data: protocolCatalog, isFetching: protocolCatalogFetching, isError: protocolCatalogError } = useQuery({ queryKey: ["engineProtocolDescriptions"], queryFn: protocolDescriptionsApi.list, enabled: protocol.dispatch === "protocol" });
  const validProtocol = !protocolCatalogFetching && !protocolCatalogError && protocolCatalog?.protocols.some((row) => row.id === protocol.protocol && row.dispatch_supported) === true;
  const [errors, setErrors] = useState<FieldErrors>({});
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [finding, setFinding] = useState<Finding | null>(null);
  const [revealed, setRevealed] = useState<CheckResult | null>(null);
  const abort = useRef<AbortController | null>(null);
  const revealTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [nothingRecognised, setNothingRecognised] = useState(false);
  /** "Add anyway" was pressed for this check's warnings; create says so, so the server does not ask again. */
  const [acknowledged, setAcknowledged] = useState(false);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [models, setModels] = useState<ModelDraft[]>([]);
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [createFinding, setCreateFinding] = useState<Finding | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const { announce, bodyRef } = useContext(Chrome);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const lastStep = useRef<Step>(step);
  const [focusTick, setFocusTick] = useState(0);
  // A new step puts focus on its heading, so a keyboard or screen reader user starts at the top of it.
  useEffect(() => {
    if (lastStep.current === step) return;
    lastStep.current = step;
    headingRef.current?.focus();
  }, [step]);
  // After a failure, the first invalid control wins over the heading (this effect runs second).
  useEffect(() => {
    if (focusTick) focusFirstInvalid(bodyRef.current);
  }, [focusTick, bodyRef]);
  const focusInvalid = () => setFocusTick((t) => t + 1);
  useEffect(
    () => () => {
      abort.current?.abort();
      if (revealTimer.current) clearTimeout(revealTimer.current);
    },
    [],
  );

  const suggested = check?.suggested_name || hostOf(conn.address);
  const engineName = (nameTouched ? name : name || suggested).trim();

  function connectionValues(draft = conn): EngineValues {
    const values: EngineValues = { address: draft.address.trim() };
    if (protocol.dispatch === "protocol") {
      values.dispatch = "protocol";
      values.protocol_description = protocol.protocol;
    }
    if (owner) {
      if (draft.envOpen) {
        if (draft.envName.trim()) values.api_key_env = draft.envName.trim();
      } else if (draft.key) values.api_key = draft.key;
    }
    return values;
  }

  function allValues(): EngineValues {
    return connectionValues();
  }

  function pickKind(d: EngineDefinition) {
    setDef(d);
    setFinding(null);
    setErrors({});
    setStep("address");
  }

  function applyCheck(result: CheckResult, untick = false) {
    setCheck(result);
    setTicked(new Set(untick ? [] : result.capabilities.filter((c) => c.ticked).map((c) => c.id)));
    setModels(draftsFromCheck(result.models));
    setTests({});
    setNothingRecognised(untick);
    setFinding(null);
    setRevealed(null);
    setStep("caps");
  }

  function runCheck(draft = conn) {
    if (!def) return;
    if (protocol.dispatch === "protocol") {
      setProtocolError("Protocol descriptions are validated without contacting the engine. Add this engine off.");
      return;
    }
    const address = fieldOf(def, "address");
    const next: FieldErrors = {};
    if (!looksLikeAddress(draft.address))
      next.address = `Enter an address that starts with http:// or https://, for example ${address?.placeholder ?? "http://192.168.1.50:8080"}.`;
    setErrors(next);
    if (Object.keys(next).length) {
      focusInvalid();
      return;
    }
    setFinding(null);
    setAcknowledged(false);
    setRevealed(null);
    announce("Checking the server.");
    setStep("checking");
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    enginesApi
      .check({ definition: def.definition, values: connectionValues(draft) }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.outcome === "ok") {
          const count = result.models.length;
          announce(`Check finished: reached the server, ${count} ${count === 1 ? "model" : "models"} found.`);
          if (checkRevealMs > 0) {
            setRevealed(result);
            revealTimer.current = setTimeout(() => applyCheck(result), checkRevealMs);
          } else applyCheck(result);
          return;
        }
        setCheck(result);
        const first = result.findings.find((f) => f.severity === "error") ?? result.findings[0] ?? null;
        setFinding(first);
        announce(first ? `Check finished: ${first.title}.` : "Check finished with a problem.");
        if (first?.severity === "error") focusInvalid();
        setStep("address");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const field = fieldFromError(error) as keyof FieldErrors | null;
        const message = error instanceof Error ? error.message : "The check could not run.";
        announce(`Check finished: ${message}`);
        focusInvalid();
        if (field && ["address", "api_key", "api_key_env"].includes(field)) setErrors({ [field]: message });
        else if (error instanceof ApiError && error.code === "owner_required") setErrors({ api_key: message });
        else setErrors({ address: `Azizam could not run the check. ${message}` });
        setStep("address");
      });
  }

  function cancelCheck() {
    abort.current?.abort();
    if (revealTimer.current) clearTimeout(revealTimer.current);
    setRevealed(null);
    announce("Check cancelled.");
    setStep("address");
  }

  function fixPath(action: FindingAction) {
    let address = trimAddress(conn.address);
    if (action.address_append) address += action.address_append;
    if (action.address_strip && address.endsWith(action.address_strip)) address = address.slice(0, -action.address_strip.length);
    const draft = { ...conn, address };
    setConn(draft);
    runCheck(draft);
  }

  function addAnyway() {
    if (!check) return;
    setAcknowledged(true);
    applyCheck(check, finding?.code === "nocap");
  }

  async function runTest(capability: string) {
    if (!def) return;
    if (protocol.dispatch === "protocol") return;
    setTests((t) => ({ ...t, [capability]: { status: "running" } }));
    const label = def.capabilities.find((c) => c.id === capability)?.label.toLowerCase() ?? "";
    const offered = models.filter((m) => m.on);
    const model = (offered.find((m) => m.use.toLowerCase().includes(label)) ?? offered[0] ?? models[0])?.id;
    try {
      const result = await enginesApi.test({
        definition: def.definition,
        values: allValues(),
        capability,
        ...(model ? { model } : {}),
      });
      const row = check?.capabilities.find((c) => c.id === capability);
      const tickForYou = result.ok && row?.level === "possible" && !ticked.has(capability);
      if (tickForYou) setTicked((s) => new Set(s).add(capability));
      setTests((t) => ({ ...t, [capability]: { status: "done", result, tickedForYou: tickForYou } }));
    } catch (error) {
      setTests((t) => ({ ...t, [capability]: { status: "error", message: error instanceof Error ? error.message : "" } }));
    }
  }

  function tickedLabels(): string[] {
    if (!def) return [];
    return def.capabilities.filter((c) => ticked.has(c.id)).map((c) => c.label);
  }

  function testsBody() {
    const out: Record<string, { ok: boolean; at: number; seconds?: number }> = {};
    for (const [id, t] of Object.entries(tests)) {
      if (t.status === "done") out[t.result.capability || id] = { ok: t.result.ok, at: t.result.at, seconds: t.result.seconds };
    }
    return out;
  }

  async function create(options: { acknowledge?: boolean; keepOff?: boolean; needKey?: boolean } = {}) {
    if (!def) return;
    const finalName = engineName || suggested;
    let body: CreateEngineBody;
    if (options.keepOff) {
      body = { definition: def.definition, name: finalName, values: connectionValues(), disabled: true };
      if (options.needKey) body.keep_off_reason = "needkey";
    } else {
      body = { definition: def.definition, name: finalName, values: allValues() };
      body.capabilities = def.capabilities.filter((c) => ticked.has(c.id)).map((c) => c.id);
      body.models = modelsBody(models);
      body.tests = testsBody();
      if (options.acknowledge || acknowledged) body.acknowledge_warnings = true;
    }
    setCreating(true);
    setCreateError(null);
    const before = queryClient.getQueryData<ServiceRegistry>(queryKeys.serviceRegistry);
    try {
      const { registry, serviceId } = await enginesApi.create(body);
      onAdded({
        serviceId: serviceId || newServiceId(registry, before, finalName),
        message: protocol.dispatch === "protocol"
          ? `Added “${finalName}”, turned off. The description was validated without contacting the engine. Configure its offerings on the engine page before turning it on.`
          : options.keepOff ? keptOffMessage(finalName, Boolean(options.needKey)) : addedMessage(finalName, tickedLabels()),
        registry,
      });
    } catch (error) {
      const refused = checkFromError(error);
      const first = refused?.findings.find((f) => f.severity === "error") ?? refused?.findings[0];
      const field = fieldFromError(error);
      if (first && (first.code === "dup" || first.severity === "warning")) {
        setCreateFinding(first);
        if (step !== "address") setStep("name");
        else setFinding(first);
      } else if (first) {
        if (refused) setCheck(refused);
        setFinding(first);
        focusInvalid();
        setStep("address");
      } else if (field && ["address", "api_key", "api_key_env"].includes(field)) {
        setErrors({ [field]: error instanceof Error ? error.message : "" });
        focusInvalid();
        setStep("address");
      } else {
        setCreateError(error instanceof Error ? error.message : "The gateway did not answer.");
        if (step === "checking") setStep("address");
      }
    } finally {
      setCreating(false);
    }
  }

  function addNow() {
    if (!nameTouched && !name) setName(suggested);
    void create();
  }

  function back() {
    const order: Record<Step, Step> = { kind: "kind", address: "kind", checking: "address", caps: "address", detail: "caps", name: "detail" };
    setFinding(null);
    setCreateFinding(null);
    setStep(order[step]);
  }

  function next() {
    if (step === "caps") setStep("detail");
    else if (step === "detail") {
      if (!name && !nameTouched) setName(suggested);
      setStep("name");
    }
  }

  if (definitionsPending)
    return (
      <Frame step={1} stepName="Kind" footer={null}>
        <Skeleton aria-hidden="true" className="h-40 w-full" />
      </Frame>
    );
  if (definitionsFailed || !definitionsData)
    return (
      <Frame step={1} stepName="Kind" footer={null}>
        <Callout
          as="div"
          severity="critical"
          title="Server kinds could not be loaded"
          meta="Azizam needs the list of server kinds to add an engine. Try again."
          actions={
            <Button type="button" variant="outline" size="sm" onClick={() => void refetchDefinitions()}>
              Retry
            </Button>
          }
        />
      </Frame>
    );

  const definitions = definitionsData.definitions;
  if (step === "kind" || !def)
    return (
      <Frame
        step={1}
        stepName={definitions[0]?.steps[0] ?? "Kind"}
        footer={<FooterRow right={<CloseButton />} />}
      >
        <StepKind definitions={definitions} query={query} onQuery={setQuery} onPick={pickKind} headingRef={headingRef} />
      </Frame>
    );

  const stepName = def.steps[STEP_NUMBER[step] - 1] ?? "";
  const empty = !conn.address.trim();
  const keepOffButton = (label = "Add but keep off", needKey = false) => (
    <Button type="button" variant="outline" size="md" disabled={creating} onClick={() => void create({ keepOff: true, needKey })}>
      {label}
    </Button>
  );
  const errorCallout = createError && (
    <Callout as="div" severity="critical" title="The engine could not be added" meta={createError} />
  );

  if (step === "address" || step === "checking") {
    const checking = step === "checking";
    const f = checking ? null : finding;
    let footer: ReactNode;
    if (checking)
      footer = (
        <FooterRow
          right={
            <>
              <Button type="button" variant="outline" size="md" onClick={cancelCheck}>
                Cancel check
              </Button>
              <Button type="button" size="md" disabled>
                <Loader2 aria-hidden="true" className="animate-spin" />
                Checking…
              </Button>
            </>
          }
        />
      );
    else if (protocol.dispatch === "protocol")
      footer = (
        <FooterRow left={<BackButton onClick={back} />} right={
          <Button type="button" size="md" disabled={creating || !looksLikeAddress(conn.address) || !validProtocol} onClick={() => void create({ keepOff: true })}>
            Add protocol engine (off)
          </Button>
        } />
      );
    else if (f?.severity === "warning")
      footer = (
        <FooterRow
          left={<BackButton onClick={back} />}
          right={
            <>
              {keepOffButton()}
              <Button type="button" size="md" onClick={addAnyway}>
                Add anyway
              </Button>
            </>
          }
        />
      );
    else if (f?.owner_note)
      footer = (
        <FooterRow
          right={
            <>
              <CloseButton label="Cancel" />
              <Button type="button" size="md" className="whitespace-normal" disabled={creating} onClick={() => void create({ keepOff: true, needKey: true })}>
                Add but keep off (owner adds the key)
              </Button>
            </>
          }
        />
      );
    else if (f?.keep_off)
      footer = (
        <FooterRow
          left={<BackButton onClick={back} />}
          right={
            <>
              {keepOffButton()}
              <Button type="button" size="md" disabled={empty} onClick={() => runCheck()}>
                Check again
              </Button>
            </>
          }
        />
      );
    else
      footer = (
        <FooterRow
          left={<BackButton onClick={back} />}
          right={
            <Button type="button" size="md" disabled={empty} onClick={() => runCheck()}>
              {f && f.field !== "address" ? "Check again" : "Continue"}
            </Button>
          }
        />
      );
    const fine = protocol.dispatch === "protocol"
      ? "Validation checks the installed description, without contacting the engine. Configure its offerings on the engine page before turning it on."
      : f?.owner_note
      ? "“Add but keep off” saves the engine without a key and turned off. The owner adds the key later, then turns it on."
      : f && (f.keep_off || f.severity === "warning")
        ? "“Add but keep off” adds the engine turned off and skips the check. It shows as not checked yet, and Azizam checks it when you turn it on or press Check now."
        : "Continue sends a few harmless requests: lists and empty ones. Nothing is generated and nothing is saved yet.";
    return (
      <Frame step={2} stepName={stepName} footer={footer}>
        {checking ? (
          <CheckingRows definition={def} revealed={revealed} headingRef={headingRef} />
        ) : (
          <div>
            <h3 ref={headingRef} tabIndex={-1} className="text-heading outline-none">
              Where is the server?
            </h3>
            <p className="text-meta text-muted-foreground mt-0.5">
              {protocol.dispatch === "protocol"
                ? "Enter the root address and a key if it needs one. The description supplies the request paths."
                : "Enter the server address and a key if it needs one. Azizam then looks at the server and fills in the rest."}
            </p>
          </div>
        )}
        {f && <SummaryLine finding={f} />}
        {errorCallout}
        <ConnectionFields
          definition={def}
          draft={conn}
          onChange={(patch) => {
            setConn((c) => ({ ...c, ...patch }));
            const edited = ["address", "key", "envName"].some((k) => k in patch);
            // A finding belongs to the values that were checked; an edit makes it stale.
            if (edited && finding) {
              setFinding(null);
              setAcknowledged(false);
            }
            if (patch.address !== undefined) setErrors((e) => ({ ...e, address: undefined }));
          }}
          owner={owner}
          finding={f}
          errors={errors}
          disabled={checking}
          onChangeKind={() => setStep("kind")}
          onFixPath={fixPath}
          onOpenExisting={onOpenEngine}
          onRecheck={() => runCheck()}
          protocolDispatch={protocol.dispatch === "protocol"}
        />
        <ProtocolChoice value={protocol} disabled={checking} onChange={(value) => {
          setProtocol(value);
          setProtocolError(null);
          setFinding(null);
          setAcknowledged(false);
        }} />
        {protocolError && <p role="alert" className="text-meta text-danger">{protocolError}</p>}
        {!checking && <p className="text-meta text-muted-foreground">{fine}</p>}
      </Frame>
    );
  }

  const noneTicked = ticked.size === 0;
  const addNowButton = (disabled: boolean) => (
    <Button type="button" variant="outline" size="md" disabled={disabled || creating} onClick={addNow}>
      Add engine now
    </Button>
  );

  if (step === "caps" && check) {
    const keyWord = check.auth === "accepted" ? "Key accepted" : "No key needed";
    const rows = withTests(rowsFromCheck(check.capabilities), tests);
    return (
      <Frame
        step={3}
        stepName={stepName}
        footer={
          <FooterRow
            left={<BackButton onClick={back} />}
            right={
              <>
                {addNowButton(noneTicked)}
                <Button type="button" size="md" onClick={next}>
                  Continue
                </Button>
              </>
            }
          />
        }
      >
        <div>
          <h3 ref={headingRef} tabIndex={-1} className="text-heading outline-none">
            What this server can do
          </h3>
          <p className="text-meta text-muted-foreground mt-0.5">
            Azizam looked at the server. Tick what you want to use. Review the selected abilities. They become available when you
            add the engine.
          </p>
        </div>
        {errorCallout}
        {nothingRecognised ? (
          <Note tone="warn" role="status">
            Azizam could not tell what this server can do. Tick what you know it can do, then run a real test.
          </Note>
        ) : (
          <Note tone="ok">
            {`Reached the server. ${keyWord}. Found ${check.models.length} models.`}
          </Note>
        )}
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
          tests={tests}
          onTest={(id) => void runTest(id)}
          testNote={DRAFT_TEST_NOTE}
        />
        {noneTicked && (
          <Note tone="warn" role="status">
            Nothing is turned on. The engine will be added but will not serve anything.
          </Note>
        )}
        <p className="text-meta text-muted-foreground">
          A real test sends one tiny request from the gateway’s computer, using the values on this screen. Nothing is saved, and it
          does not show in Activity. It can take a few seconds if the server has to load a model.
        </p>
        <CapabilityLegend />
        <p className="text-meta text-muted-foreground">
          In a hurry? “Add engine now” adds what is ticked, every model the server listed, and the name “{suggested}”.
        </p>
      </Frame>
    );
  }

  if (step === "detail") {
    const dup = duplicateNames(models).size > 0;
    return (
      <Frame
        step={4}
        stepName={stepName}
        footer={
          <FooterRow
            left={<BackButton onClick={back} />}
            right={
              <Button type="button" size="md" disabled={dup} onClick={next}>
                Continue
              </Button>
            }
          />
        }
      >
        <div>
          <h3 ref={headingRef} tabIndex={-1} className="text-heading outline-none">
            Models and limits
          </h3>
          <p className="text-meta text-muted-foreground mt-0.5">
            {models.length
              ? `A public name is what apps type to pick a model, for example “${models[0].public_name}”. `
              : "A public name is what apps type to pick a model. "}
            The context window is how much text a model can read at once.
          </p>
        </div>
        {def.models_note && <Note tone="info">{def.models_note}</Note>}
        {models.length > 0 && (
          <Note>
            Prefilled values <Tag tone="suggest">Suggested</Tag> come from the server. They work as they are. Change one only if you
            know better.
          </Note>
        )}
        <ModelsBlock models={models} onChange={setModels} emptyText="The server listed no models. Add a model by name below." />
      </Frame>
    );
  }

  // Step 5: name and create.
  const recap = recapRows();
  const nameHelp = nameTouched
    ? "People see this name on Engines and Status."
    : `${
        check?.name_source === "server"
          ? `Taken from what the server calls itself, “${suggested}”. `
          : check?.name_source === "address"
            ? "This server does not name itself, so Azizam used its address. "
            : ""
      }Change it to something you will recognise.`;
  const isDup = createFinding?.code === "dup";
  const isWarning = createFinding?.severity === "warning";
  return (
    <Frame
      step={5}
      stepName={stepName}
      footer={
        <FooterRow
          left={<BackButton onClick={back} />}
          right={
            <Button
              type="button"
              size="md"
              disabled={isDup || !engineName || creating}
              onClick={() => void create({ acknowledge: isWarning })}
            >
              {creating && <Loader2 aria-hidden="true" className="animate-spin" />}
              {isWarning ? "Add anyway" : "Add engine"}
            </Button>
          }
        />
      }
    >
      <div>
        <h3 ref={headingRef} tabIndex={-1} className="text-heading outline-none">
          Name and create
        </h3>
        <p className="text-meta text-muted-foreground mt-0.5">One last check of what you are adding.</p>
      </div>
      {createFinding && (
        <FindingCard
          finding={createFinding}
          onOpenExisting={onOpenEngine}
          extraActions={
            isDup && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  setCreateFinding(null);
                  setStep("address");
                }}
              >
                Change the address
              </Button>
            )
          }
        />
      )}
      {errorCallout}
      <FormField
        label="Name"
        required
        help={nameHelp}
        tag={nameTouched ? undefined : <Tag tone="suggest">Suggested</Tag>}
      >
        {(control) => (
          <Input
            {...control}
            value={nameTouched ? name : name || suggested}
            autoComplete="off"
            onChange={(event) => {
              setNameTouched(true);
              setName(event.target.value);
            }}
          />
        )}
      </FormField>
      <div className="bg-card rounded-panel border p-4">
        <dl data-testid="recap" className="text-meta grid gap-x-4 gap-y-1.5 sm:grid-cols-[auto_1fr]">
          {recap.map(([term, value]) => (
            <div key={term} className="contents">
              <dt className="text-muted-foreground">{term}</dt>
              <dd className="min-w-0 [overflow-wrap:anywhere]">{value}</dd>
            </div>
          ))}
        </dl>
      </div>
      <AccessAsk
        capabilities={def.capabilities.filter((c) => ticked.has(c.id)).map((c) => c.id)}
        models={accessModels(models, def.capabilities)}
        newTab
      />
    </Frame>
  );

  function recapRows(): [string, string][] {
    if (!def) return [];
    const rows: [string, string][] = [["Kind", def.title], ["Address", conn.address.trim()]];
    rows.push([
      "Key",
      owner && conn.envOpen && conn.envName.trim()
        ? "Read from a server variable"
        : owner && conn.key
          ? "Stored on this gateway. Never shown again."
          : "No key supplied.",
    ]);
    rows.push(["Will offer", tickedLabels().join(", ") || "Nothing yet"]);
    const on = models.filter((m) => m.on).length;
    const added = models.filter((m) => m.on && m.byName).length;
    rows.push(["Models", `${on} offered${added ? ` (${on - added} the server listed, ${added} you added)` : ""}`]);
    return rows;
  }
}

function Frame({ step, stepName, footer, children }: { step: number; stepName: string; footer: ReactNode; children: ReactNode }) {
  const { returnTo, announcement, bodyRef } = useContext(Chrome);
  return (
    <DialogContent
      aria-describedby={undefined}
      className="flex! max-h-[85dvh] flex-col gap-0! overflow-hidden p-0 sm:max-w-2xl"
      onCloseAutoFocus={(event) => {
        if (returnTo?.isConnected) {
          event.preventDefault();
          returnTo.focus();
        }
      }}
    >
      <p data-testid="wizard-announcement" role="status" aria-live="polite" aria-label="Add engine progress" className="sr-only">
        {announcement}
      </p>
      <div className="shrink-0 px-6 pt-5 pr-14 pb-3">
        <DialogTitle className="text-heading">Add engine</DialogTitle>
        <p className="text-meta text-muted-foreground mt-0.5">
          Step {step} of 5 · {stepName}
        </p>
        <div aria-hidden="true" className="mt-3 grid grid-cols-5 gap-1.5">
          {[1, 2, 3, 4, 5].map((i) => (
            <i key={i} className={cn("h-1 rounded-full", i <= step ? "bg-primary" : "bg-muted")} />
          ))}
        </div>
      </div>
      <div ref={bodyRef} data-slot="dialog-body" className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-4">
        {children}
      </div>
      {footer && (
        <div data-slot="dialog-footer" className="shrink-0 border-t px-6 py-3">
          {footer}
        </div>
      )}
    </DialogContent>
  );
}

function FooterRow({ left, right }: { left?: ReactNode; right: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div>{left}</div>
      <div className="flex flex-wrap justify-end gap-2">{right}</div>
    </div>
  );
}

function BackButton({ onClick }: { onClick: () => void }) {
  return (
    <Button type="button" variant="ghost" size="md" onClick={onClick}>
      Back
    </Button>
  );
}

function CloseButton({ label = "Cancel" }: { label?: string }) {
  return (
    <DialogClose asChild>
      <Button type="button" variant="outline" size="md">
        {label}
      </Button>
    </DialogClose>
  );
}

function CheckingRows({
  definition,
  revealed,
  headingRef,
}: {
  definition: EngineDefinition;
  revealed: CheckResult | null;
  headingRef: RefObject<HTMLHeadingElement | null>;
}) {
  // The rows say nothing about progress. They tick only when the server's real answer arrives.
  return (
    <>
      <div>
        <h3 ref={headingRef} tabIndex={-1} className="text-heading outline-none">
          Checking the server
        </h3>
        <p className="text-meta text-muted-foreground mt-0.5 flex items-center gap-2">
          {!revealed && <Loader2 aria-hidden="true" className="size-4 animate-spin" />}
          Checking the server. This may take a few seconds.
        </p>
      </div>
      <ul aria-label="Check progress" className="bg-card rounded-panel divide-y border">
        {definition.check_rows.map((label) => {
          const result = revealed?.steps.find((s) => s.label === label)?.result ?? "";
          return (
            <li key={label} className="text-meta flex items-center gap-2 px-4 py-2.5">
              {revealed ? (
                <CircleCheck aria-hidden="true" className="text-success size-4" />
              ) : (
                <Circle aria-hidden="true" className="text-muted-foreground size-4" />
              )}
              <span className={cn("flex-1", !revealed && "text-muted-foreground")}>{label}</span>
              {revealed && result && <span className="text-muted-foreground">{result}</span>}
            </li>
          );
        })}
      </ul>
      <Note tone="info">Azizam sends only lists and empty requests. Nothing is generated and nothing is saved yet.</Note>
    </>
  );
}

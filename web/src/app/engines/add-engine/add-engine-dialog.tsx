"use client";

/**
 * Add engine: a five-step dialog driven by the server's engine definitions.
 * Kind, address and key, what it can do (or the workflow file), models (or
 * the model name, or the workflow inputs), then name and create. Every field
 * label, help line, check row and capability comes from the definition.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
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
  TestResultBlock,
  rowsFromCheck,
  withTests,
  type TestState,
} from "../parts/capabilities";
import { FindingCard } from "../parts/finding-card";
import { FormField, Note, Tag } from "../parts/form";
import { ModelsBlock, draftsFromCheck, duplicateNames, modelsBody, type ModelDraft } from "../parts/models-block";
import { SpeechFields, speechDraft, speechValues, type SpeechDraft } from "../parts/speech-fields";
import {
  addedMessage,
  clockTime,
  fieldOf,
  hasPath,
  joinUrl,
  keptOffMessage,
  looksLikeAddress,
  trimAddress,
} from "./draft";
import { ConnectionFields, SummaryLine, type ConnectionDraft, type FieldErrors } from "./step-address";
import { StepKind } from "./step-kind";
import { StepInputs, StepWorkflowFile, inputsMissing, type WorkflowOk } from "./step-workflow";

export const engineDefinitionsKey = ["engineDefinitions"] as const;

export interface AddedEngine {
  serviceId: string;
  message: string;
  registry: ServiceRegistry;
}

type Step = "kind" | "address" | "checking" | "caps" | "detail" | "name";
const STEP_NUMBER: Record<Step, number> = { kind: 1, address: 2, checking: 2, caps: 3, detail: 4, name: 5 };

const DRAFT_TEST_NOTE = "Ran on the gateway’s computer with the values on this screen. Nothing was saved, and it is not in Activity.";

interface WorkflowDraft {
  read: WorkflowOk | null;
  finding: Finding | null;
  reading: boolean;
  makes: "images" | "video";
  inputs: Record<string, string>;
  publicName: string;
  publicSuggested: boolean;
}
const emptyWorkflow: WorkflowDraft = {
  read: null,
  finding: null,
  reading: false,
  makes: "images",
  inputs: {},
  publicName: "",
  publicSuggested: true,
};

function readText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

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
      {open && <Wizard onAdded={onAdded} onOpenEngine={onOpenEngine} checkRevealMs={checkRevealMs} />}
    </Dialog>
  );
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
  const defs = useQuery({ queryKey: engineDefinitionsKey, queryFn: enginesApi.definitions, staleTime: 60_000 });

  const [step, setStep] = useState<Step>("kind");
  const [def, setDef] = useState<EngineDefinition | null>(null);
  const [query, setQuery] = useState("");
  const [conn, setConn] = useState<ConnectionDraft>({ address: "", path: "", key: "", envOpen: false, envName: "" });
  const [errors, setErrors] = useState<FieldErrors>({});
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [finding, setFinding] = useState<Finding | null>(null);
  const [checkIndex, setCheckIndex] = useState(0);
  const [revealed, setRevealed] = useState<CheckResult | null>(null);
  const abort = useRef<AbortController | null>(null);
  const revealTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [nothingRecognised, setNothingRecognised] = useState(false);
  /** "Add anyway" was pressed for this check's warnings; create says so, so the server does not ask again. */
  const [acknowledged, setAcknowledged] = useState(false);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [models, setModels] = useState<ModelDraft[]>([]);
  const [speech, setSpeech] = useState<SpeechDraft | null>(null);
  const [wf, setWf] = useState<WorkflowDraft>(emptyWorkflow);
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [createFinding, setCreateFinding] = useState<Finding | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (step !== "checking" || !def) return;
    const timer = setInterval(() => setCheckIndex((i) => Math.min(i + 1, def.check_rows.length - 1)), 700);
    return () => clearInterval(timer);
  }, [step, def]);
  useEffect(
    () => () => {
      abort.current?.abort();
      if (revealTimer.current) clearTimeout(revealTimer.current);
    },
    [],
  );

  const flow = def?.flow;
  const declared = def ? hasPath(def) : false;
  const suggested = check?.suggested_name || hostOf(conn.address);
  const engineName = (nameTouched ? name : name || suggested).trim();

  function connectionValues(draft = conn): EngineValues {
    const values: EngineValues = { address: draft.address.trim() };
    if (def && hasPath(def)) values.path = draft.path.trim();
    if (owner) {
      if (draft.envOpen) {
        if (draft.envName.trim()) values.api_key_env = draft.envName.trim();
      } else if (draft.key) values.api_key = draft.key;
    }
    return values;
  }

  function capabilityForMakes(makes: "images" | "video"): string {
    const caps = def?.capabilities ?? [];
    return (caps.find((c) => c.id.endsWith(makes)) ?? caps[0])?.id ?? makes;
  }

  function allValues(): EngineValues {
    const values = connectionValues();
    if (!def) return values;
    if (flow === "declared" && speech) Object.assign(values, speechValues(def, speech));
    if (flow === "workflow" && wf.read) {
      values.workflow_public_name = wf.publicName.trim();
      values.workflow_graph = wf.read.graph;
      values.workflow_file = wf.read.file;
      values.workflow_makes = wf.makes;
      values.workflow_inputs = Object.fromEntries(Object.entries(wf.inputs).filter(([, v]) => v));
    }
    return values;
  }

  function pickKind(d: EngineDefinition) {
    setDef(d);
    setConn((c) => ({ ...c, path: "" }));
    setFinding(null);
    setErrors({});
    setSpeech(speechDraft(d));
    setWf(emptyWorkflow);
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
    const address = fieldOf(def, "address");
    const path = fieldOf(def, "path");
    const next: FieldErrors = {};
    if (!looksLikeAddress(draft.address))
      next.address = `Enter an address that starts with http:// or https://, for example ${address?.placeholder ?? "http://192.168.1.50:8080"}.`;
    if (path && !draft.path.trim()) next.path = `Type the path the server uses, for example ${path.placeholder ?? "/inference"}.`;
    setErrors(next);
    if (Object.keys(next).length) return;
    setFinding(null);
    setAcknowledged(false);
    setCheckIndex(0);
    setRevealed(null);
    setStep("checking");
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    enginesApi
      .check({ definition: def.definition, values: connectionValues(draft) }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.outcome === "ok") {
          if (checkRevealMs > 0) {
            setRevealed(result);
            revealTimer.current = setTimeout(() => applyCheck(result), checkRevealMs);
          } else applyCheck(result);
          return;
        }
        setCheck(result);
        const first = result.findings.find((f) => f.severity === "error") ?? result.findings[0] ?? null;
        setFinding(first);
        setStep("address");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const field = fieldFromError(error) as keyof FieldErrors | null;
        const message = error instanceof Error ? error.message : "The check could not run.";
        if (field && ["address", "path", "api_key", "api_key_env"].includes(field)) setErrors({ [field]: message });
        else if (error instanceof ApiError && error.code === "owner_required") setErrors({ api_key: message });
        else setErrors({ address: `Azizam could not run the check. ${message}` });
        setStep("address");
      });
  }

  function cancelCheck() {
    abort.current?.abort();
    if (revealTimer.current) clearTimeout(revealTimer.current);
    setRevealed(null);
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
    setTests((t) => ({ ...t, [capability]: { status: "running" } }));
    const label = def.capabilities.find((c) => c.id === capability)?.label.toLowerCase() ?? "";
    const offered = models.filter((m) => m.on);
    const model = flow === "detect" ? (offered.find((m) => m.use.toLowerCase().includes(label)) ?? offered[0] ?? models[0])?.id : undefined;
    try {
      const result = await enginesApi.test({
        definition: def.definition,
        values: allValues(),
        capability,
        ...(model ? { model } : {}),
        ...(flow === "workflow" && wf.read ? { workflow: wf.read.graph, inputs: allValues().workflow_inputs } : {}),
      });
      const row = check?.capabilities.find((c) => c.id === capability);
      const tickForYou = result.ok && row?.level === "possible" && !ticked.has(capability);
      if (tickForYou) setTicked((s) => new Set(s).add(capability));
      setTests((t) => ({ ...t, [capability]: { status: "done", result, tickedForYou: tickForYou } }));
    } catch (error) {
      setTests((t) => ({ ...t, [capability]: { status: "error", message: error instanceof Error ? error.message : "" } }));
    }
  }

  async function chooseFile(file: File) {
    if (!def) return;
    const max = def.workflow_max_bytes ?? 20 * 1048576;
    if (file.size > max) {
      setWf({
        ...emptyWorkflow,
        finding: {
          code: "too_large",
          severity: "error",
          field: "file",
          title: "The file is too large",
          what: `“${file.name}” is larger than ${Math.round(max / 1048576)} MB.`,
          why: "A workflow file is small. A larger file is probably not a workflow.",
          fix: "Pick the workflow file you exported in API format.",
          keep_off: true,
        },
      });
      return;
    }
    setWf((w) => ({ ...w, reading: true }));
    try {
      const text = await readText(file);
      const read = await enginesApi.workflow({ definition: def.definition, file: file.name, text });
      if (read.ok) {
        setWf({
          read,
          finding: null,
          reading: false,
          makes: read.makes,
          inputs: Object.fromEntries(read.inputs.filter((r) => r.auto).map((r) => [r.key, r.auto])),
          publicName: read.file_stem,
          publicSuggested: true,
        });
        setTests({});
      } else {
        setWf({
          ...emptyWorkflow,
          finding: read.finding ?? {
            code: read.reason,
            severity: "error",
            field: "file",
            title: "This file is not in API format",
            what: `Azizam could not read “${file.name}”.`,
            why: read.found ?? "",
            fix: "In the editor, choose “Export (API)”, then pick that new file here.",
            keep_off: true,
          },
        });
      }
    } catch (error) {
      setWf({
        ...emptyWorkflow,
        finding: {
          code: "read_failed",
          severity: "error",
          field: "file",
          title: "Azizam could not read the file",
          what: error instanceof Error ? error.message : "The file could not be read.",
          why: "The gateway did not answer, or the file could not be opened.",
          fix: "Choose the file again.",
          keep_off: true,
        },
      });
    }
  }

  function tickedLabels(): string[] {
    if (!def) return [];
    if (flow === "workflow") return [wf.makes === "video" ? "Video" : "Images"];
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
      body.capabilities = flow === "workflow" ? [capabilityForMakes(wf.makes)] : def.capabilities.filter((c) => ticked.has(c.id)).map((c) => c.id);
      if (flow === "detect") body.models = modelsBody(models);
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
        message: options.keepOff ? keptOffMessage(finalName, Boolean(options.needKey)) : addedMessage(finalName, tickedLabels()),
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
        setStep("address");
      } else if (field && ["address", "path", "api_key", "api_key_env"].includes(field)) {
        setErrors({ [field]: error instanceof Error ? error.message : "" });
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

  if (defs.isPending)
    return (
      <Frame step={1} stepName="Kind" footer={null}>
        <Skeleton aria-hidden="true" className="h-40 w-full" />
      </Frame>
    );
  if (defs.isError || !defs.data)
    return (
      <Frame step={1} stepName="Kind" footer={null}>
        <Callout
          as="div"
          severity="critical"
          title="Server kinds could not be loaded"
          meta="Azizam needs the list of server kinds to add an engine. Try again."
          actions={
            <Button type="button" variant="outline" size="sm" onClick={() => void defs.refetch()}>
              Retry
            </Button>
          }
        />
      </Frame>
    );

  const definitions = defs.data.definitions;
  if (step === "kind" || !def)
    return (
      <Frame
        step={1}
        stepName={definitions[0]?.steps[0] ?? "Kind"}
        footer={<FooterRow right={<CloseButton />} />}
      >
        <StepKind definitions={definitions} query={query} onQuery={setQuery} onPick={pickKind} />
      </Frame>
    );

  const stepName = def.steps[STEP_NUMBER[step] - 1] ?? "";
  const empty = !conn.address.trim() || (declared && !conn.path.trim());
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
    const fine = f?.owner_note
      ? "“Add but keep off” saves the engine without a key and turned off. The owner adds the key later, then turns it on."
      : f && (f.keep_off || f.severity === "warning")
        ? "“Add but keep off” adds the engine turned off and skips the check. It shows as not checked yet, and Azizam checks it when you turn it on or press Check now."
        : declared
          ? "Continue sends one empty request to the path. Nothing is generated and nothing is saved yet."
          : "Continue sends a few harmless requests: lists and empty ones. Nothing is generated and nothing is saved yet.";
    return (
      <Frame step={2} stepName={stepName} footer={footer}>
        {checking ? (
          <CheckingRows definition={def} index={checkIndex} revealed={revealed} declared={declared} />
        ) : (
          <div>
            <h3 className="text-heading">Where is the server?</h3>
            <p className="text-meta text-muted-foreground mt-0.5">
              {declared
                ? "Tell Azizam where the server is and which path to call. Azizam then checks the path and fills in the rest."
                : "Two fields. Azizam then looks at the server and fills in the rest."}
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
            const edited = ["address", "path", "key", "envName"].some((k) => k in patch);
            // A finding belongs to the values that were checked; an edit makes it stale.
            if (edited && finding) {
              setFinding(null);
              setAcknowledged(false);
            }
            if (patch.address !== undefined) setErrors((e) => ({ ...e, address: undefined }));
            if (patch.path !== undefined) setErrors((e) => ({ ...e, path: undefined }));
          }}
          owner={owner}
          finding={f}
          errors={errors}
          disabled={checking}
          onChangeKind={() => setStep("kind")}
          onFixPath={fixPath}
          onOpenExisting={onOpenEngine}
          onRecheck={() => runCheck()}
        />
        {!checking && <p className="text-meta text-muted-foreground">{fine}</p>}
      </Frame>
    );
  }

  const noneTicked = flow !== "workflow" && ticked.size === 0;
  const addNowButton = (disabled: boolean) => (
    <Button type="button" variant="outline" size="md" disabled={disabled || creating} onClick={addNow}>
      Add now with these settings
    </Button>
  );

  if (step === "caps" && flow === "workflow") {
    const failed = !wf.read && wf.finding;
    return (
      <Frame
        step={3}
        stepName={stepName}
        footer={
          <FooterRow
            left={<BackButton onClick={back} />}
            right={
              <>
                {failed && keepOffButton()}
                <Button type="button" size="md" disabled={!wf.read} onClick={next}>
                  Continue
                </Button>
              </>
            }
          />
        }
      >
        {errorCallout}
        <StepWorkflowFile
          definition={def}
          read={wf.read}
          finding={wf.finding}
          reading={wf.reading}
          makes={wf.makes}
          onMakes={(makes) => setWf((w) => ({ ...w, makes }))}
          onFile={(file) => void chooseFile(file)}
          onReplace={() => {
            setWf(emptyWorkflow);
            setTests({});
          }}
        />
      </Frame>
    );
  }

  if (step === "caps" && check) {
    const keyWord = check.auth === "accepted" ? "Key accepted" : "No key needed";
    const rows = withTests(rowsFromCheck(check.capabilities), tests);
    const firstLower = def.summary.charAt(0).toLowerCase() + def.summary.slice(1);
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
          <h3 className="text-heading">{declared ? "What this server will do" : "What this server can do"}</h3>
          <p className="text-meta text-muted-foreground mt-0.5">
            {declared
              ? `You said this server ${firstLower.replace(/\.$/, "")}. One real test shows that it works.`
              : "Azizam looked at the server. Tick what you want to use. Nothing turns on until you do."}
          </p>
        </div>
        {errorCallout}
        {nothingRecognised ? (
          <Note tone="warn" role="status">
            Azizam could not tell what this server can do. Tick what you know it can do, then run a real test.
          </Note>
        ) : (
          <Note tone="ok" role="status">
            {declared
              ? `Reached the server. ${keyWord}. The path answered.`
              : `Reached the server. ${keyWord}. Found ${check.models.length} models.`}
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
        <CapabilityLegend declared={declared} />
        <p className="text-meta text-muted-foreground">
          In a hurry? “Add now with these settings” adds what is ticked
          {declared ? `, the model name “${speech?.public_name ?? ""}”,` : ", every model the server listed,"} and the name “
          {suggested}”.
        </p>
      </Frame>
    );
  }

  if (step === "detail" && flow === "workflow" && wf.read) {
    const missing = inputsMissing(wf.read, wf.inputs) || !wf.publicName.trim();
    const cap = capabilityForMakes(wf.makes);
    const test = tests[cap];
    return (
      <Frame
        step={4}
        stepName={stepName}
        footer={
          <FooterRow
            left={<BackButton onClick={back} />}
            right={
              <>
                {addNowButton(missing)}
                <Button type="button" size="md" disabled={missing} onClick={next}>
                  Continue
                </Button>
              </>
            }
          />
        }
      >
        {errorCallout}
        <StepInputs
          read={wf.read}
          inputs={wf.inputs}
          onInputs={(inputs) => setWf((w) => ({ ...w, inputs }))}
          publicField={fieldOf(def, "workflow_public_name")}
          publicName={wf.publicName}
          publicSuggested={wf.publicSuggested}
          onPublicName={(publicName) => setWf((w) => ({ ...w, publicName, publicSuggested: false }))}
        />
        <div className="flex flex-col gap-2">
          <p className="text-meta font-medium">Real test</p>
          <p className="text-meta text-muted-foreground">
            Azizam sends your workflow with one input set to a value the server must refuse. The server reads every node, then stops
            before it queues anything. Nothing is generated and nothing is saved.
          </p>
          {test ? (
            <TestResultBlock test={test} label="the workflow" note={DRAFT_TEST_NOTE} onTestAgain={() => void runTest(cap)} />
          ) : (
            <Button type="button" variant="outline" size="sm" className="w-fit" disabled={missing} onClick={() => void runTest(cap)}>
              Run a real test
            </Button>
          )}
        </div>
        <p className="text-meta text-muted-foreground">
          In a hurry? “Add now with these settings” adds this workflow with the inputs above, under the name “{suggested}”.
        </p>
      </Frame>
    );
  }

  if (step === "detail" && flow === "declared" && speech) {
    const withVoices = Boolean(fieldOf(def, "voices"));
    return (
      <Frame
        step={4}
        stepName={stepName}
        footer={
          <FooterRow
            left={<BackButton onClick={back} />}
            right={
              <Button type="button" size="md" disabled={!speech.public_name.trim()} onClick={next}>
                Continue
              </Button>
            }
          />
        }
      >
        <div>
          <h3 className="text-heading">{withVoices ? "Name the model and its voices" : "Name the model"}</h3>
          <p className="text-meta text-muted-foreground mt-0.5">
            This kind of server has one model, so there is no list to pick from. Apps choose it by the public name.
          </p>
        </div>
        <Note>
          Prefilled values <Tag tone="suggest">Suggested</Tag> work as they are. Change one only if you know better.
        </Note>
        <SpeechFields definition={def} draft={speech} onChange={setSpeech} />
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
          <h3 className="text-heading">Models and limits</h3>
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
        <h3 className="text-heading">Name and create</h3>
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
    </Frame>
  );

  function recapRows(): [string, string][] {
    if (!def) return [];
    const rows: [string, string][] = [["Kind", def.title], ["Address", conn.address.trim()]];
    if (declared) {
      rows.push(["Path", conn.path.trim()]);
      rows.push(["Azizam will call", `${def.check_request.method} ${joinUrl(conn.address, conn.path.trim())}`]);
    }
    rows.push([
      "Key",
      owner && conn.envOpen && conn.envName.trim()
        ? "Read from a server variable"
        : owner && conn.key
          ? "Stored on this gateway. Never shown again."
          : "None. The server has no key.",
    ]);
    if (flow === "workflow" && wf.read) {
      const used = wf.read.inputs.filter((r) => wf.inputs[r.key]).map((r) => r.label);
      const unused = wf.read.inputs.filter((r) => !wf.inputs[r.key]).map((r) => r.label);
      const t = tests[capabilityForMakes(wf.makes)];
      rows.push(["Workflow", `${wf.read.file} (${wf.read.node_count} nodes)`]);
      rows.push([fieldOf(def, "workflow_public_name")?.label ?? "Name apps use", wf.publicName.trim()]);
      rows.push(["Will offer", wf.makes === "video" ? "Video" : "Images"]);
      rows.push(["Inputs filled", used.join(", ") || "None"]);
      if (unused.length) rows.push(["Not used", unused.join(", ")]);
      rows.push(["Tested", t?.status === "done" && t.result.ok ? `Workflow checked at ${clockTime(t.result.at)}` : "Not tested"]);
      return rows;
    }
    rows.push(["Will offer", tickedLabels().join(", ") || "Nothing yet"]);
    if (flow === "declared" && speech) {
      rows.push(["Model", `${speech.public_name.trim()}${speech.server_model.trim() ? `, which the server calls “${speech.server_model.trim()}”` : ""}`]);
      if (fieldOf(def, "voices")) rows.push(["Voices", speech.voices.join(", ") || "None listed"]);
      const t = Object.values(tests).find((x) => x.status === "done");
      rows.push(["Tested", t?.status === "done" && t.result.ok ? `Passed at ${clockTime(t.result.at)}` : "Not tested"]);
      return rows;
    }
    const on = models.filter((m) => m.on).length;
    const added = models.filter((m) => m.on && m.byName).length;
    rows.push(["Models", `${on} offered${added ? ` (${on - added} the server listed, ${added} you added)` : ""}`]);
    return rows;
  }
}

function Frame({ step, stepName, footer, children }: { step: number; stepName: string; footer: ReactNode; children: ReactNode }) {
  return (
    <DialogContent aria-describedby={undefined} className="flex! max-h-[85dvh] flex-col gap-0! overflow-hidden p-0 sm:max-w-2xl">
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
      <div data-slot="dialog-body" className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-4">
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
  index,
  revealed,
  declared,
}: {
  definition: EngineDefinition;
  index: number;
  revealed: CheckResult | null;
  declared: boolean;
}) {
  return (
    <>
      <div>
        <h3 className="text-heading">Checking the server</h3>
        <p className="text-meta text-muted-foreground mt-0.5">This takes a few seconds.</p>
      </div>
      <ul role="status" aria-live="polite" aria-label="Check progress" className="bg-card rounded-panel divide-y border">
        {definition.check_rows.map((label, i) => {
          const result = revealed?.steps.find((s) => s.label === label)?.result ?? "";
          const state = revealed ? "done" : i < index ? "done" : i === index ? "now" : "wait";
          return (
            <li key={label} className="text-meta flex items-center gap-2 px-4 py-2.5">
              {state === "done" ? (
                <CircleCheck aria-hidden="true" className="text-success size-4" />
              ) : state === "now" ? (
                <Loader2 aria-hidden="true" className="size-4 animate-spin" />
              ) : (
                <Circle aria-hidden="true" className="text-muted-foreground size-4" />
              )}
              <span className={cn("flex-1", state === "wait" && "text-muted-foreground")}>
                {label}
                {state === "now" && "…"}
              </span>
              {state === "done" && result && <span className="text-muted-foreground">{result}</span>}
            </li>
          );
        })}
      </ul>
      <Note tone="info">
        {declared
          ? "Azizam sends one empty request to the path. Nothing is generated and nothing is saved yet."
          : "Azizam sends only lists and empty requests. Nothing is generated and nothing is saved yet."}
      </Note>
    </>
  );
}

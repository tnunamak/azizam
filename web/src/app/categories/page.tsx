"use client";

import { useContext, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Layers, Lock } from "lucide-react";
import { adminFetch, ApiError, api } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { Callout, StatusLine } from "@/components/product/status-ui";

type Category = { id: string; label: string; values: string[]; applies_to: string[] };
type Rule = { id: string; category: string; op: string; values: string[] };
type Grant = { capability: string; service: string; release?: string; workflow?: string };
type Plan = { id: string; grants: Grant[]; require: Rule[]; deny: Rule[]; missing: Record<string, string>; mode: string; legacy_tags: string[] };
type Assertion = { category: string; value: unknown; source: string; accepted: boolean; accepted_at: string; observed_at: string; accepting_owner: string };
type Member = { id: string; kind: string; assertions: Assertion[] };
type Service = { id: string; members: Member[]; dependencies: Service[]; checkable: boolean };
type Snapshot = { id: string; categories: Record<string, Category>; people: Record<string, Plan[]>; apps: Record<string, Plan[]>; services: Record<string, Service>; retained: Record<string, Service> };
type Revision = { revision: number; parent_revision: number | null; created_at: string; activated_at?: string | null; author: string; reason: string; snapshot: Snapshot };
const root = "/admin/api/categories";
const plain = (value: string) => value.replace(/[_-]/g, " ").replace(/^./, (letter) => letter.toUpperCase());
const capabilityNames: Record<string, string> = { "chat": "Chat", "chat.completions": "Chat", "image.generate": "Images", "video.generate": "Video", "speech.synthesis": "Voice", "speech.transcription": "Transcription", "music.generate": "Music", "sound.generate": "Sound effects", "embed": "Embeddings" };
const capabilityName = (value: string) => capabilityNames[value] ?? value;
function time(value?: string | null) {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? date.toLocaleString() : "Time not recorded";
}
function ruleText(rule: Rule, categories: Snapshot["categories"]) {
  const category = categories[rule.category]?.label ?? rule.category;
  if (rule.op === "exists") return category + " has an accepted value";
  if (rule.op === "missing") return category + " has no accepted value";
  return category + ": " + rule.values.map(plain).join(" or ");
}
function rules(service: Service) {
  const seen = new Set<string>();
  const members: Member[] = [];
  function visit(item: Service) {
    if (seen.has(item.id)) return;
    seen.add(item.id);
    members.push(...item.members);
    item.dependencies.forEach(visit);
  }
  visit(service);
  return members.filter((member, index) => members.findIndex((item) => item.kind === member.kind && item.id === member.id) === index);
}
async function loadActive() {
  try {
    return await adminFetch<Revision>(root + "/active");
  } catch (error) {
    if (error instanceof ApiError && error.code === "no_active_snapshot") return null;
    throw error;
  }
}

function CurrentRules({ active, personName, appName }: { active: Revision; personName: (id: string) => string; appName: (id: string) => string }) {
  const snapshot = active.snapshot;
  const assigned = [
    ...Object.entries(snapshot.people).map(([id, plans]) => ({ key: "person:" + id, label: "Person: " + personName(id), plans })),
    ...Object.entries(snapshot.apps).map(([id, plans]) => ({ key: "app:" + id, label: "App: " + appName(id), plans })),
  ];
  return <section aria-labelledby="current-rules" className="space-y-3">
    <h2 id="current-rules" className="text-heading">Current rules</h2>
    <div className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
      <p className="text-body font-medium">Revision {active.revision}</p>
      <p className="text-meta text-muted-foreground">{active.activated_at ? "Turned on: " + time(active.activated_at) : "Activation time not recorded"}</p>
      <dl className="space-y-3">
        {Object.values(snapshot.categories).map((category) => <div key={category.id}>
          <dt className="text-body font-medium">{category.label}</dt>
          <dd className="text-meta">{category.values.map(plain).join(", ") || "No values"}</dd>
        </div>)}
      </dl>
      {!Object.keys(snapshot.categories).length && <p className="text-meta">No categories in this revision.</p>}
    </div>
    {assigned.map(({ key, label, plans }) => <div key={key} className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
      <p className="text-body font-medium">{label}</p>
      {!plans.length && <p className="text-meta">No access plans attached. The preview denies access.</p>}
      {plans.map((plan) => <div key={plan.id} className="space-y-2 text-meta">
        <p className="text-body font-medium">Access plan: {plain(plan.id)}</p>
        {plan.mode === "legacy" && <p>Legacy tag rules: any matching tag passes. Tags: {plan.legacy_tags.map(plain).join(", ") || "None"}.</p>}
        {plan.grants.map((grant, index) => <p key={index}>Allows {capabilityName(grant.capability)} with {grant.service}{grant.release ? " · Model release: " + grant.release : ""}{grant.workflow ? " · Workflow: " + grant.workflow : ""}</p>)}
        {!plan.grants.length && <p>No grants. This access plan allows no services.</p>}
        {plan.require.map((row) => <p key={row.id}>Needs {ruleText(row, snapshot.categories)} <span className="text-muted-foreground">(rule: {row.id})</span></p>)}
        {plan.deny.map((row) => <p key={row.id}>Blocks when {ruleText(row, snapshot.categories)} <span className="text-muted-foreground">(rule: {row.id})</span></p>)}
        {Object.entries(plan.missing).filter(([, value]) => value === "allow").map(([id]) => <p key={id}>Missing-value waiver for {id}: skips only this requirement when its value is missing.</p>)}
      </div>)}
    </div>)}
    {[...Object.entries(snapshot.services).map(([id, service]) => ({ key: "service:" + id, service, output: "" })), ...Object.entries(snapshot.retained).map(([id, service]) => ({ key: "output:" + id, service, output: id }))].map(({ key, service, output }) => <div key={key} className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
      <p className="text-body font-medium">Service: {service.id}{output ? " · Retained output: " + output : ""}</p>
      {!service.checkable && <p className="text-meta">The gateway cannot check every dependency. The preview denies access.</p>}
      {rules(service).map((member) => <div key={member.kind + ":" + member.id} className="space-y-2">
        <p className="text-body font-medium">{plain(member.kind)}: {member.id}</p>
        {member.assertions.filter((assertion) => assertion.accepted).map((assertion, index) => <div key={index} className="text-meta">
          <p>{snapshot.categories[assertion.category]?.label ?? assertion.category}: {Array.isArray(assertion.value) ? assertion.value.map((value) => plain(String(value))).join(", ") : plain(String(assertion.value))}</p>
          <p className="text-muted-foreground">Source: {assertion.source || "Not recorded"}{assertion.accepted_at ? " · Accepted: " + assertion.accepted_at : ""}{assertion.accepting_owner ? " · By: " + personName(assertion.accepting_owner) : ""}{assertion.observed_at ? " · Observed: " + assertion.observed_at : ""}</p>
        </div>)}
        {!member.assertions.some((assertion) => assertion.accepted) && <p className="text-meta">No accepted values.</p>}
      </div>)}
    </div>)}
  </section>;
}

export default function CategoriesPage() {
  const viewer = useContext(ViewerContext);
  if (!viewer?.authenticated || viewer.role !== "owner") return <div className="space-y-6">
    <PageHeader title="Categories" />
    <EmptyState icon={Lock} title="Only the owner can manage categories" description="Ask the gateway owner to review these rules." />
  </div>;
  return <OwnerCategories />;
}

type Decision = { outcome: "allow" | "deny"; reason: string; rule: string; category: string; member: string; waivers: [string, string, string][] };
type PolicyRequest = { person: string; app: string; capability: string; service: string; output?: string; phase?: string; tags?: string[] | null; metadata_complete?: boolean };
type ChangePreview = { gains: PolicyRequest[]; losses: PolicyRequest[]; reason_changes: { request: PolicyRequest; before: Decision; after: Decision }[]; scope: PolicyRequest[] };
type Candidate = Revision & { preview: ChangePreview; preview_base_revision: number | null; restored_from_revision: number };
type HistoryEntry = { sequence: number; action: "activate" | "restore"; revision: number; previous_revision: number | null; source_revision: number | null; active_counter: number; created_at: string; author: string; reason: string };

// The kernel's complete stable reason-code vocabulary, with one plain fallback.
const reasonSentences: Record<string, string> = {
  ALLOW: "Every attached access plan allows this request.",
  NO_PLANS: "The person or app has no access plan.",
  NO_GRANT: "An access plan does not grant this capability with this service.",
  EXPLICIT_DENY: "A blocking rule matches this member.",
  DENY_INPUT_UNKNOWN: "A blocking rule cannot be checked because an accepted value is missing.",
  REQUIRE_MISMATCH: "An accepted value does not meet a required rule.",
  REQUIRE_MISSING: "An accepted value needed by a required rule is missing.",
  NO_APPLICABLE_SUBJECT: "There is no applicable member to check for this rule.",
  METADATA_INCOMPLETE: "The gateway is missing information needed to check this request.",
  LEGACY_TAG_MISMATCH: "The service tags do not match the legacy access plan.",
  IDENTITY_UNAVAILABLE: "The gateway could not identify the person or app.",
  POLICY_UNAVAILABLE: "The gateway could not check the category rules.",
};
const reasonSentence = (code: string) => Object.prototype.hasOwnProperty.call(reasonSentences, code) ? reasonSentences[code] : "The gateway did not provide a recognized reason for this result.";

function DecisionDetails({ decision }: { decision: Decision }) {
  return <div className="space-y-2 text-meta">
    <p>{reasonSentence(decision.reason)}</p>
    {decision.outcome === "deny" && <dl className="space-y-1">
      <div><dt className="inline text-muted-foreground">Failing rule: </dt><dd className="inline">{decision.rule || "Not reported"}</dd></div>
      <div><dt className="inline text-muted-foreground">Member: </dt><dd className="inline">{decision.member || "Not reported"}</dd></div>
      {decision.category && <div><dt className="inline text-muted-foreground">Category: </dt><dd className="inline">{decision.category}</dd></div>}
    </dl>}
    {decision.waivers.map(([row, category, member], index) => <p key={index}>Missing-value waiver used: {row} · {category} · {member}</p>)}
  </div>;
}

function PreviewAs({ active, personName, appName }: { active: Revision; personName: (id: string) => string; appName: (id: string) => string }) {
  const [selection, setSelection] = useState({ person: "", app: "", capability: "", service: "" });
  const [submitted, setSubmitted] = useState<typeof selection | null>(null);
  const [validation, setValidation] = useState("");
  const snapshot = active.snapshot;
  const capabilities = Array.from(new Set([...Object.values(snapshot.people), ...Object.values(snapshot.apps)].flatMap((plans) => plans.flatMap((plan) => plan.grants.map((grant) => grant.capability))))).sort();
  const { data: previewData, isError: previewIsError, isFetching: previewIsFetching, refetch: previewRefetch } = useQuery({
    queryKey: ["categories", "preview", active.revision, submitted],
    queryFn: () => adminFetch<Decision>(root + "/preview-as", { method: "POST", body: { ...submitted, revision: active.revision } }),
    enabled: submitted !== null,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const fields: { key: keyof typeof selection; label: string; options: { value: string; label: string }[] }[] = [
    { key: "person", label: "Person", options: Object.keys(snapshot.people).map((value) => ({ value, label: personName(value) })) },
    { key: "app", label: "App", options: Object.keys(snapshot.apps).map((value) => ({ value, label: appName(value) })) },
    { key: "capability", label: "Capability", options: capabilities.map((value) => ({ value, label: capabilityName(value) })) },
    { key: "service", label: "Service", options: Object.keys(snapshot.services).map((value) => ({ value, label: value })) },
  ];
  return <section aria-labelledby="preview-as" className="space-y-3">
    <h2 id="preview-as" className="text-heading">Preview as</h2>
    <form className="rounded-panel border bg-card p-4 sm:p-6 space-y-4" onSubmit={(event) => {
      event.preventDefault();
      if (Object.values(selection).some((value) => !value)) {
        setValidation("Choose a person, an app, a capability and a service.");
        return;
      }
      setValidation("");
      if (submitted) void previewRefetch();
      else setSubmitted({ ...selection });
    }}>
      <div className="grid gap-4 sm:grid-cols-2">
        {fields.map(({ key, label, options }) => <div key={key} className="space-y-2">
          <label className="text-label" htmlFor={"preview-" + key}>{label}</label>
          <Select value={selection[key]} onValueChange={(value) => {
            setSelection((current) => ({ ...current, [key]: value }));
            setSubmitted(null);
            setValidation("");
          }}>
            <SelectTrigger id={"preview-" + key} className="w-full"><SelectValue placeholder={"Choose " + label.toLowerCase()} /></SelectTrigger>
            <SelectContent>{options.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
          </Select>
        </div>)}
      </div>
      {fields.some((field) => !field.options.length) && <p className="text-meta">This revision has no complete set of people, apps, grants and services to preview.</p>}
      {validation && <p role="alert" className="text-meta">{validation}</p>}
      <Button size="md" type="submit" disabled={previewIsFetching}>Check access</Button>
      {previewIsFetching && <StatusLine tone="unknown" text="Checking access" compact />}
      {submitted && previewIsError && <Callout as="div" severity="critical" title="Couldn't check access"><p>Try checking again.</p></Callout>}
      {submitted && previewData && !previewIsFetching && !previewIsError && <div role="status" aria-live="polite" aria-atomic="true" className="space-y-2">
        <p className="text-body font-medium">{previewData.outcome === "allow" ? "Allow" : "Deny"} · Revision {active.revision}</p>
        <DecisionDetails decision={previewData} />
      </div>}
    </form>
  </section>;
}

function OwnerCategories() {
  const { data: activeData, isError: activeIsError, isFetching: activeIsFetching, isPending: activeIsPending, refetch: activeRefetch } = useQuery({ queryKey: ["categories", "active"], queryFn: loadActive, retry: false, refetchOnWindowFocus: false });
  const { data: historyData, isError: historyIsError, isFetching: historyIsFetching, isPending: historyIsPending, refetch: historyRefetch } = useQuery({ queryKey: ["categories", "history"], queryFn: () => adminFetch<{ history: HistoryEntry[] }>(root + "/history"), retry: false, refetchOnWindowFocus: false });
  const { data: peopleData, isError: peopleIsError } = useQuery({ queryKey: ["users"], queryFn: api.users.list });
  const { data: appsData, isError: appsIsError } = useQuery({ queryKey: ["apps"], queryFn: api.apps.list });
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [restoreReason, setRestoreReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ error: boolean; text: string } | null>(null);
  const personName = (id: string) => {
    const person = peopleData?.find((item) => item.id === id);
    return person?.display_name || person?.name || person?.email || id;
  };
  const appName = (id: string) => appsData?.find((item) => item.id === id)?.name || id;
  const requestName = (request: PolicyRequest) => personName(request.person) + " · " + appName(request.app) + " · " + capabilityName(request.capability) + " · " + request.service + (request.output ? " · Output: " + request.output : "") + (request.phase ? " · " + plain(request.phase) : "");
  const knownBase = !activeIsPending && !activeIsError && activeData !== undefined;
  const baseRevision = activeData?.revision ?? null;
  const refresh = async () => { await Promise.all([activeRefetch(), historyRefetch()]); };
  async function change(action: "restore" | "activate", revision: number) {
    if (busy || !knownBase || activeIsFetching) return;
    if (action === "activate" && (!candidate || candidate.preview_base_revision !== baseRevision)) {
      setCandidate(null);
      setMessage({ error: true, text: "The active rules changed. Restore again to review a new preview." });
      await refresh();
      return;
    }
    setBusy(true);
    setMessage(null);
    if (action === "restore") setCandidate(null);
    try {
      if (action === "restore") {
        const result = await adminFetch<Candidate>(root + "/revisions/" + revision + "/restore", {
          method: "POST",
          body: { base_revision: baseRevision, reason: restoreReason.trim() || "Restore revision " + revision },
        });
        setCandidate(result);
        await historyRefetch();
      } else {
        await adminFetch(root + "/candidates/" + revision + "/activate", {
          method: "POST",
          body: { expected_active_revision: candidate!.preview_base_revision },
        });
        setCandidate(null);
        setMessage({ error: false, text: "Revision " + revision + " is now selected for category previews." });
        await refresh();
      }
    } catch (error) {
      setCandidate(null);
      if (error instanceof ApiError && error.code === "stale_base") {
        setMessage({ error: true, text: "The active rules changed. Restore again to review a new preview." });
        await refresh();
      } else {
        setMessage({ error: true, text: action === "restore" ? "Couldn't prepare the restore. Refresh the rules and try again." : "Couldn't confirm whether the revision was turned on. Refresh the rules before trying again." });
        if (action === "activate") await refresh();
      }
    } finally {
      setBusy(false);
    }
  }
  const candidateMatches = candidate && candidate.preview_base_revision === baseRevision;
  return <div className="max-w-(--content-read) space-y-8">
    <PageHeader title="Categories" subtitle="Review category rules before they apply to requests" />
    <p className="text-meta text-muted-foreground">Nothing is enforced by this page yet. Request enforcement comes later; existing access plans still apply.</p>
    <Button variant="outline" disabled={busy || activeIsFetching || historyIsFetching} onClick={() => { setCandidate(null); void refresh(); }}>Refresh</Button>
    {message && (message.error ? <Callout as="div" severity="critical" title={message.text} /> : <p role="status" className="text-meta">{message.text}</p>)}
    {activeIsPending && <div aria-busy="true" aria-label="Loading category rules"><StatusLine tone="unknown" text="Loading category rules" compact /></div>}
    {activeIsError && <Callout as="div" severity="critical" title={activeData ? "Couldn't refresh category rules" : "Couldn't load category rules"} headerAction={<Button variant="outline" onClick={() => void activeRefetch()}>Try again</Button>}>
      {activeData && <p>The rules already loaded are still shown. Changes are paused until they can be refreshed.</p>}
    </Callout>}
    {activeData === null && !activeIsError && <EmptyState icon={Layers} title="No category rules are active" description="There are no rules to preview yet. A previous revision can be restored from History." />}
    {activeData && <>
      <CurrentRules active={activeData} personName={personName} appName={appName} />
      <PreviewAs key={activeData.revision} active={activeData} personName={personName} appName={appName} />
    </>}
    {(peopleIsError || appsIsError) && <p className="text-meta text-muted-foreground">Some names couldn&rsquo;t be loaded. Their saved IDs are shown instead.</p>}
    <section aria-labelledby="category-history" className="space-y-3">
      <h2 id="category-history" className="text-heading">History</h2>
      {historyIsPending && <div aria-busy="true" aria-label="Loading category history"><StatusLine tone="unknown" text="Loading category history" compact /></div>}
      {historyIsError && <Callout as="div" severity="critical" title="Couldn't load category history" headerAction={<Button variant="outline" onClick={() => void historyRefetch()}>Try again</Button>} />}
      {historyData && <>
        {historyData.history.length > 0 && <div className="space-y-2">
          <label htmlFor="restore-reason" className="text-label">Reason for restore (optional)</label>
          <Input id="restore-reason" value={restoreReason} onChange={(event) => setRestoreReason(event.target.value)} disabled={busy} />
        </div>}
        <div className="divide-y rounded-panel border bg-card">
          {[...historyData.history].reverse().map((entry) => <div key={entry.sequence} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
            <div className="min-w-0 space-y-1">
              <p className="text-body font-medium">Revision {entry.revision} · {entry.action === "activate" ? "Turned on" : "Restore prepared"}{entry.revision === baseRevision && entry.action === "activate" ? " · Current" : ""}</p>
              <p className="text-meta text-muted-foreground">{personName(entry.author)} · {time(entry.created_at)}</p>
              <p className="text-meta break-words">{entry.reason}</p>
              {entry.source_revision !== null && <p className="text-meta">Restored from revision {entry.source_revision}</p>}
            </div>
            <Button variant="outline" aria-label={"Restore revision " + entry.revision} disabled={busy || !knownBase || activeIsFetching} onClick={() => void change("restore", entry.revision)}>Restore</Button>
          </div>)}
          {!historyData.history.length && <EmptyState inCard icon={Layers} title="No category history yet" description="Restores and revisions turned on will appear here." />}
        </div>
      </>}
    </section>
    {candidate && <section aria-labelledby="change-preview" className="space-y-3">
      <h2 id="change-preview" className="text-heading">Change preview</h2>
      <div className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
        <p className="text-body font-medium">Restore revision {candidate.restored_from_revision} as revision {candidate.revision}</p>
        <p className="text-meta">Compared with {candidate.preview_base_revision === null ? "no active revision" : "revision " + candidate.preview_base_revision}. Nothing has been turned on.</p>
        <p className="text-meta text-muted-foreground">This compares {candidate.preview.scope.length} known requests. It does not cover unknown future requests.</p>
        {(["gains", "losses"] as const).map((kind) => <div key={kind} className="space-y-2">
          <p className="text-body font-medium">{kind === "gains" ? "Who gains access" : "Who loses access"}</p>
          {candidate.preview[kind].length ? <ul className="space-y-2 text-meta">{candidate.preview[kind].map((request, index) => <li key={index}>{requestName(request)}</li>)}</ul> : <p className="text-meta">None in the compared requests.</p>}
        </div>)}
        <div className="space-y-2">
          <p className="text-body font-medium">Reasons that change</p>
          {!candidate.preview.reason_changes.length && <p className="text-meta">None in the compared requests.</p>}
          {candidate.preview.reason_changes.map((item, index) => <div key={index} className="space-y-2 border-t pt-3">
            <p className="text-meta font-medium">{requestName(item.request)}</p>
            <p className="text-meta">Before: {plain(item.before.outcome)}</p><DecisionDetails decision={item.before} />
            <p className="text-meta">After: {plain(item.after.outcome)}</p><DecisionDetails decision={item.after} />
          </div>)}
        </div>
        <details className="text-meta"><summary>Compared requests</summary><ul className="mt-3 space-y-2">{candidate.preview.scope.map((request, index) => <li key={index}>{requestName(request)}</li>)}</ul></details>
        {!candidateMatches && <p role="alert" className="text-meta">The active rules changed. Restore again to review a new preview.</p>}
        <div className="flex flex-wrap gap-3">
          <Button disabled={busy || !knownBase || activeIsFetching || !candidateMatches} onClick={() => void change("activate", candidate.revision)}>Turn on</Button>
          <Button variant="outline" disabled={busy} onClick={() => setCandidate(null)}>Close preview</Button>
        </div>
      </div>
    </section>}
    {busy && <StatusLine tone="unknown" text="Saving category revision" compact />}
  </div>;
}

"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ListCard, ListRow } from "@/components/product/list-card";
import { StateBadge } from "@/components/product/status-ui";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { isNetworkFailure, NETWORK_FAILURE_MESSAGE } from "@/lib/network-error";
import { ownerApi, OwnerApiError, type EntryProblem } from "./owner-api";

type OwnerListRead = Awaited<ReturnType<typeof ownerApi.list>>;
type OwnerDetailRead = Awaited<ReturnType<typeof ownerApi.get>>;
type WorkflowDraft = Parameters<typeof ownerApi.check>[0];
type WorkflowFacts = NonNullable<WorkflowDraft["facts"]>;
type WorkflowProblem = OwnerApiError["problems"][number];
type FactKey = "exposure" | "trust_tier" | "resource_tier" | "resource_class";
type Selection = { id: string | null; serial: number; list: OwnerListRead };
type DraftState = {
  json: string;
  facts: WorkflowFacts;
  factsMode: "keep" | "set" | "clear";
  overrides?: string | null;
};

const OWNER_LIST_KEY = ["workflows", "owner"] as const;
const FACT_FIELDS: { key: FactKey; label: string }[] = [
  { key: "exposure", label: "Exposure" },
  { key: "trust_tier", label: "Trust tier" },
  { key: "resource_tier", label: "Resource tier" },
  { key: "resource_class", label: "Resource class" },
];

function emptyFacts(): WorkflowFacts {
  return { exposure: "", trust_tier: "", resource_tier: "", resource_class: "", tags: [] };
}

function draftFrom(detail?: OwnerDetailRead): DraftState {
  return {
    json: detail ? JSON.stringify(detail.data.contract, null, 2) : "{}",
    facts: detail?.data.facts ? { ...detail.data.facts, tags: [...detail.data.facts.tags] } : emptyFacts(),
    factsMode: "keep",
    // A bundled workflow is replaced by saying so; its source is the release itself.
    overrides: detail ? detail.data.overrides ?? (detail.data.provenance.source === "local" ? detail.data.id : null) : null,
  };
}

function messageFor(error: unknown, fallback: string): string {
  if (isNetworkFailure(error)) return NETWORK_FAILURE_MESSAGE;
  if (error instanceof OwnerApiError) return error.message;
  return fallback;
}

function problemPath(problem: WorkflowProblem): string {
  return problem.pointer || "document";
}

function Problems({ problems }: { problems: readonly WorkflowProblem[] }) {
  if (!problems.length) return null;
  return (
    <ul aria-label="Workflow problems" className="space-y-2 text-meta">
      {problems.map((problem, index) => (
        <li key={`${problemPath(problem)}-${index}`} className="break-words">
          <span className="font-mono font-medium">{problemPath(problem)}</span>
          {": "}{problem.message}
        </li>
      ))}
    </ul>
  );
}

function EntryProblems({ problems }: { problems: readonly EntryProblem[] }) {
  if (!problems.length) return null;
  return <span className="break-words">Not ready: {problems.map((problem) => problem.code.replaceAll("_", " ")).join(", ")}</span>;
}

/** Do not even mount the owner queries for a member or a delegated admin. */
export function OwnerView({ owner }: { owner: boolean }) {
  return owner ? <OwnerManagement /> : null;
}

function OwnerManagement() {
  const [selection, setSelection] = useState<Selection | null>(null);
  const sequence = useRef(0);
  const list = useQuery({
    queryKey: OWNER_LIST_KEY,
    queryFn: ({ signal }) => ownerApi.list(signal),
    retry: false,
  });
  const open = (id: string | null) => {
    if (list.data) setSelection({ id, serial: ++sequence.current, list: list.data });
  };
  return (
    <section aria-label="Manage workflows" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-heading">Manage workflows</h2>
          <p className="text-muted-foreground text-meta">Edit, publish and manage access to workflows.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => void list.refetch()} disabled={list.isFetching}>
            Refresh owner workflows
          </Button>
          <Button onClick={() => open(null)} disabled={!list.data || list.isFetching || list.isError}>New workflow</Button>
        </div>
      </div>
      {list.isPending ? <p role="status">Loading owner workflows…</p> : list.isError ? (
        <p role="alert">{messageFor(list.error, "Owner workflows could not be loaded. Try again.")}</p>
      ) : list.data ? (
        list.data.data.workflows.length ? (
          <ListCard label="Owner workflows">
            {list.data.data.workflows.map((entry) => (
              <ListRow
                key={entry.id}
                lines={2}
                className="[&_p]:whitespace-normal [&_p]:overflow-visible [&_p]:text-clip"
                title={<button type="button" aria-label={`Manage ${entry.id}`} className="break-all text-left hover:underline" onClick={() => open(entry.id)}>{entry.id}</button>}
                badge={<StateBadge state={entry.state === "active" ? "ready" : "degraded"} word={entry.state === "active" ? "Enabled" : "Disabled"} />}
                meta={<><span>{entry.published ? "Published" : "Unpublished"} · Revision {entry.record_revision}</span>{entry.entry_problems.length ? <span className="mt-1 block"><EntryProblems problems={entry.entry_problems} /></span> : null}</>}
              />
            ))}
          </ListCard>
        ) : <p className="text-muted-foreground text-meta">No workflows yet. Add a workflow document to get started.</p>
      ) : null}
      {selection && (
        <OwnerSheet
          key={selection.serial}
          selection={selection}
          list={list.data ?? selection.list}
          onClose={() => setSelection(null)}
          onCreated={(id, latestList) => setSelection({ id, serial: ++sequence.current, list: latestList })}
        />
      )}
    </section>
  );
}

function OwnerSheet({
  selection,
  list,
  onClose,
  onCreated,
}: {
  selection: Selection;
  list: OwnerListRead;
  onClose: () => void;
  onCreated: (id: string, latestList: OwnerListRead) => void;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["workflows", "owner-detail", selection.id, selection.serial] as const;
  const detail = useQuery({
    queryKey,
    queryFn: ({ signal }) => ownerApi.get(selection.id!, signal),
    enabled: selection.id !== null,
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const [snapshot, setSnapshot] = useState<OwnerDetailRead | null>(null);
  const [createEtag, setCreateEtag] = useState(selection.list.etag);
  const [draft, setDraft] = useState(() => draftFrom());
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<readonly WorkflowProblem[]>([]);
  const [conflict, setConflict] = useState(false);
  const [reloadRequired, setReloadRequired] = useState(false);
  const alive = useRef(true);
  const busy = useRef(false);
  const hydrated = useRef(false);
  const operation = useRef(0);
  const draftVersion = useRef(0);
  useEffect(() => {
    alive.current = true;
    const ops = operation;
    return () => { alive.current = false; ops.current++; };
  }, []);
  useEffect(() => {
    if (detail.data && !hydrated.current) {
      hydrated.current = true;
      setSnapshot(detail.data);
      setDraft(draftFrom(detail.data));
    }
  }, [detail.data]);

  const ready = selection.id === null || snapshot !== null;
  const dirty = snapshot !== null && (draft.json !== JSON.stringify(snapshot.data.contract, null, 2) || draft.factsMode !== "keep");
  const choices = list.data.choices;
  const incompleteFacts = draft.factsMode === "set" && FACT_FIELDS.some(({ key }) => !choices[key].includes(draft.facts[key]));
  const disabled = !!pending || !ready || conflict || reloadRequired;
  const current = (token: number) => alive.current && operation.current === token;
  const begin = (label: string) => {
    if (busy.current) return null;
    busy.current = true;
    setPending(label);
    setNotice(null);
    setError(null);
    setProblems([]);
    return ++operation.current;
  };
  const finish = (token: number) => {
    if (!current(token)) return;
    busy.current = false;
    setPending(null);
  };
  const edit = (next: DraftState) => {
    draftVersion.current++;
    setDraft(next);
    setProblems([]);
    setNotice(null);
    setError(null);
  };
  const requestDraft = (): WorkflowDraft | null => {
    let contract: unknown;
    try { contract = JSON.parse(draft.json); } catch {
      setError("The workflow document is not valid JSON. Fix it before checking or saving.");
      return null;
    }
    if (!contract || typeof contract !== "object" || Array.isArray(contract)) {
      setError("The workflow document must be a JSON object.");
      return null;
    }
    if (incompleteFacts) {
      setError("Choose a value for each fact before checking or saving.");
      return null;
    }
    return {
      contract: contract as Record<string, unknown>,
      ...(draft.factsMode === "clear" ? { facts: null } : draft.factsMode === "set" ? { facts: draft.facts } : {}),
      ...(draft.overrides != null ? { overrides: draft.overrides } : {}),
    };
  };
  const refreshLists = () => {
    void queryClient.invalidateQueries({ queryKey: ["workflows", "catalog"] });
    return queryClient.fetchQuery({ queryKey: OWNER_LIST_KEY, queryFn: ({ signal }) => ownerApi.list(signal), staleTime: 0 });
  };
  const applyDetail = (latest: OwnerDetailRead) => {
    setSnapshot(latest);
    setDraft(draftFrom(latest));
    draftVersion.current++;
    queryClient.setQueryData(queryKey, latest);
    setReloadRequired(false);
    setConflict(false);
  };

  const handleFailure = async (failure: unknown, token: number) => {
    if (!current(token)) return;
    if (failure instanceof OwnerApiError && failure.status === 412) {
      setConflict(true);
      setNotice("The workflow changed since it was opened. Your edits are still shown. Reload latest before saving.");
      // Read both views immediately, but never replay the rejected mutation or
      // silently combine the old draft with the new revision's write token.
      const [newList, newDetail] = await Promise.allSettled([
        refreshLists(),
        selection.id !== null ? ownerApi.get(selection.id) : Promise.resolve(null),
      ]);
      if (!current(token)) return;
      if (newList.status === "fulfilled") setCreateEtag(newList.value.etag);
      if (newDetail.status === "fulfilled" && newDetail.value) setSnapshot(newDetail.value);
      if (newList.status === "rejected" || newDetail.status === "rejected") {
        setError("The latest version could not be loaded. Try Reload latest again.");
      }
      return;
    }
    if (failure instanceof OwnerApiError) {
      setProblems(failure.problems);
      if (failure.status === 409 && failure.code === "id_is_bundled") {
        setError("This workflow is included with Azizam and cannot be deleted. You can disable it instead.");
        return;
      }
    }
    setError(messageFor(failure, "The change could not be saved. Try again."));
  };

  const check = async () => {
    if (disabled) return;
    const body = requestDraft();
    if (!body) return;
    const token = begin("Checking");
    if (token === null) return;
    const checkedVersion = draftVersion.current;
    try {
      const report = await ownerApi.check(body);
      if (!current(token) || checkedVersion !== draftVersion.current) return;
      setProblems(report.errors);
      setNotice(report.ok && !report.errors.length ? "No problems found." : "Fix the problems below before saving.");
    } catch (failure) {
      if (!current(token) || checkedVersion !== draftVersion.current) return;
      if (failure instanceof OwnerApiError) setProblems(failure.problems);
      setError(messageFor(failure, "The workflow could not be checked. Try again."));
    } finally { finish(token); }
  };

  const write = async (
    label: string,
    action: (etag: string) => ReturnType<typeof ownerApi.create>,
    deleted = false,
  ) => {
    if (disabled) return;
    const token = begin(label);
    if (token === null) return;
    try {
      // Existing records always use the ETag paired with this sheet's detail,
      // never the independently refreshed list's ETag.
      const commit = await action(selection.id === null ? createEtag : snapshot!.etag);
      if (deleted || selection.id === null) {
        // A refresh failure must never leave a successful create looking like
        // an unsaved new document, or a deleted record available to edit.
        const latestList = await refreshLists().catch(() => list);
        if (!current(token)) return;
        if (deleted) onClose();
        else onCreated(commit.workflow_id, latestList);
        return;
      }
      const [latestList, latestDetail] = await Promise.allSettled([
        refreshLists(),
        ownerApi.get(selection.id),
      ]);
      if (!current(token)) return;
      if (latestDetail.status === "rejected") {
        setReloadRequired(true);
        setError("The change was saved, but the updated workflow could not be loaded. Reload latest before making another change.");
        return;
      }
      applyDetail(latestDetail.value);
      setNotice(latestList.status === "fulfilled" ? "Changes saved." : "Changes saved. Refresh owner workflows to update the list.");
    } catch (failure) {
      await handleFailure(failure, token);
    } finally { finish(token); }
  };

  const save = () => {
    if (disabled) return;
    const body = requestDraft();
    if (!body) return;
    void write("Saving", (etag) => selection.id === null
      ? ownerApi.create(body, etag)
      : ownerApi.replace(selection.id, body, etag));
  };
  const reload = async () => {
    const token = begin("Reloading");
    if (token === null) return;
    try {
      const latestList = await refreshLists();
      const latest = selection.id === null ? null : await ownerApi.get(selection.id);
      if (!current(token)) return;
      if (latest) applyDetail(latest);
      else { setCreateEtag(latestList.etag); setConflict(false); setReloadRequired(false); }
      setNotice("Latest version loaded. Review it before saving.");
    } catch (failure) {
      if (current(token)) setError(messageFor(failure, "The latest version could not be loaded. Try again."));
    } finally { finish(token); }
  };
  const entry = snapshot?.data;
  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-full overflow-y-auto sm:!max-w-3xl">
        <SheetHeader className="border-b pr-12">
          <SheetTitle>{selection.id === null ? "New workflow" : `Manage ${selection.id}`}</SheetTitle>
          <SheetDescription>Edit the workflow document, check it and save your changes.</SheetDescription>
        </SheetHeader>
        <div className="space-y-6 p-4">
          {selection.id !== null && !ready ? (
            detail.isError ? <div className="space-y-2"><p role="alert">{messageFor(detail.error, "This workflow could not be loaded. Try again.")}</p><Button variant="outline" onClick={() => void detail.refetch()} disabled={detail.isFetching}>Retry workflow details</Button></div>
              : <p role="status">Loading workflow details…</p>
          ) : null}
          {notice && <p role="status" className="text-meta">{notice}</p>}
          {error && <p role="alert" className="text-meta">{error}</p>}
          {(conflict || reloadRequired || dirty) && <Button variant="outline" onClick={() => void reload()} disabled={!!pending}>Reload latest</Button>}
          {pending && <p role="status" className="text-muted-foreground text-meta">{pending}…</p>}
          {ready && <>
            {entry && <section aria-label="Workflow record" className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <StateBadge state={entry.state === "active" ? "ready" : "degraded"} word={entry.state === "active" ? "Enabled" : "Disabled"} />
                <span className="text-meta">{entry.published ? "Published" : "Unpublished"} · Revision {entry.record_revision}</span>
              </div>
              <p className="text-muted-foreground text-meta"><EntryProblems problems={entry.entry_problems} /></p>
              
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-meta">
                <dt>Record digest</dt><dd className="break-all font-mono">{entry.record_digest}</dd>
                <dt>Contract digest</dt><dd className="break-all font-mono">{entry.contract_digest}</dd>
                <dt>Source</dt><dd className="break-words">{entry.provenance.source === "local" ? "Bundled with Azizam" : entry.provenance.trusted ? "Added by an owner" : "Not reviewed"}</dd>
              </dl>
              {dirty && <p className="text-meta">Save or reload your edits first.</p>}
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" disabled={disabled || dirty} onClick={() => void write("Saving publication", (etag) => ownerApi.setPublished(entry.id, !entry.published, etag))}>{entry.published ? "Unpublish" : "Publish"}</Button>
                <Button variant="outline" disabled={disabled || dirty} onClick={() => void write("Saving state", (etag) => ownerApi.setState(entry.id, entry.state === "active" ? "disabled" : "active", etag))}>{entry.state === "active" ? "Disable" : "Enable"}</Button>
                <Button variant="outline" disabled={disabled || dirty} onClick={() => void write("Deleting", (etag) => ownerApi.delete(entry.id, etag), true)}>Delete workflow</Button>
              </div>
            </section>}
            <section aria-label="Workflow editor" className="space-y-3">
              <label className="block space-y-2">
                <span className="font-medium">Workflow JSON</span>
                <textarea value={draft.json} onChange={(event) => edit({ ...draft, json: event.target.value })} disabled={!!pending} rows={18} spellCheck={false} className="border-input bg-card rounded-control w-full resize-y border p-3 font-mono text-meta" />
              </label>
              <fieldset disabled={!!pending} className="space-y-3">
                <legend className="mb-2 font-medium">Policy facts</legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  {FACT_FIELDS.map(({ key, label }) => (
                    <label key={key} className="block space-y-1">
                      <span className="text-meta">{label}</span>
                      <select value={choices[key].includes(draft.facts[key]) ? draft.facts[key] : ""} onChange={(event) => edit({ ...draft, factsMode: "set", facts: { ...draft.facts, [key]: event.target.value } })} className="border-input bg-card rounded-control min-h-10 w-full border px-3 text-meta max-sm:min-h-11">
                        <option value="">Choose {label.toLowerCase()}</option>
                        {choices[key].map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                      </select>
                    </label>
                  ))}
                </div>
                <p className="text-muted-foreground text-meta">Tags: {draft.facts.tags.length ? draft.facts.tags.join(", ") : "None"}. {draft.factsMode === "clear" ? "Tags will be cleared with the facts." : "Existing tags are kept when you change facts."}</p>
                <Button type="button" variant="outline" onClick={() => edit({ ...draft, factsMode: "clear", facts: { ...emptyFacts(), tags: draft.facts.tags } })}>Clear facts</Button>
                {draft.factsMode === "clear" && <p className="text-meta">Facts will be cleared when you save. This workflow will need configuration.</p>}
              </fieldset>
              <Problems problems={problems} />
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" disabled={disabled} onClick={() => void check()}>Check</Button>
                <Button disabled={disabled} onClick={save}>Save</Button>
              </div>
            </section>
            {snapshot && <section aria-label="History" className="space-y-3">
              <h3 className="font-medium">History</h3>
              <p className="text-muted-foreground text-meta">The latest 20 revisions. Restoring keeps the workflow’s current enabled or disabled state.</p>
              {snapshot.data.history.length ? <ListCard label="Workflow history">
                {snapshot.data.history.slice(0, 20).map((revision) => <ListRow key={revision.record_revision} lines={2}
                  title={`Revision ${revision.record_revision}`}
                  meta={`${revision.change} · ${revision.actor.label ?? revision.actor.id} · ${new Date(revision.saved_at_s * 1000).toLocaleString()}`}
                  trailing={<Button variant="outline" disabled={disabled || dirty} aria-label={`Restore revision ${revision.record_revision}`} onClick={() => void write("Restoring", (etag) => ownerApi.restore(selection.id!, revision.record_revision, etag))}>Restore</Button>}
                />)}
              </ListCard> : <p className="text-muted-foreground text-meta">No saved revisions yet.</p>}
            </section>}
          </>}
        </div>
      </SheetContent>
    </Sheet>
  );
}

import { adminExchange } from "@/lib/api";

export type WorkflowState = "active" | "disabled";

export interface WorkflowFacts {
  readonly exposure: string;
  readonly trust_tier: string;
  readonly resource_tier: string;
  readonly resource_class: string;
  readonly tags: readonly string[];
}

/** Values come from the list response, never a second policy vocabulary in the web app. */
export interface FactChoices {
  readonly exposure: readonly string[];
  readonly trust_tier: readonly string[];
  readonly resource_tier: readonly string[];
  readonly resource_class: readonly string[];
}

export interface WorkflowDraft {
  readonly contract: Record<string, unknown>;
  /** Omitted keeps stored facts; null explicitly clears them. */
  readonly facts?: WorkflowFacts | null;
  readonly overrides?: string | null;
}

export interface WorkflowHistoryEntry {
  readonly record_revision: number;
  readonly record_digest: string;
  readonly contract_digest: string;
  readonly saved_at_s: number;
  readonly actor: {
    readonly kind: string;
    readonly id: string;
    readonly label: string | null;
  };
  readonly change: "create" | "update" | "state" | "publish" | "restore" | "delete";
}

export interface WorkflowCommit {
  readonly revision: number;
  readonly workflow_id: string;
  readonly record_revision: number;
  readonly contract_digest: string | null;
  readonly record_digest: string | null;
}

/** A token belongs to this read, not to whichever request happened to finish last. */
export interface OwnerRead<T> {
  readonly data: T;
  readonly etag: string;
}

/*
 * Wire shapes of gateway/workflow_owner_api.py. List {workflows, revision, choices};
 * detail {workflow}; check {report: {ok, errors}}; write {commit}; error
 * {error: {code, message, ...}} with a top-level `problems` list on a 422.
 */
export interface WorkflowProblem {
  readonly path: readonly (string | number)[];
  /** The path as a JSON Pointer; "" is the document itself. */
  readonly pointer: string;
  readonly code: string;
  readonly message: string;
}

export interface EntryProblem {
  readonly code: string;
}

export interface Provenance {
  readonly trusted: boolean;
  /** "local" is a bundled workflow. */
  readonly source: string | null;
}

export interface OwnerWorkflowSummary {
  readonly id: string;
  readonly state: WorkflowState;
  readonly published: boolean;
  readonly record_revision: number;
  readonly contract_digest: string;
  readonly record_digest: string;
  readonly provenance: Provenance;
  readonly entry_problems: readonly EntryProblem[];
  readonly needs_configuration: boolean;
}

export interface OwnerWorkflowList {
  readonly workflows: readonly OwnerWorkflowSummary[];
  readonly revision: number;
  readonly choices: FactChoices;
}

export interface OwnerWorkflowDetail extends OwnerWorkflowSummary {
  readonly contract: Record<string, unknown>;
  readonly facts: WorkflowFacts | null;
  /** The id of the bundled workflow this record replaces. */
  readonly overrides: string | null;
  readonly incarnation: string;
  readonly history: readonly WorkflowHistoryEntry[];
}

export interface WorkflowCheckReport {
  readonly ok: boolean;
  readonly errors: readonly WorkflowProblem[];
}

export interface OwnerApiErrorOptions {
  readonly status: number;
  readonly code: string;
  readonly message: string;
  readonly problems?: readonly WorkflowProblem[];
  readonly currentRevision?: number;
}

export class OwnerApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problems: readonly WorkflowProblem[];
  readonly currentRevision?: number;

  constructor(options: OwnerApiErrorOptions) {
    super(options.message);
    this.name = "OwnerApiError";
    this.status = options.status;
    this.code = options.code;
    this.problems = options.problems ?? [];
    this.currentRevision = options.currentRevision;
  }
}

const ROOT = "/admin/api/workflows";

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const string = (value: unknown): value is string => typeof value === "string";
const nullableString = (value: unknown) => value === null || string(value);
const strings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(string);
const revision = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

function problem(value: unknown): value is WorkflowProblem {
  return record(value) && Array.isArray(value.path) &&
    value.path.every((part) => string(part) || typeof part === "number") &&
    string(value.pointer) && string(value.code) && string(value.message);
}

const problems = (value: unknown): value is WorkflowProblem[] =>
  Array.isArray(value) && value.every(problem);

function facts(value: unknown): value is WorkflowFacts {
  return record(value) && string(value.exposure) && string(value.trust_tier) &&
    string(value.resource_tier) && string(value.resource_class) && strings(value.tags);
}

function choices(value: unknown): value is FactChoices {
  return record(value) && strings(value.exposure) && strings(value.trust_tier) &&
    strings(value.resource_tier) && strings(value.resource_class);
}

function provenance(value: unknown): value is Provenance {
  return record(value) && typeof value.trusted === "boolean" && nullableString(value.source);
}

function summary(value: unknown): value is OwnerWorkflowSummary {
  return record(value) && string(value.id) &&
    (value.state === "active" || value.state === "disabled") &&
    typeof value.published === "boolean" && revision(value.record_revision) &&
    string(value.contract_digest) && string(value.record_digest) &&
    provenance(value.provenance) &&
    Array.isArray(value.entry_problems) &&
    value.entry_problems.every((item) => record(item) && string(item.code)) &&
    typeof value.needs_configuration === "boolean";
}

function historyEntry(value: unknown): value is WorkflowHistoryEntry {
  if (!record(value) || !record(value.actor)) return false;
  return revision(value.record_revision) && string(value.record_digest) &&
    string(value.contract_digest) && revision(value.saved_at_s) &&
    string(value.actor.kind) && string(value.actor.id) && nullableString(value.actor.label) &&
    string(value.change) && ["create", "update", "state", "publish", "restore", "delete"].includes(value.change);
}

function workflowList(value: unknown): value is OwnerWorkflowList {
  return record(value) && Array.isArray(value.workflows) &&
    value.workflows.every(summary) && revision(value.revision) && choices(value.choices);
}

function workflowDetail(value: unknown): value is { workflow: OwnerWorkflowDetail } {
  const w = record(value) ? value.workflow : undefined;
  return summary(w) && record(w) && record(w.contract) &&
    (w.facts === null || facts(w.facts)) && nullableString(w.overrides) &&
    string(w.incarnation) && Array.isArray(w.history) && w.history.every(historyEntry);
}

function checkReport(value: unknown): value is { report: WorkflowCheckReport } {
  const r = record(value) ? value.report : undefined;
  return record(r) && typeof r.ok === "boolean" && problems(r.errors);
}

function commit(value: unknown): value is { commit: WorkflowCommit } {
  const c = record(value) ? value.commit : undefined;
  return unwrappedCommit(c);
}

function unwrappedCommit(value: unknown): value is WorkflowCommit {
  return record(value) && revision(value.revision) && string(value.workflow_id) &&
    revision(value.record_revision) && nullableString(value.contract_digest) &&
    nullableString(value.record_digest);
}

function validEtag(value: unknown): value is string {
  return string(value) && /^"os-\d+"$/.test(value);
}

function invalidResponse(status: number): OwnerApiError {
  return new OwnerApiError({
    status,
    code: "invalid_response",
    message: "The workflow response could not be read. Reload before trying another change.",
  });
}

function requireEtag(etag: string): void {
  if (!validEtag(etag)) {
    throw new OwnerApiError({
      status: 0,
      code: "missing_etag",
      message: "Reload this workflow before making a change.",
    });
  }
}

function responseError(status: number, value: unknown): OwnerApiError {
  const top = record(value) ? value : {};
  const body = record(top.error) ? top.error : {};
  const code = string(body.code) ? body.code : "request_failed";
  let message = string(body.message) ? body.message : "The workflow request failed. Try again.";
  if (code === "generation_mismatch") {
    message = "Workflows changed since this version was loaded. Reload and review the latest version before saving.";
  } else if (code === "workflow_exists") {
    message = "A workflow with this ID already exists. Open it to make changes, or choose another ID.";
  } else if (code === "id_is_bundled") {
    message = "This workflow is bundled with Azizam and cannot be deleted. You can disable it instead.";
  } else if (status === 422) {
    message = "The workflow has problems to fix before it can be saved.";
  }
  if (top.problems !== undefined && !problems(top.problems)) throw invalidResponse(status);
  return new OwnerApiError({
    status,
    code,
    message,
    problems: problems(top.problems) ? top.problems : [],
    currentRevision: revision(body.current_revision) ? body.current_revision : undefined,
  });
}

interface RequestOptions {
  readonly method?: "GET" | "POST" | "PUT" | "DELETE";
  readonly body?: unknown;
  readonly etag?: string;
  readonly signal?: AbortSignal;
}

async function request<T>(
  path: string,
  valid: (value: unknown) => value is T,
  options: RequestOptions = {},
): Promise<{ data: T; response: Response }> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.etag !== undefined) headers["If-Match"] = options.etag;
  // The shared admin sender: writes carry the action and CSRF headers a browser session needs.
  const { response, payload: value } = await adminExchange(path, {
    method: options.method ?? "GET",
    headers,
    signal: options.signal,
    ...(options.body === undefined ? {} : { body: options.body }),
  });
  if (value === null) {
    if (!response.ok) throw responseError(response.status, undefined);
    throw invalidResponse(response.status);
  }
  if (!response.ok) throw responseError(response.status, value);
  if (!valid(value)) throw invalidResponse(response.status);
  return { data: value, response };
}

async function read<W, T>(
  path: string,
  valid: (value: unknown) => value is W,
  select: (wire: W) => T,
  signal?: AbortSignal,
): Promise<OwnerRead<T>> {
  const { data, response } = await request(path, valid, { signal });
  const etag = response.headers.get("ETag");
  if (!validEtag(etag)) throw invalidResponse(response.status);
  return Object.freeze({ data: select(data), etag });
}

async function write(
  path: string,
  method: "POST" | "PUT" | "DELETE",
  body: unknown,
  etag: string,
  signal?: AbortSignal,
): Promise<WorkflowCommit> {
  // Check this before starting a request, including create. No blind write.
  requireEtag(etag);
  const { data } = await request(path, commit, { method, body, etag, signal });
  return data.commit;
}

const workflowPath = (id: string) => `${ROOT}/${encodeURIComponent(id)}`;

/**
 * Stateless by design. Keep a detail's ETag with its editable draft until the
 * user reloads that detail. List refreshes and check results cannot replace it.
 * After a successful write, read the detail again before the next change.
 */
export const ownerApi = {
  list(signal?: AbortSignal): Promise<OwnerRead<OwnerWorkflowList>> {
    return read(ROOT, workflowList, (wire) => wire, signal);
  },
  get(id: string, signal?: AbortSignal): Promise<OwnerRead<OwnerWorkflowDetail>> {
    return read(workflowPath(id), workflowDetail, (wire) => wire.workflow, signal);
  },
  async check(draft: WorkflowDraft, signal?: AbortSignal): Promise<WorkflowCheckReport> {
    // This POST is read-only and must not advance a caller's edit token.
    const { data } = await request(`${ROOT}/check`, checkReport, { method: "POST", body: draft, signal });
    return data.report;
  },
  create(draft: WorkflowDraft, etag: string, signal?: AbortSignal): Promise<WorkflowCommit> {
    return write(ROOT, "POST", draft, etag, signal);
  },
  replace(id: string, draft: WorkflowDraft, etag: string, signal?: AbortSignal): Promise<WorkflowCommit> {
    return write(workflowPath(id), "PUT", draft, etag, signal);
  },
  setState(id: string, state: WorkflowState, etag: string, signal?: AbortSignal): Promise<WorkflowCommit> {
    return write(`${workflowPath(id)}/state`, "POST", { state }, etag, signal);
  },
  setPublished(id: string, published: boolean, etag: string, signal?: AbortSignal): Promise<WorkflowCommit> {
    return write(`${workflowPath(id)}/publish`, "POST", { published }, etag, signal);
  },
  restore(id: string, recordRevision: number, etag: string, signal?: AbortSignal): Promise<WorkflowCommit> {
    return write(`${workflowPath(id)}/restore`, "POST", { record_revision: recordRevision }, etag, signal);
  },
  delete(id: string, etag: string, signal?: AbortSignal): Promise<WorkflowCommit> {
    return write(workflowPath(id), "DELETE", undefined, etag, signal);
  },
};

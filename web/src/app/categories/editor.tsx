"use client";

import { useId, useRef, useState } from "react";
import { adminFetch, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Callout, StatusLine } from "@/components/product/status-ui";

export type Category = { id: string; label: string; kind?: string; values: string[]; applies_to: string[] };
export type Rule = { id: string; category: string; op: string; values: string[] };
export type Grant = { capability: string; service: string; release?: string; workflow?: string };
export type Plan = { id: string; grants: Grant[]; require: Rule[]; deny: Rule[]; missing: Record<string, string>; mode: string; legacy_tags: string[] };
export type Assertion = { category: string; value: unknown; source: string; accepted: boolean; observed_at: string; accepted_at: string; accepting_owner: string; mapping_version?: string };
export type Member = { id: string; kind: string; assertions: Assertion[] };
export type Service = { id: string; members: Member[]; dependencies: Service[]; checkable: boolean };
export type Snapshot = { id: string; categories: Record<string, Category>; people: Record<string, Plan[]>; apps: Record<string, Plan[]>; services: Record<string, Service>; completeness_gate?: boolean; retained: Record<string, Service> };
type Active = { revision: number; snapshot: Snapshot };
type Request = { person: string; app: string; capability: string; service: string; output?: string; phase?: string };
type Decision = { outcome: string; reason: string; rule: string; category: string; member: string; waivers: [string, string, string][] };
type Candidate = Active & { preview_base_revision: number | null; preview: { gains: Request[]; losses: Request[]; reason_changes: { request: Request; before: Decision; after: Decision }[]; scope: Request[] } };
type Choice = { id: string; label: string };
const root = "/admin/api/categories";
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const owns = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);
const categoryFor = (categories: Snapshot["categories"], id: string) => owns(categories, id) ? categories[id] : undefined;
const plain = (value: string) => value.replace(/[_-]/g, " ").replace(/^./, (letter) => letter.toUpperCase());
export const categoryId = (label: string) => label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
const capabilities = [
  { id: "chat.completions", label: "Chat" }, { id: "image.generate", label: "Images" },
  { id: "video.generate", label: "Video" }, { id: "speech.synthesis", label: "Voice" },
  { id: "speech.transcription", label: "Transcription" }, { id: "music.generate", label: "Music" },
  { id: "sound.generate", label: "Sound effects" }, { id: "embed", label: "Embeddings" },
];
const emptySnapshot = (): Snapshot => ({ id: "owner-rules", categories: {}, people: {}, apps: {}, services: {}, completeness_gate: true, retained: {} });
const allPlans = (snapshot: Snapshot) => [...Object.values(snapshot.people), ...Object.values(snapshot.apps)].flat();
function servicesIn(snapshot: Snapshot) {
  const result: Service[] = [];
  const visit = (service: Service) => { result.push(service); service.dependencies.forEach(visit); };
  [...Object.values(snapshot.services), ...Object.values(snapshot.retained)].forEach(visit);
  return result;
}
const assertionValues = (value: unknown): unknown[] => Array.isArray(value) ? value : [value];

export function valueInUse(snapshot: Snapshot, category: string, value: string): boolean {
  return allPlans(snapshot).some((plan) => [...plan.require, ...plan.deny].some((row) => row.category === category && row.values.includes(value)))
    || servicesIn(snapshot).some((service) => service.members.some((member) => member.assertions.some((assertion) => assertion.category === category && assertionValues(assertion.value).includes(value))));
}

export function isPinned(id: string): boolean {
  const at = id.lastIndexOf("@");
  return (at > 0 && at < id.length - 1) || (id.startsWith("sha256:") && id.length > 7);
}
export function validateSnapshot(snapshot: Snapshot): string[] {
  const errors: string[] = [];
  const nonempty = (value: unknown): value is string => typeof value === "string" && value.length > 0;
  const unique = (ids: string[], label: string) => {
    if (ids.some((id) => !nonempty(id))) errors.push(label + " needs a name.");
    if (new Set(ids).size !== ids.length) errors.push(label + " IDs must be unique.");
  };
  if (!nonempty(snapshot.id)) errors.push("Give these rules a name.");
  if (snapshot.completeness_gate === false) errors.push("These rules must keep the information check turned on.");
  unique(Object.values(snapshot.categories).map((category) => category.id), "Category");
  for (const [key, category] of Object.entries(snapshot.categories)) {
    if (key !== category.id) errors.push("A category ID does not match its saved name.");
    if (!nonempty(category.label)) errors.push("Every category needs a label.");
    if ((category.kind ?? "set") !== "set") errors.push("Only categories with a set of values can be edited.");
    if (category.values.some((value) => !nonempty(value))) errors.push(category.label + " values cannot be empty.");
    unique(category.values, category.label + " value");
    if (!category.applies_to.length || category.applies_to.some((kind) => !["release", "route", "workflow"].includes(kind))) errors.push(category.label + " needs at least one supported member type.");
  }
  const checkValues = (categoryId: string, values: unknown[], label: string) => {
    const category = categoryFor(snapshot.categories, categoryId);
    if (!category) { errors.push(label + " refers to a missing category."); return; }
    if (values.some((value) => typeof value !== "string" || !category.values.includes(value))) errors.push(label + " refers to a missing value in " + category.label + ".");
  };
  for (const plan of allPlans(snapshot)) {
    if (!nonempty(plan.id)) errors.push("Every access plan needs a name.");
    if (!["v3", "legacy"].includes(plan.mode)) errors.push("An access plan has an unknown mode.");
    if (plan.mode === "legacy" && (plan.require.length || plan.deny.length || Object.keys(plan.missing).length)) errors.push("A legacy access plan cannot have category rules or missing-value waivers.");
    if (plan.mode === "v3" && plan.legacy_tags.length) errors.push("A category access plan cannot have legacy tags.");
    if (plan.legacy_tags.some((tag) => typeof tag !== "string")) errors.push("Legacy tags must be text.");
    unique([...plan.require, ...plan.deny].map((row) => row.id), "Rule");
    for (const [kind, rows] of [["require", plan.require], ["deny", plan.deny]] as const) {
      for (const row of rows) {
        checkValues(row.category, row.values, "Rule " + row.id);
        if (!(kind === "require" ? ["in", "exists", "missing"] : ["in", "missing"]).includes(row.op)) errors.push("Choose how to check rule " + row.id + ".");
        if (row.op !== "in" && row.values.length) errors.push("Rule " + row.id + " checks whether a value is present, so it cannot list values.");
      }
    }
    for (const [row, waiver] of Object.entries(plan.missing)) {
      if (!plan.require.some((item) => item.id === row) || waiver !== "allow") errors.push("A missing-value waiver must name a required rule.");
    }
    for (const grant of plan.grants) {
      if (!owns(grant, "capability") || !owns(grant, "service") || Object.entries(grant).some(([key, value]) => !["capability", "service", "release", "workflow"].includes(key) || !nonempty(value))) errors.push("Every grant needs a capability and service, with no empty selectors.");
      if (!isPinned(grant.service)) errors.push("Use a pinned service revision for every grant.");
      if (grant.release !== undefined && !isPinned(grant.release)) errors.push("Use a pinned model release for every grant.");
      if (grant.workflow !== undefined && !isPinned(grant.workflow)) errors.push("Use a pinned workflow revision for every grant.");
    }
  }
  for (const [key, service] of Object.entries(snapshot.services)) {
    if (key !== service.id) errors.push("A service ID does not match its saved name.");
  }
  const catalog = new Map<string, Service>();
  for (const service of servicesIn(snapshot)) {
    if (!nonempty(service.id)) errors.push("Every service needs an ID.");
    const previous = catalog.get(service.id);
    if (previous && !equal(previous, service)) errors.push("Service IDs must name the same reviewed members everywhere.");
    catalog.set(service.id, service);
    for (const member of service.members) {
      if (!isPinned(member.id) || !["release", "route", "workflow"].includes(member.kind)) errors.push("Every service member needs a pinned revision and a supported type.");
      for (const assertion of member.assertions) {
        if (typeof assertion.accepted !== "boolean") errors.push("An assertion must say whether it is accepted.");
        // Unaccepted imports are inert and retain their exact original evidence.
        if (assertion.accepted === true) {
          const values = assertionValues(assertion.value);
          checkValues(assertion.category, values, "An accepted value on " + member.id);
          if (!values.length) errors.push("An accepted assertion needs at least one value.");
          const category = categoryFor(snapshot.categories, assertion.category);
          if (category && !category.applies_to.includes(member.kind)) errors.push(category.label + " does not apply to " + member.kind + " members.");
          if (![assertion.source, assertion.observed_at, assertion.accepted_at, assertion.accepting_owner, assertion.mapping_version].every(nonempty)) errors.push("An accepted value on " + member.id + " needs its source, dates, owner and mapping version.");
        }
      }
    }
  }
  return [...new Set(errors)];
}

function nextServiceId(id: string, used: Set<string>, editToken: string): string {
  const at = id.lastIndexOf("@");
  const stem = at > 0 ? id.slice(0, at) : id;
  const revision = at > 0 ? id.slice(at + 1) : "r0";
  const suffix = /^r?([0-9]+)(?:-.*)?$/.exec(revision);
  const increment = (value: string): string => {
    const digits = value.split("");
    for (let index = digits.length - 1; index >= 0; index--) {
      if (digits[index] !== "9") { digits[index] = String(Number(digits[index]) + 1); return digits.join(""); }
      digits[index] = "0";
    }
    return "1" + digits.join("");
  };
  let number = suffix ? increment(suffix[1]) : "1";
  let next = stem + "@r" + number + "-" + editToken;
  while (used.has(next)) { number = increment(number); next = stem + "@r" + number + "-" + editToken; }
  return next;
}

// New assertions change an immutable closure. Version changed dependencies first,
// then their parents, and move only current grants. Retained output bundles stay intact.
export function prepareSnapshot(draft: Snapshot, base: Snapshot, editToken: string): Snapshot {
  const result = clone(draft);
  const old = new Map(servicesIn(base).map((service) => [service.id, service]));
  const used = new Set([...old.keys(), ...servicesIn(draft).map((service) => service.id)]);
  const replacements = new Map<string, Service>();
  const version = (service: Service): Service => {
    const known = replacements.get(service.id);
    if (known) return clone(known);
    const next = { ...service, dependencies: service.dependencies.map(version) };
    if (old.has(service.id) && !equal(old.get(service.id), next)) {
      next.id = nextServiceId(service.id, used, editToken);
      used.add(next.id);
    }
    replacements.set(service.id, next);
    return clone(next);
  };
  result.services = Object.fromEntries(Object.values(result.services).map((service) => {
    const next = version(service);
    return [next.id, next];
  }));
  for (const plan of allPlans(result)) {
    plan.grants = plan.grants.map((grant) => ({ ...grant, service: replacements.get(grant.service)?.id ?? grant.service }));
  }
  return result;
}

// Apply local changes to the latest rules without silently overwriting someone
// else's work. Arrays are intentional units; conflicting edits need review.
export function rebuildSnapshot(base: Snapshot, draft: Snapshot, latest: Snapshot): Snapshot {
  const merge = (before: unknown, edited: unknown, current: unknown, path: string): unknown => {
    if (equal(before, edited)) return current;
    if (equal(before, current) || equal(edited, current)) return edited;
    if ([before, edited, current].every((value) => value !== null && typeof value === "object" && !Array.isArray(value))) {
      const b = before as Record<string, unknown>, d = edited as Record<string, unknown>, c = current as Record<string, unknown>;
      return Object.fromEntries([...new Set([...Object.keys(b), ...Object.keys(d), ...Object.keys(c)])]
        .map((key) => [key, merge(b[key], d[key], c[key], path ? path + " / " + key : key)])
        .filter(([, value]) => value !== undefined));
    }
    throw new Error("Both edits changed " + plain(path) + ". Your edit is still here. Start from the current rules to review this change.");
  };
  return clone(merge(base, draft, latest, "") as Snapshot);
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  const id = useId();
  return <div className="space-y-2"><label className="text-label" htmlFor={id}>{label}</label><Input id={id} value={value} onChange={(event) => onChange(event.target.value)} /></div>;
}
function Pick({ label, value, choices, onChange }: { label: string; value: string; choices: Choice[]; onChange: (value: string) => void }) {
  const id = useId();
  const options = value && !choices.some((choice) => choice.id === value) ? [{ id: value, label: value }, ...choices] : choices;
  return <div className="space-y-2"><label className="text-label" htmlFor={id}>{label}</label>
    <Select value={value} onValueChange={onChange}><SelectTrigger id={id} className="w-full"><SelectValue placeholder={"Choose " + label.toLowerCase()} /></SelectTrigger>
      <SelectContent>{options.map((choice) => <SelectItem key={choice.id} value={choice.id}>{choice.label}</SelectItem>)}</SelectContent>
    </Select></div>;
}
function Values({ category, selected, onChange, label }: { category: Category; selected: string[]; onChange: (values: string[]) => void; label: string }) {
  return <fieldset className="space-y-2"><legend className="text-label">{label}</legend>
    {category.values.map((value) => <label key={value} className="flex min-h-8 max-sm:min-h-11 items-center gap-2 text-meta">
      <input className="size-4 accent-primary" type="checkbox" checked={selected.includes(value)} onChange={(event) => onChange(event.target.checked ? [...selected, value] : selected.filter((item) => item !== value))} />{plain(value)}
    </label>)}</fieldset>;
}
function CategoryCard({ category, change, remove, report }: { category: Category; change: (category: Category) => void; remove: (value: string) => void; report: (message: string) => void }) {
  const [value, setValue] = useState("");
  return <div className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
    <Field label={"Label for " + category.id} value={category.label} onChange={(label) => change({ ...category, label })} />
    <ul className="space-y-2">{category.values.map((item) => <li key={item} className="flex items-center justify-between gap-3 text-meta"><span>{plain(item)}</span><Button type="button" variant="outline" aria-label={"Remove " + plain(item) + " from " + category.label} onClick={() => remove(item)}>Remove</Button></li>)}</ul>
    <Field label={"New value for " + category.label} value={value} onChange={setValue} />
    <Button type="button" variant="outline" onClick={() => {
      const next = value.trim();
      if (!next) return report("Enter a value before adding it.");
      if (category.values.includes(next)) return report("That value is already in " + category.label + ".");
      change({ ...category, values: [...category.values, next] }); setValue("");
    }}>Add value to {category.label}</Button>
  </div>;
}
function ServiceCard({ service, categories, owner, onChange }: { service: Service; categories: Category[]; owner: string; onChange: (service: Service) => void }) {
  return <div className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
    <p className="text-body font-medium">Service: {service.id}</p>
    {!service.members.length && <p className="text-meta">No members in this service.</p>}
    {service.members.map((member, index) => <fieldset key={member.kind + ":" + member.id} className="space-y-4">
      <legend className="text-body font-medium">{plain(member.kind)}: {member.id}</legend>
      {categories.filter((category) => category.applies_to.includes(member.kind)).map((category) => {
        const selected = [...new Set(member.assertions.filter((assertion) => assertion.accepted && assertion.category === category.id).flatMap((assertion) => assertionValues(assertion.value)).filter((value): value is string => typeof value === "string"))];
        return <Values key={category.id} category={category} label={"Accepted " + category.label + " for " + member.id} selected={selected} onChange={(values) => {
          const assertions = member.assertions.flatMap((assertion) => {
            if (!assertion.accepted || assertion.category !== category.id) return [assertion];
            const kept = assertionValues(assertion.value).filter((value) => typeof value === "string" && values.includes(value));
            if (!kept.length) return [];
            return [{ ...assertion, value: Array.isArray(assertion.value) ? kept : kept[0] }];
          });
          const timestamp = new Date().toISOString();
          values.filter((value) => !selected.includes(value)).forEach((value) => assertions.push({
            category: category.id, value, accepted: true, source: "owner",
            observed_at: timestamp, accepted_at: timestamp, accepting_owner: owner, mapping_version: "owner-edit-1",
          }));
          onChange({ ...service, members: service.members.map((item, at) => at === index ? { ...item, assertions } : item) });
        }} />;
      })}
    </fieldset>)}
    {service.dependencies.map((dependency, index) => <ServiceCard key={dependency.id} service={dependency} categories={categories} owner={owner} onChange={(next) => onChange({ ...service, dependencies: service.dependencies.map((item, at) => at === index ? next : item) })} />)}
  </div>;
}
export function updateCurrentService(snapshot: Snapshot, updated: Service): Snapshot {
  const replacements = new Map<string, Service>();
  const collect = (service: Service) => { replacements.set(service.id, service); service.dependencies.forEach(collect); };
  collect(updated);
  const replace = (service: Service): Service => {
    const current = replacements.get(service.id) ?? service;
    return { ...current, dependencies: current.dependencies.map(replace) };
  };
  return { ...snapshot, services: Object.fromEntries(Object.entries(snapshot.services).map(([id, service]) => [id, replace(service)])) };
}
function rowText(row: Rule, categories: Snapshot["categories"]) {
  const category = categoryFor(categories, row.category)?.label ?? "Choose a category";
  if (row.op === "exists") return category + " has an accepted value";
  if (row.op === "missing") return category + " has no accepted value";
  return category + ": " + (row.values.map(plain).join(" or ") || "Choose values");
}
function RuleEditor({ row, kind, categories, waiver, onChange, onWaiver, onRemove }: { row: Rule; kind: "require" | "deny"; categories: Snapshot["categories"]; waiver: boolean; onChange: (row: Rule) => void; onWaiver: (enabled: boolean) => void; onRemove: () => void }) {
  const category = categoryFor(categories, row.category);
  return <div className="space-y-4 border-t pt-4">
    <p className="text-body font-medium">{kind === "require" ? "Needs " : "Never: "}{rowText(row, categories)}</p>
    <Field label="Rule name" value={row.id} onChange={(id) => onChange({ ...row, id })} />
    <Pick label="Category" value={row.category} choices={Object.values(categories).map((item) => ({ id: item.id, label: item.label }))} onChange={(id) => onChange({ ...row, category: id, values: [] })} />
    <Pick label="Check" value={row.op} choices={[{ id: "in", label: "Matches these values" }, ...(kind === "require" ? [{ id: "exists", label: "Has an accepted value" }] : []), { id: "missing", label: "Has no accepted value" }]} onChange={(op) => onChange({ ...row, op, values: op === "in" ? row.values : [] })} />
    {category && row.op === "in" && <Values category={category} selected={row.values} label="Allowed values" onChange={(values) => onChange({ ...row, values })} />}
    {kind === "require" && <label className="flex min-h-8 max-sm:min-h-11 items-center gap-2 text-meta"><input className="size-4 accent-primary" type="checkbox" checked={waiver} onChange={(event) => onWaiver(event.target.checked)} />Allow a missing value for {row.id || "this required rule"}</label>}
    {waiver && <p className="text-meta">Missing-value waiver for {row.id}: skips only this requirement when its value is missing.</p>}
    <Button type="button" variant="outline" onClick={onRemove}>Remove {kind === "require" ? "requirement" : "blocking rule"}</Button>
  </div>;
}
function PlanEditor({ plan, categories, services, onChange }: { plan: Plan; categories: Snapshot["categories"]; services: Snapshot["services"]; onChange: (plan: Plan) => void }) {
  const newRule = (kind: "require" | "deny") => {
    const prefix = plan.id + "-" + (kind === "require" ? "needs" : "never");
    let number = 1;
    while ([...plan.require, ...plan.deny].some((row) => row.id === prefix + "-" + number)) number++;
    onChange({ ...plan, [kind]: [...plan[kind], { id: prefix + "-" + number, category: "", op: "in", values: [] }] });
  };
  return <div className="space-y-4 border-t pt-4">
    <Field label="Access plan name" value={plan.id} onChange={(id) => onChange({ ...plan, id })} />
    {plan.mode === "legacy" && <div className="space-y-3"><p className="text-meta">This access plan uses legacy tags: {plan.legacy_tags.map(plain).join(", ") || "None"}.</p><Button type="button" variant="outline" onClick={() => onChange({ ...plan, mode: "v3", legacy_tags: [] })}>Use category rules for this access plan</Button></div>}
    {plan.grants.map((grant, index) => <div key={index} className="space-y-4">
      <Pick label="Capability" value={grant.capability} choices={capabilities} onChange={(capability) => onChange({ ...plan, grants: plan.grants.map((item, at) => at === index ? { ...item, capability } : item) })} />
      <Pick label="Service" value={grant.service} choices={Object.keys(services).map((id) => ({ id, label: id }))} onChange={(service) => onChange({ ...plan, grants: plan.grants.map((item, at) => at === index ? { ...item, service } : item) })} />
      {(grant.release || grant.workflow) && <p className="text-meta">Also limited to {grant.release ? "model release " + grant.release : "workflow " + grant.workflow}.</p>}
      <Button type="button" variant="outline" onClick={() => onChange({ ...plan, grants: plan.grants.filter((_, at) => at !== index) })}>Remove grant</Button>
    </div>)}
    <Button type="button" variant="outline" onClick={() => onChange({ ...plan, grants: [...plan.grants, { capability: "", service: "" }] })}>Add grant</Button>
    {(["require", "deny"] as const).map((kind) => <div key={kind} className="space-y-4">
      {plan[kind].map((row, index) => <RuleEditor key={index} row={row} kind={kind} categories={categories} waiver={owns(plan.missing, row.id) && plan.missing[row.id] === "allow"} onChange={(next) => {
        let missing = { ...plan.missing };
        if (next.id !== row.id && owns(missing, row.id)) { missing = { ...missing, [next.id]: missing[row.id] }; delete missing[row.id]; }
        onChange({ ...plan, missing, [kind]: plan[kind].map((item, at) => at === index ? next : item) });
      }} onWaiver={(enabled) => { const missing = { ...plan.missing, ...(enabled ? { [row.id]: "allow" } : {}) }; if (!enabled) delete missing[row.id]; onChange({ ...plan, missing }); }} onRemove={() => {
        const missing = { ...plan.missing }; delete missing[row.id];
        onChange({ ...plan, missing, [kind]: plan[kind].filter((_, at) => at !== index) });
      }} />)}
      <Button type="button" variant="outline" disabled={plan.mode === "legacy"} onClick={() => newRule(kind)}>Add {kind === "require" ? "requirement" : "blocking rule"}</Button>
    </div>)}
  </div>;
}
function PlansSection({ kind, entries, choices, categories, services, onChange, report }: { kind: "people" | "apps"; entries: Record<string, Plan[]>; choices: Choice[]; categories: Snapshot["categories"]; services: Snapshot["services"]; onChange: (entries: Record<string, Plan[]>) => void; report: (message: string) => void }) {
  const [selected, setSelected] = useState("");
  const names = [...choices, ...Object.keys(entries).filter((id) => !choices.some((choice) => choice.id === id)).map((id) => ({ id, label: id }))];
  return <section className="space-y-3" aria-label={kind === "people" ? "Person access plans" : "App access plans"}>
    <h2 className="text-heading">{kind === "people" ? "Person access plans" : "App access plans"}</h2>
    {Object.entries(entries).map(([id, plans]) => <div key={id} className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
      <p className="text-body font-medium">{kind === "people" ? "Person: " : "App: "}{names.find((choice) => choice.id === id)?.label ?? id}</p>
      {!plans.length && <p className="text-meta">No access plans. This {kind === "people" ? "person" : "app"} cannot use any service.</p>}
      {plans.map((plan, index) => <PlanEditor key={index} plan={plan} categories={categories} services={services} onChange={(next) => onChange({ ...entries, [id]: plans.map((item, at) => at === index ? next : item) })} />)}
    </div>)}
    <div className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
      <Pick label={kind === "people" ? "Person for new access plan" : "App for new access plan"} choices={names} value={selected} onChange={setSelected} />
      <Button type="button" variant="outline" onClick={() => {
        if (!selected) return report("Choose a " + (kind === "people" ? "person" : "app") + " for the access plan.");
        const plans = owns(entries, selected) ? entries[selected] : [];
        let number = 1; while (plans.some((plan) => plan.id === "access-plan-" + number)) number++;
        onChange({ ...entries, [selected]: [...plans, { id: "access-plan-" + number, grants: [], require: [], deny: [], missing: {}, mode: "v3", legacy_tags: [] }] });
      }}>Add {kind === "people" ? "person" : "app"} access plan</Button>
    </div>
  </section>;
}

const reasonWords: Record<string, string> = {
  ALLOW: "Every attached access plan allows this request.", NO_PLANS: "The person or app has no access plan.",
  NO_GRANT: "An access plan does not grant this capability with this service.", EXPLICIT_DENY: "A blocking rule matches this member.",
  DENY_INPUT_UNKNOWN: "A blocking rule cannot be checked because an accepted value is missing.",
  REQUIRE_MISMATCH: "An accepted value does not meet a required rule.", REQUIRE_MISSING: "An accepted value needed by a required rule is missing.",
  NO_APPLICABLE_SUBJECT: "There is no applicable member to check for this rule.", METADATA_INCOMPLETE: "The gateway is missing information needed to check this request.",
  LEGACY_TAG_MISMATCH: "The service tags do not match the legacy access plan.", IDENTITY_UNAVAILABLE: "The gateway could not identify the person or app.",
  POLICY_UNAVAILABLE: "The gateway could not check the category rules.",
};
function DecisionText({ decision }: { decision: Decision }) {
  return <div className="text-meta space-y-2"><p>{plain(decision.outcome)}: {Object.prototype.hasOwnProperty.call(reasonWords, decision.reason) ? reasonWords[decision.reason] : "The gateway did not provide a recognized reason."}</p>
    {decision.rule && <p>Rule: {decision.rule}</p>}{decision.member && <p>Member: {decision.member}</p>}
    {decision.waivers.map(([row, category, member], index) => <p key={index}>Missing-value waiver used: {row} · {category} · {member}</p>)}
  </div>;
}
export function CategoriesEditor({ active, owner, people, apps, onClose, onActivated, ownerLoading = false, paused = false }: { active: Active | null; owner: string; ownerLoading?: boolean; paused?: boolean; people: Choice[]; apps: Choice[]; onClose: () => void; onActivated: () => Promise<void> | void }) {
  const [editToken] = useState(() => Date.now().toString(36) + Math.random().toString(36).slice(2, 6).padEnd(4, "0"));
  const [base, setBase] = useState(() => ({ revision: active?.revision ?? null, snapshot: clone(active?.snapshot ?? emptySnapshot()) }));
  const [draft, setDraft] = useState(() => clone(base.snapshot));
  const [label, setLabel] = useState("");
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const currentChanged = active !== null && active.revision > (base.revision ?? 0);
  const needsRebuild = stale || currentChanged;
  const prepared = prepareSnapshot(draft, base.snapshot, editToken);
  const rewrites = (["people", "apps"] as const).flatMap((kind) => Object.entries(draft[kind]).flatMap(([id, plans]) => plans.flatMap((plan, planIndex) => plan.grants.flatMap((grant, grantIndex) => {
    const next = prepared[kind][id][planIndex].grants[grantIndex].service;
    return next === grant.service ? [] : [{ key: kind + ":" + id + ":" + planIndex + ":" + grantIndex, label: (kind === "people" ? people : apps).find((choice) => choice.id === id)?.label ?? id, plan: plan.id, before: grant.service, after: next }];
  }))));
  const edit = (next: Snapshot) => { setDraft(next); setCandidate(null); setErrors([]); setNotice(""); };
  const report = (message: string) => setErrors([message]);
  const requestName = (request: Request) => (people.find((person) => person.id === request.person)?.label ?? request.person) + " · " + (apps.find((app) => app.id === request.app)?.label ?? request.app) + " · " + (capabilities.find((item) => item.id === request.capability)?.label ?? request.capability) + " · " + request.service + (request.output ? " · Output: " + request.output : "") + (request.phase ? " · " + plain(request.phase) : "");
  const loadCurrent = async () => {
    try { return await adminFetch<Active>(root + "/active"); }
    catch (error) { if (error instanceof ApiError && error.code === "no_active_snapshot") return null; throw error; }
  };
  const run = async (action: () => Promise<void>) => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setErrors([]); setNotice("");
    try { await action(); }
    catch (error) {
      setCandidate(null);
      if (error instanceof ApiError && error.code === "stale_base") { setStale(true); }
      else report(error instanceof ApiError ? error.message : error instanceof Error ? error.message : "The gateway could not save these rules. Try again.");
    } finally { inFlight.current = false; setBusy(false); }
  };
  const preview = () => {
    if (needsRebuild || paused || ownerLoading || inFlight.current) return;
    const validation = validateSnapshot(prepared);
    if (!reason.trim()) validation.push("Enter a reason for these changes.");
    if (!owner.trim()) validation.push("Your owner identity could not be loaded. Refresh it before saving.");
    if (validation.length) { setErrors(validation); return; }
    void run(async () => {
      setCandidate(null);
      const result = await adminFetch<Candidate>(root + "/candidates", { method: "POST", body: { base_revision: base.revision, reason: reason.trim(), snapshot: prepared } });
      if (result.preview_base_revision !== base.revision) { setStale(true); return; }
      setCandidate(result);
    });
  };
  const rebuild = (discard: boolean) => void run(async () => {
    const latest = await loadCurrent();
    const snapshot = latest?.snapshot ?? emptySnapshot();
    const next = discard ? clone(snapshot) : rebuildSnapshot(base.snapshot, draft, snapshot);
    setBase({ revision: latest?.revision ?? null, snapshot: clone(snapshot) }); setDraft(next);
    setCandidate(null); setStale(false); setNotice(discard ? "Current rules loaded. Add your changes and preview them again." : "Your edit is rebuilt on the current rules. Preview changes again before turning them on.");
  });
  return <section aria-label="Edit category rules" className="space-y-8">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-heading">Edit rules</h2><Button type="button" variant="outline" disabled={busy} onClick={onClose}>Close editor</Button></div>
    <p className="text-meta">Edits stay here until you preview them. Nothing is turned on until you choose Turn on.</p>
    {needsRebuild && <Callout as="div" severity="critical" title="The active rules changed. Create a new preview."><div className="flex flex-wrap gap-3">
      <Button variant="outline" disabled={busy} onClick={() => rebuild(false)}>Rebuild on current rules</Button>
      <Button variant="outline" disabled={busy} onClick={() => rebuild(true)}>Discard edit and load current rules</Button>
    </div></Callout>}
    {errors.length > 0 && <div role="alert" className="space-y-2 text-meta">{errors.map((error) => <p key={error}>{error}</p>)}</div>}
    {notice && <p role="status" className="text-meta">{notice}</p>}
    {ownerLoading && <StatusLine tone="unknown" text="Loading owner identity" compact />}
    {!ownerLoading && !owner.trim() && <p role="alert" className="text-meta">Your owner identity could not be loaded. Refresh it before saving.</p>}
    <fieldset disabled={busy || ownerLoading || !owner.trim()} className="space-y-8 min-w-0">
      <section aria-label="Edit categories" className="space-y-3"><h2 className="text-heading">Categories</h2>
        {Object.entries(draft.categories).map(([id, category]) => <CategoryCard key={id} category={category} report={report} change={(next) => edit({ ...draft, categories: { ...draft.categories, [id]: next } })} remove={(value) => {
          if (valueInUse(draft, id, value)) return report(plain(value) + " is used by an access plan or a service. Remove that use before removing this value.");
          edit({ ...draft, categories: { ...draft.categories, [id]: { ...category, values: category.values.filter((item) => item !== value) } } });
        }} />)}
        <div className="rounded-panel border bg-card p-4 sm:p-6 space-y-4"><Field label="New category label" value={label} onChange={setLabel} />
          <Button type="button" variant="outline" onClick={() => {
            const id = categoryId(label);
            if (!id) return report("Enter a category label with at least one letter or number.");
            if (Object.prototype.hasOwnProperty.call(draft.categories, id)) return report("A category with that ID already exists. Choose a different label.");
            edit({ ...draft, categories: { ...draft.categories, [id]: { id, label: label.trim(), kind: "set", values: [], applies_to: ["release", "route", "workflow"] } } }); setLabel("");
          }}>Add category</Button></div>
      </section>
      <section aria-label="Edit services" className="space-y-3"><h2 className="text-heading">Services</h2>
        <p className="text-meta">Changing accepted values creates new service revisions and moves current grants to them in the preview. Saved outputs keep their reviewed members.</p>
        {!Object.keys(draft.services).length && <p className="text-meta">No services are saved in these rules yet.</p>}
        {Object.entries(draft.services).map(([id, service]) => <ServiceCard key={id} service={service} categories={Object.values(draft.categories)} owner={owner} onChange={(next) => edit(updateCurrentService(draft, next))} />)}
      </section>
      <PlansSection kind="people" entries={draft.people} choices={people} categories={draft.categories} services={draft.services} report={report} onChange={(people) => edit({ ...draft, people })} />
      <PlansSection kind="apps" entries={draft.apps} choices={apps} categories={draft.categories} services={draft.services} report={report} onChange={(apps) => edit({ ...draft, apps })} />
      {rewrites.length > 0 && <section aria-label="Service grant changes" className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
        <h2 className="text-heading">Service grant changes</h2>
        <ul className="space-y-2 text-meta">{rewrites.map((item) => <li key={item.key}>{item.label} · {plain(item.plan)}: {item.before} → {item.after}</li>)}</ul>
      </section>}
      <Field label="Reason for changes" value={reason} onChange={(next) => { setReason(next); setCandidate(null); }} />
      {!candidate && <Button type="button" size="md" disabled={needsRebuild || paused} onClick={preview}>Preview changes</Button>}
    </fieldset>
    {candidate && !needsRebuild && <section aria-label="Change preview" className="rounded-panel border bg-card p-4 sm:p-6 space-y-4">
      <h2 className="text-heading">Change preview</h2>
      <p className="text-meta">Compared with {candidate.preview_base_revision === null ? "no active revision" : "revision " + candidate.preview_base_revision}. Nothing has been turned on.</p>
      <p className="text-meta">This compares {candidate.preview.scope.length} known requests. It does not cover unknown future requests.</p>
      {(["gains", "losses"] as const).map((kind) => <div key={kind} className="space-y-2"><p className="text-body font-medium">{kind === "gains" ? "Who gains access" : "Who loses access"}</p>
        {candidate.preview[kind].length ? <ul className="space-y-2 text-meta">{candidate.preview[kind].map((request, index) => <li key={index}>{requestName(request)}</li>)}</ul> : <p className="text-meta">None in the compared requests.</p>}
      </div>)}
      <div className="space-y-2"><p className="text-body font-medium">Reasons that change</p>
        {!candidate.preview.reason_changes.length && <p className="text-meta">None in the compared requests.</p>}
        {candidate.preview.reason_changes.map((item, index) => <div key={index} className="space-y-2 border-t pt-3"><p className="text-meta">{requestName(item.request)}</p><p className="text-label">Before</p><DecisionText decision={item.before} /><p className="text-label">After</p><DecisionText decision={item.after} /></div>)}
      </div>
      <details className="text-meta"><summary className="flex min-h-8 max-sm:min-h-11 items-center">Compared requests</summary><ul>{candidate.preview.scope.map((request, index) => <li key={index}>{requestName(request)}</li>)}</ul></details>
      <div className="flex flex-wrap gap-3"><Button size="md" disabled={busy || paused || ownerLoading || !owner.trim() || candidate.preview_base_revision !== base.revision} onClick={() => void run(async () => {
        try {
          await adminFetch(root + "/candidates/" + candidate.revision + "/activate", { method: "POST", body: { expected_active_revision: candidate.preview_base_revision } });
        } catch (error) {
          if (error instanceof ApiError && error.code === "stale_base") throw error;
          setCandidate(null); setStale(true);
          throw new Error("Couldn't confirm whether these rules were turned on. Load the current rules before trying again.");
        }
        setBase({ revision: candidate.revision, snapshot: clone(candidate.snapshot) }); setDraft(clone(candidate.snapshot)); setCandidate(null); setStale(false);
        setNotice("Revision " + candidate.revision + " is now selected for category previews.");
        await onActivated();
      })}>Turn on</Button><Button variant="outline" disabled={busy} onClick={() => setCandidate(null)}>Close preview</Button></div>
    </section>}
    {busy && <StatusLine tone="unknown" text="Saving category revision" compact />}
  </section>;
}

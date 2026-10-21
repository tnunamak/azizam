// Plain category rules: a category is a named group of models and workflows with one
// rule, who may see what is in it. These helpers read and edit the same rules
// snapshot the gateway enforces; they never invent a second copy.
import { categoryId, type CatalogService, type Category, type Service, type Snapshot } from "./editor";

/** The one value of a plain category: "this item is in it". */
export const MEMBER_VALUE = "included";
const SOURCE = "owner";
const MAPPING_VERSION = "owner-edit-1";

export type Visibility = "everyone" | "owner" | "plans";
/** A bundled starter category (manifests/categories). Adding one writes the rows a custom category gets. */
export type Preset = { id: string; label: string; description: string; visibility: "owner" | "everyone" };
export type Item = { stem: string; serviceId: string; kind: "workflow" | "model"; label: string };

export const emptySnapshot = (): Snapshot => ({
  id: "owner-rules", categories: {}, people: {}, apps: {}, services: {}, completeness_gate: true, retained: {},
});
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
export const stemOf = (id: string) => (id.includes("@") ? id.slice(0, id.lastIndexOf("@")) : id);

/** A plain category holds one yes/no value. Categories with several values are access rules, shown under Advanced. */
export const isPlain = (category: Category) => category.values.length === 1 && category.values[0] === MEMBER_VALUE;
export const plainCategories = (snapshot: Snapshot) => Object.values(snapshot.categories).filter(isPlain);

export function visibilityOf(category: Category): Visibility {
  if (category.visible_to === undefined || category.visible_to === null) return "everyone";
  return category.visible_to.length ? "plans" : "owner";
}

/** Who can see a category, in the words the page shows. */
export function visibilityText(category: Category, planName: (id: string) => string): string {
  const kind = visibilityOf(category);
  if (kind === "everyone") return "Everyone who can use the gateway";
  if (kind === "owner") return "Only you";
  return "You; allowed plans: " + (category.visible_to ?? []).map(planName).join(", ");
}

function closure(service: Service): Service[] {
  return [service, ...service.dependencies.flatMap(closure)];
}
const hasMember = (service: Service, category: string) =>
  closure(service).some((s) => s.members.some((m) => m.assertions.some((a) => a.accepted && a.category === category)));

/** The models and workflows filed in a category. */
export function itemsIn(snapshot: Snapshot, category: string, catalog: CatalogService[]): Item[] {
  return Object.values(snapshot.services)
    .filter((service) => hasMember(service, category))
    .map((service) => {
      const stem = stemOf(service.id);
      const entry = catalog.find((item) => item.stem === stem);
      const kind: Item["kind"] = entry?.kind ?? (service.members[0]?.kind === "workflow" ? "workflow" : "model");
      return { stem, serviceId: service.id, kind, label: entry?.label ?? "Name unavailable" };
    })
    .sort((a, b) => a.label.localeCompare(b.label));
}

/** The plain category an item is in now, if any. */
export function categoryOf(snapshot: Snapshot, stem: string): string | null {
  const service = Object.values(snapshot.services).find((item) => stemOf(item.id) === stem);
  if (!service) return null;
  return plainCategories(snapshot).find((category) => hasMember(service, category.id))?.id ?? null;
}

function uniqueId(snapshot: Snapshot, label: string): string {
  const base = categoryId(label) || "category";
  let id = base;
  for (let n = 2; snapshot.categories[id] || id === "none" || id === "uncategorized"; n++) id = base + "-" + n;
  return id;
}

export type CategoryInput = { label: string; description: string; visibility: Visibility; plans: string[] };
const visibleTo = (input: CategoryInput): string[] | null =>
  input.visibility === "everyone" ? null : input.visibility === "owner" ? [] : [...input.plans];

/** Add a category, or change the one named by `id` (its items stay where they are). */
export function saveCategory(snapshot: Snapshot, input: CategoryInput, id?: string): { snapshot: Snapshot; id: string } {
  const next = clone(snapshot);
  const key = id ?? uniqueId(next, input.label);
  const existing = next.categories[key];
  next.categories[key] = {
    id: key, label: input.label.trim(), kind: "set", values: [MEMBER_VALUE],
    applies_to: existing?.applies_to ?? ["release", "workflow"],
    description: input.description.trim(), visible_to: visibleTo(input),
  };
  return { snapshot: next, id: key };
}

export const presetAdded = (snapshot: Snapshot, preset: Preset) => Boolean(snapshot.categories[preset.id]);

/** Add a bundled preset as an ordinary category: same rows as the Add category form. */
export function addPreset(snapshot: Snapshot, preset: Preset): Snapshot {
  if (presetAdded(snapshot, preset)) return snapshot;
  return saveCategory(snapshot, { label: preset.label, description: preset.description, visibility: preset.visibility, plans: [] }, preset.id).snapshot;
}

/** What stops a category from being deleted: an access rule that still refers to it. */
export function deleteBlocker(snapshot: Snapshot, category: string): string | null {
  const plans = [...Object.values(snapshot.people), ...Object.values(snapshot.apps)].flat();
  const used = plans.some((plan) => [...plan.require, ...plan.deny].some((row) => row.category === category));
  return used ? "An access rule still uses this category. Remove that rule under Advanced first." : null;
}

function stripAssertions(service: Service, category: string): void {
  for (const part of closure(service)) {
    for (const member of part.members) member.assertions = member.assertions.filter((a) => a.category !== category);
  }
}

/** Drop services that no longer say anything and that no access rule grants by name. */
function prune(snapshot: Snapshot): void {
  const granted = new Set([...Object.values(snapshot.people), ...Object.values(snapshot.apps)]
    .flat().flatMap((plan) => plan.grants.map((grant) => grant.service)));
  for (const [key, service] of Object.entries(snapshot.services)) {
    const empty = closure(service).every((part) => part.members.every((member) => member.assertions.length === 0));
    if (empty && !granted.has(key)) delete snapshot.services[key];
  }
}

export function deleteCategory(snapshot: Snapshot, category: string): Snapshot {
  const next = clone(snapshot);
  delete next.categories[category];
  for (const service of [...Object.values(next.services), ...Object.values(next.retained)]) stripAssertions(service, category);
  prune(next);
  return next;
}

/** Put a model or workflow in a category, or in none (`to` null). It leaves any plain category it was in. */
export function moveItem(snapshot: Snapshot, entry: CatalogService, to: string | null, now: string): Snapshot {
  const next = clone(snapshot);
  const found = Object.values(next.services).find((service) => stemOf(service.id) === entry.stem);
  if (found) for (const category of plainCategories(next)) stripAssertions(found, category.id);
  if (to !== null) {
    let service = found;
    if (!service) {
      const id = entry.stem + "@r1";
      service = { id, dependencies: [], checkable: true, members: [{ id, kind: entry.kind === "workflow" ? "workflow" : "release", assertions: [] }] };
      next.services[id] = service;
    }
    service.members[0].assertions.push({
      category: to, value: MEMBER_VALUE, source: SOURCE, accepted: true,
      observed_at: now, accepted_at: now, accepting_owner: "", mapping_version: MAPPING_VERSION,
    });
  }
  prune(next);
  return next;
}

"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Layers, Plus } from "lucide-react";
import { adminFetch, ApiError, api, queryKeys } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { EmptyState } from "@/components/product/empty-state";
import { Callout } from "@/components/product/status-ui";
import type { CatalogService, Category, Snapshot } from "./editor";
import {
  addPreset, presetAdded, type Preset,
  categoryOf, deleteBlocker, deleteCategory, itemsIn, moveItem, plainCategories, saveCategory,
  type CategoryInput, type Item, type Visibility, emptySnapshot, visibilityOf, visibilityText,
} from "./simple";

const root = "/admin/api/categories";
type ActiveRules = { revision: number; snapshot: Snapshot } | null;
type Dialogs =
  | { kind: "category"; id: string | null }
  | { kind: "add"; id: string }
  | { kind: "move"; item: Item }
  | null;

const itemKind = (item: Item) => (item.kind === "workflow" ? "Workflow" : "Model");
const asEntry = (item: Item): CatalogService => ({ stem: item.stem, label: item.label, kind: item.kind, capabilities: [] });

/** Save a rules snapshot: write the candidate, then turn it on only if nobody changed the rules since. */
async function writeRules(base: number | null, snapshot: Snapshot, reason: string): Promise<void> {
  const candidate = await adminFetch<{ revision: number }>(root + "/candidates", {
    method: "POST", body: { base_revision: base, reason, snapshot },
  });
  await adminFetch(root + "/candidates/" + candidate.revision + "/activate", {
    method: "POST", body: { expected_active_revision: base },
  });
}
const staleMessage = "Categories changed while you were editing. Review the latest settings before saving.";
const failure = (error: unknown) =>
  error instanceof ApiError && error.code === "stale_base"
    ? staleMessage
    : "We could not confirm that this change was saved. Reload the categories before trying again.";

export function CategoryList({ active, catalog, presets, catalogLoading, catalogError, onCatalogRetry, onSaved, adding, onAddingChange, picking, onPickingChange }: {
  active: ActiveRules; catalog: CatalogService[]; presets: Preset[]; catalogLoading: boolean; catalogError: boolean; onCatalogRetry: () => void;
  onSaved: () => Promise<unknown> | void; adding: boolean; onAddingChange: (open: boolean) => void; picking: boolean; onPickingChange: (open: boolean) => void;
}) {
  const { data: plans, isError: plansError, refetch: plansRefetch } = useQuery({ queryKey: queryKeys.accessPlans, queryFn: api.accessPlans.list });
  const [dialog, setDialog] = useState<Dialogs>(null);
  // Check access links to /categories?category=<id>: open that category's editor once,
  // when the rules have loaded. The requested id is held in state, read from the address once.
  const [requested, setRequested] = useState<string | null>(
    () => (typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("category")),
  );
  if (requested && active) {
    setRequested(null);
    if (active.snapshot.categories[requested]) setDialog({ kind: "category", id: requested });
  }
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recovery, setRecovery] = useState(0);
  const [status, setStatus] = useState("");
  const snapshot = active?.snapshot ?? emptySnapshot();
  const categories = plainCategories(snapshot);
  const planName = (id: string) => {
    if (!plans || plansError) return "access plans unavailable";
    const plan = plans.find((plan) => plan.id === id);
    if (!plan) return "a removed access plan";
    if (!plan.used_by) return plan.name + " (assignment counts unavailable)";
    const people = plan.used_by.people.length;
    const appCount = plan.used_by.apps.length;
    return `${plan.name} (${people} ${people === 1 ? "person" : "people"}, ${appCount} ${appCount === 1 ? "app" : "apps"}${!people && !appCount ? "; no one assigned" : ""})`;
  };
  const categoryDialog = adding ? { kind: "category" as const, id: null } : dialog;

  // The category being edited was deleted elsewhere: leave its editor and say why.
  // Adjusted during render, so the stale editor never paints.
  if (error === staleMessage && categoryDialog?.kind === "category" && categoryDialog.id && !snapshot.categories[categoryDialog.id]) {
    setDialog(null);
    setError("");
    setStatus("This category was deleted while you were editing.");
  }

  function close() {
    setDialog(null);
    onAddingChange(false);
    onPickingChange(false);
    setError("");
    setRecovery(0);
  }
  async function commit(next: Snapshot, reason: string, then?: () => void) {
    setBusy(true);
    setError("");
    try {
      await writeRules(active?.revision ?? null, next, reason);
      await onSaved();
      close();
      then?.();
    } catch (cause) {
      setError(failure(cause));
      await onSaved();
      if (cause instanceof ApiError && cause.code === "stale_base") setRecovery((value) => value + 1);
    } finally {
      setBusy(false);
    }
  }

  return <section aria-label="Categories" className="space-y-4">
    {status && <p role="status" className="text-meta">{status}</p>}
    {catalogError && <Callout as="div" severity="critical" title="Models and workflows couldn't be loaded." headerAction={<Button variant="outline" onClick={onCatalogRetry}>Try again</Button>} />}
    {!categories.length && <EmptyState icon={Layers} title="No categories yet"
      description="A category groups models and workflows and decides who can see them. Start with one for work that is not ready, or for adult content."
      action={<div className="flex flex-wrap gap-2">
        <Button onClick={() => onAddingChange(true)}><Plus aria-hidden="true" />Add category</Button>
        {presets.length > 0 && <Button variant="outline" onClick={() => onPickingChange(true)}>Start from a preset</Button>}
      </div>} />}
    {categories.map((category) => {
      const items = itemsIn(snapshot, category.id, catalog);
      const headingId = "category-" + category.id;
      return <section key={category.id} aria-labelledby={headingId} className="rounded-panel border bg-card">
        <div className="flex flex-wrap items-start justify-between gap-3 p-4 sm:p-6">
          <div className="min-w-0 space-y-1">
            <h2 id={headingId} className="text-heading">{category.label}</h2>
            {category.description && <p className="text-meta text-muted-foreground">{category.description}</p>}
            <p className="text-meta"><span className="text-muted-foreground">Who can see it: </span><span>{visibilityText(category, planName)}</span></p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" aria-label={"Edit " + category.label} onClick={() => setDialog({ kind: "category", id: category.id })}>Edit</Button>
            <Button variant="outline" aria-label={"Add items to " + category.label} onClick={() => setDialog({ kind: "add", id: category.id })}>Add items</Button>
          </div>
        </div>
        {items.length ? <ul className="divide-y border-t">
          {items.map((item) => <li key={item.serviceId} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
            <div className="min-w-0">
              <p className="text-body font-medium break-words">{item.label}</p>
              <p className="text-meta text-muted-foreground">{itemKind(item)}</p>
            </div>
            <Button variant="outline" aria-label={"Move " + item.label} onClick={() => setDialog({ kind: "move", item })}>Move</Button>
          </li>)}
        </ul> : <div className="border-t"><EmptyState inCard title={"Nothing in " + category.label + " yet"}
          description="Add models and workflows to control who can see them." /></div>}
      </section>;
    })}

    {picking && <PresetDialog presets={presets} snapshot={snapshot} busy={busy} error={error} onClose={close}
      onAdd={(preset) => commit(addPreset(snapshot, preset), "Add category " + preset.label)} />}
    {categoryDialog?.kind === "category" && (!categoryDialog.id || snapshot.categories[categoryDialog.id]) && <CategoryDialog
      key={categoryDialog.id ?? "new"} recovery={recovery} category={categoryDialog.id ? snapshot.categories[categoryDialog.id] : undefined}
      plans={plans ?? []} plansUnavailable={plansError} onRetryPlans={() => void plansRefetch()}
      busy={busy} error={error} blocker={categoryDialog.id ? deleteBlocker(snapshot, categoryDialog.id) : null} onClose={close}
      onSubmit={(input) => {
        const { snapshot: next } = saveCategory(snapshot, input, categoryDialog.id ?? undefined);
        return commit(next, (categoryDialog.id ? "Edit category " : "Add category ") + input.label.trim());
      }}
      onDelete={categoryDialog.id ? () => commit(deleteCategory(snapshot, categoryDialog.id!), "Delete category " + snapshot.categories[categoryDialog.id!].label) : undefined} />}
    {dialog?.kind === "add" && <AddItemsDialog key={dialog.id} category={snapshot.categories[dialog.id]} snapshot={snapshot} catalog={catalog}
      loading={catalogLoading} planName={planName} busy={busy} error={error} onClose={close}
      onSubmit={(entries) => {
        const now = new Date().toISOString();
        const next = entries.reduce((current, entry) => moveItem(current, entry, dialog.id, now), snapshot);
        return commit(next, "Add " + entries.length + " to " + snapshot.categories[dialog.id].label);
      }} />}
    {dialog?.kind === "move" && <MoveDialog key={dialog.item.stem} item={dialog.item} categories={categories} current={categoryOf(snapshot, dialog.item.stem)}
      planName={planName} busy={busy} error={error} onClose={close}
      onSubmit={(to) => commit(moveItem(snapshot, asEntry(dialog.item), to, new Date().toISOString()),
        to ? "Move " + dialog.item.label + " to " + snapshot.categories[to].label : "Take " + dialog.item.label + " out of its category")} />}
  </section>;
}

const visibilityChoices: { value: Visibility; label: string; help: string }[] = [
  { value: "everyone", label: "Everyone", help: "Anyone who can use the gateway can see and use what is in it." },
  { value: "owner", label: "Only me", help: "Nobody else sees it, including apps you run for other people." },
  { value: "plans", label: "Me and chosen access plans", help: "People and apps on these plans can also see it." },
];

function CategoryDialog({ category, recovery, plans, plansUnavailable, onRetryPlans, busy, error, blocker, onClose, onSubmit, onDelete }: {
  category?: Category; recovery: number; plans: { id: string; name: string }[]; plansUnavailable: boolean; onRetryPlans: () => void;
  busy: boolean; error: string; blocker: string | null; onClose: () => void;
  onSubmit: (input: CategoryInput) => Promise<void>; onDelete?: () => Promise<void>;
}) {
  const [label, setLabel] = useState(category?.label ?? "");
  const [description, setDescription] = useState(category?.description ?? "");
  const [visibility, setVisibility] = useState<Visibility>(category ? visibilityOf(category) : "owner");
  const [chosen, setChosen] = useState<string[]>(category?.visible_to ?? []);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [recoveredCategory, setRecoveredCategory] = useState(category);
  const [appliedRecovery, setAppliedRecovery] = useState(recovery);
  // After a stale-save recovery the latest saved values replace the form. The recovered
  // category and applied counter record what the form last took, so this runs once per
  // recovery or category change, during render.
  if (recovery && (recoveredCategory !== category || appliedRecovery !== recovery)) {
    setLabel(category?.label ?? "");
    setDescription(category?.description ?? "");
    setVisibility(category ? visibilityOf(category) : "owner");
    setChosen(category?.visible_to ?? []);
    setConfirmDelete(false);
    setRecoveredCategory(category);
    setAppliedRecovery(recovery);
  }
  const changed = label !== (category?.label ?? "") || description !== (category?.description ?? "")
    || visibility !== (category ? visibilityOf(category) : "owner")
    || (visibility === "plans" && (chosen.length !== (category?.visible_to ?? []).length || chosen.some((id) => !category?.visible_to?.includes(id))));
  const valid = (!recovery || changed) && label.trim().length > 0 && (visibility !== "plans" || chosen.length > 0);
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (valid && !busy) void onSubmit({ label, description, visibility, plans: chosen }); }}>
        <DialogHeader>
          <DialogTitle>{category ? "Edit " + category.label : "Add category"}</DialogTitle>
          <DialogDescription>{category ? "Change its name, its one-line meaning or who can see it." : "Name it, say what it is for and choose who can see what you put in it."}</DialogDescription>
        </DialogHeader>
        <Field label="Name" required><Input value={label} onChange={(event) => setLabel(event.target.value)} disabled={busy} autoComplete="off" /></Field>
        <Field label="What it is for" help="One short line, shown under the name."><Input value={description} onChange={(event) => setDescription(event.target.value)} disabled={busy} autoComplete="off" /></Field>
        <fieldset className="grid gap-2">
          <legend className="text-label mb-1">Who can see it</legend>
          <RadioGroup value={visibility} onValueChange={(value) => setVisibility(value as Visibility)} aria-label="Who can see it" disabled={busy}>
            {visibilityChoices.map((choice) => <label key={choice.value} className="flex cursor-pointer items-start gap-3 py-1 max-sm:min-h-11">
              <RadioGroupItem value={choice.value} className="mt-1" />
              <span className="min-w-0"><span className="text-body block font-medium">{choice.label}</span><span className="text-meta text-muted-foreground block">{choice.help}</span></span>
            </label>)}
          </RadioGroup>
          {visibility === "plans" && <div className="ml-7 grid gap-1" role="group" aria-label="Access plans that can see it">
            {plansUnavailable && <p className="text-meta">Access plans couldn&apos;t be loaded. <Button variant="outline" onClick={onRetryPlans}>Try again</Button></p>}
            {!plansUnavailable && !plans.length && <p className="text-meta text-muted-foreground">No access plans yet. Add one under People.</p>}
            {plans.map((plan) => <label key={plan.id} className="flex cursor-pointer items-center gap-3 py-1 max-sm:min-h-11">
              <Checkbox checked={chosen.includes(plan.id)} disabled={busy} aria-label={plan.name}
                onCheckedChange={(checked) => setChosen((current) => checked ? [...current, plan.id] : current.filter((id) => id !== plan.id))} />
              <span className="text-body">{plan.name}</span>
            </label>)}
          </div>}
          {visibility === "everyone" && <p className="text-meta">{category ? "Saving makes everything in this category available to everyone." : "Everything you put in this category will be available to everyone."}</p>}
        </fieldset>
        {error && <p role="alert" className="text-meta text-danger">{error}</p>}
        <DialogFooter className="gap-2 sm:justify-between">
          {onDelete ? <Button type="button" variant="outline" disabled={busy || blocker !== null} onClick={() => setConfirmDelete(true)}>Delete category</Button> : <span />}
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" variant="outline" disabled={busy} onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!valid || busy}>{category ? "Save" : "Add category"}</Button>
          </div>
        </DialogFooter>
        {blocker && <p className="text-meta text-muted-foreground">{blocker}</p>}
      </form>
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} title={"Delete " + (category?.label ?? "category") + "?"}
        description="Its models and workflows stay where they are but are no longer in a category. Anyone who could not see them before will be able to." confirmLabel="Delete category"
        onConfirm={async () => { await onDelete?.(); }} />
    </DialogContent>
  </Dialog>;
}

function AddItemsDialog({ category, snapshot, catalog, loading, planName, busy, error, onClose, onSubmit }: {
  planName: (id: string) => string;
  category: Category; snapshot: Snapshot; catalog: CatalogService[]; loading: boolean; busy: boolean; error: string;
  onClose: () => void; onSubmit: (entries: CatalogService[]) => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const here = new Set(itemsIn(snapshot, category.id, catalog).map((item) => item.stem));
  const shown = catalog.filter((entry) => !here.has(entry.stem) && entry.label.toLowerCase().includes(query.trim().toLowerCase()))
    .sort((a, b) => a.kind.localeCompare(b.kind) || a.label.localeCompare(b.label));
  const elsewhere = (stem: string) => {
    const id = categoryOf(snapshot, stem);
    return id && id !== category.id ? snapshot.categories[id]?.label : "";
  };
  const leaving = picked.filter((stem) => Boolean(elsewhere(stem))).length;
  const promoting = picked.some((stem) => widens(snapshot.categories[categoryOf(snapshot, stem) ?? ""], category));
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (picked.length && !busy) void onSubmit(catalog.filter((entry) => picked.includes(entry.stem))); }}>
        <DialogHeader>
          <DialogTitle>Add items to {category.label}</DialogTitle>
          <DialogDescription>Choose the models and workflows to put in this category. An item can be in one category at a time.</DialogDescription>
        </DialogHeader>
        <p className="text-meta">Who can see it: {visibilityText(category, planName)}</p>
        <p className="text-meta">{leaving} selected {leaving === 1 ? "item" : "items"} will leave another category.</p>
        {picked.map((stem) => <p key={stem} className="text-meta">Move {catalog.find((entry) => entry.stem === stem)?.label} to {category.label}. Who can see it changes from {audienceText(snapshot.categories[categoryOf(snapshot, stem) ?? ""], planName)} to {visibilityText(category, planName)}.</p>)}
        {promoting && <p className="text-meta">Anyone who could not see these items before may be able to.</p>}
        <Field label="Search"><Input value={query} onChange={(event) => setQuery(event.target.value)} autoComplete="off" /></Field>
        <div role="group" aria-label="Models and workflows" className="max-h-72 overflow-y-auto rounded-control border">
          {loading && <p role="status" className="text-meta text-muted-foreground p-3">Loading models and workflows…</p>}
          {!loading && !shown.length && <p className="text-meta text-muted-foreground p-3">{catalog.length ? "Nothing matches your search." : "There are no models or workflows to add yet."}</p>}
          {shown.map((entry) => <label key={entry.stem} className="flex cursor-pointer items-center gap-3 px-3 py-2 max-sm:min-h-11">
            <Checkbox checked={picked.includes(entry.stem)} disabled={busy} aria-label={entry.label}
              onCheckedChange={(checked) => setPicked((current) => checked ? [...current, entry.stem] : current.filter((stem) => stem !== entry.stem))} />
            <span className="min-w-0">
              <span className="text-body block break-words">{entry.label}</span>
              <span className="text-meta text-muted-foreground block">{entry.kind === "workflow" ? "Workflow" : "Model"}{elsewhere(entry.stem) ? " · Now in " + elsewhere(entry.stem) : ""}</span>
            </span>
          </label>)}
        </div>
        {error && <p role="alert" className="text-meta text-danger">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!picked.length || busy}>{picked.length > 1 ? "Add " + picked.length + " items" : "Add item"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}

const audienceText = (category: Category | undefined, planName: (id: string) => string) =>
  category ? visibilityText(category, planName) : "Everyone who can use the gateway";
const widens = (from: Category | undefined, to: Category | undefined): boolean => {
  const before = from?.visible_to;
  const after = to?.visible_to;
  if (before == null) return false;
  return after == null || after.some((id) => !before.includes(id));
};

function MoveDialog({ item, categories, current, planName, busy, error, onClose, onSubmit }: {
  planName: (id: string) => string;
  item: Item; categories: Category[]; current: string | null; busy: boolean; error: string;
  onClose: () => void; onSubmit: (to: string | null) => Promise<void>;
}) {
  const initial = current === null ? "uncategorized" : "category:" + current;
  const [to, setTo] = useState(initial);
  const destination = to === "uncategorized" ? null : to.startsWith("category:") ? to.slice("category:".length) : undefined;
  const fromCategory = categories.find((category) => category.id === current);
  const toCategory = categories.find((category) => category.id === destination);
  const promoting = widens(fromCategory, toCategory);
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (to !== initial && destination !== undefined && !busy) void onSubmit(destination); }}>
        <DialogHeader>
          <DialogTitle>Move {item.label}</DialogTitle>
          <DialogDescription>Choose where it goes. Its new category decides who can see it.</DialogDescription>
        </DialogHeader>
        <RadioGroup value={to} onValueChange={setTo} aria-label="Move to" disabled={busy}>
          {categories.map((category) => <label key={category.id} className="flex cursor-pointer items-center gap-3 py-1 max-sm:min-h-11">
            <RadioGroupItem value={"category:" + category.id} aria-label={category.label} /><span className="text-body">{category.label}</span><span className="text-meta text-muted-foreground">{visibilityText(category, planName)}</span>
          </label>)}
          <label className="flex cursor-pointer items-center gap-3 py-1 max-sm:min-h-11">
            <RadioGroupItem value="uncategorized" aria-label="No category" /><span className="text-body">No category</span><span className="text-meta text-muted-foreground">Everyone who can use the gateway</span>
          </label>
        </RadioGroup>
        <p className="text-meta">Move {item.label} to {toCategory?.label ?? "No category"}. Who can see it changes from {audienceText(fromCategory, planName)} to {audienceText(toCategory, planName)}.</p>
        {promoting && <p className="text-meta">{destination === null ? "With no category, everyone who can use the gateway can see and use it. " : ""}Anyone who could not see it before may be able to.</p>}
        {error && <p role="alert" className="text-meta text-danger">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy || to === initial || destination === undefined}>Move</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}

function PresetDialog({ presets, snapshot, busy, error, onClose, onAdd }: {
  presets: Preset[]; snapshot: Snapshot; busy: boolean; error: string; onClose: () => void; onAdd: (preset: Preset) => Promise<void>;
}) {
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Start from a preset</DialogTitle>
        <DialogDescription>Add a ready-made category. It is an ordinary category: rename it, change who can see it or delete it afterwards.</DialogDescription>
      </DialogHeader>
      <ul className="divide-y rounded-control border">
        {presets.map((preset) => {
          const added = presetAdded(snapshot, preset);
          return <li key={preset.id} className="flex flex-wrap items-center justify-between gap-3 px-3 py-3">
            <div className="min-w-0">
              <p className="text-body font-medium">{preset.label}</p>
              <p className="text-meta text-muted-foreground">{preset.description}</p>
              <p className="text-meta text-muted-foreground">{preset.visibility === "owner" ? "At first only you can see it." : "At first everyone can see it."}</p>
            </div>
            {added ? <span className="text-meta text-muted-foreground" aria-label={preset.label + " is already added"}>Added</span>
              : <Button variant="outline" disabled={busy} aria-label={"Add " + preset.label} onClick={() => void onAdd(preset)}>Add</Button>}
          </li>;
        })}
      </ul>
      {error && <p role="alert" className="text-meta text-danger">{error}</p>}
      <DialogFooter><Button type="button" variant="outline" disabled={busy} onClick={onClose}>Close</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}

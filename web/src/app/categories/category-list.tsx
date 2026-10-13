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
const failure = (error: unknown) =>
  error instanceof ApiError && error.code === "stale_base"
    ? "The categories changed while you were editing. Close this and try again."
    : "The change was not saved. Try again.";

export function CategoryList({ active, catalog, catalogLoading, catalogError, onCatalogRetry, onSaved, adding, onAddingChange }: {
  active: ActiveRules; catalog: CatalogService[]; catalogLoading: boolean; catalogError: boolean; onCatalogRetry: () => void;
  onSaved: () => Promise<unknown> | void; adding: boolean; onAddingChange: (open: boolean) => void;
}) {
  const { data: plans, isError: plansError, refetch: plansRefetch } = useQuery({ queryKey: queryKeys.accessPlans, queryFn: api.accessPlans.list });
  const [dialog, setDialog] = useState<Dialogs>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const snapshot = active?.snapshot ?? emptySnapshot();
  const categories = plainCategories(snapshot);
  const planName = (id: string) => !plans ? "…" : plans.find((plan) => plan.id === id)?.name ?? "a removed access plan";
  const categoryDialog = adding ? { kind: "category" as const, id: null } : dialog;

  function close() {
    setDialog(null);
    onAddingChange(false);
    setError("");
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
    } finally {
      setBusy(false);
    }
  }

  return <section aria-label="Categories" className="space-y-4">
    {catalogError && <Callout as="div" severity="critical" title="Models and workflows couldn't be loaded." headerAction={<Button variant="outline" onClick={onCatalogRetry}>Try again</Button>} />}
    {!categories.length && <EmptyState icon={Layers} title="No categories yet"
      description="A category groups models and workflows and decides who can see them. Start with one for work that is not ready, or for adult content."
      action={<Button onClick={() => onAddingChange(true)}><Plus aria-hidden="true" />Add category</Button>} />}
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

    {categoryDialog?.kind === "category" && <CategoryDialog
      key={categoryDialog.id ?? "new"} category={categoryDialog.id ? snapshot.categories[categoryDialog.id] : undefined}
      plans={plans ?? []} plansUnavailable={plansError} onRetryPlans={() => void plansRefetch()}
      busy={busy} error={error} blocker={categoryDialog.id ? deleteBlocker(snapshot, categoryDialog.id) : null} onClose={close}
      onSubmit={(input) => {
        const { snapshot: next } = saveCategory(snapshot, input, categoryDialog.id ?? undefined);
        return commit(next, (categoryDialog.id ? "Edit category " : "Add category ") + input.label.trim());
      }}
      onDelete={categoryDialog.id ? () => commit(deleteCategory(snapshot, categoryDialog.id!), "Delete category " + snapshot.categories[categoryDialog.id!].label) : undefined} />}
    {dialog?.kind === "add" && <AddItemsDialog key={dialog.id} category={snapshot.categories[dialog.id]} snapshot={snapshot} catalog={catalog}
      loading={catalogLoading} busy={busy} error={error} onClose={close}
      onSubmit={(entries) => {
        const now = new Date().toISOString();
        const next = entries.reduce((current, entry) => moveItem(current, entry, dialog.id, now), snapshot);
        return commit(next, "Add " + entries.length + " to " + snapshot.categories[dialog.id].label);
      }} />}
    {dialog?.kind === "move" && <MoveDialog key={dialog.item.stem} item={dialog.item} categories={categories} current={categoryOf(snapshot, dialog.item.stem)}
      busy={busy} error={error} onClose={close}
      onSubmit={(to) => commit(moveItem(snapshot, asEntry(dialog.item), to, new Date().toISOString()),
        to ? "Move " + dialog.item.label + " to " + snapshot.categories[to].label : "Take " + dialog.item.label + " out of its category")} />}
  </section>;
}

const visibilityChoices: { value: Visibility; label: string; help: string }[] = [
  { value: "everyone", label: "Everyone", help: "Anyone who can use the gateway can see and use what is in it." },
  { value: "owner", label: "Only me", help: "Nobody else sees it, including apps you run for other people." },
  { value: "plans", label: "Me and chosen access plans", help: "People and apps on these plans can also see it." },
];

function CategoryDialog({ category, plans, plansUnavailable, onRetryPlans, busy, error, blocker, onClose, onSubmit, onDelete }: {
  category?: Category; plans: { id: string; name: string }[]; plansUnavailable: boolean; onRetryPlans: () => void;
  busy: boolean; error: string; blocker: string | null; onClose: () => void;
  onSubmit: (input: CategoryInput) => Promise<void>; onDelete?: () => Promise<void>;
}) {
  const [label, setLabel] = useState(category?.label ?? "");
  const [description, setDescription] = useState(category?.description ?? "");
  const [visibility, setVisibility] = useState<Visibility>(category ? visibilityOf(category) : "owner");
  const [chosen, setChosen] = useState<string[]>(category?.visible_to ?? []);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const valid = label.trim().length > 0 && (visibility !== "plans" || chosen.length > 0);
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

function AddItemsDialog({ category, snapshot, catalog, loading, busy, error, onClose, onSubmit }: {
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
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (picked.length && !busy) void onSubmit(catalog.filter((entry) => picked.includes(entry.stem))); }}>
        <DialogHeader>
          <DialogTitle>Add items to {category.label}</DialogTitle>
          <DialogDescription>Choose the models and workflows to put in this category. An item can be in one category at a time.</DialogDescription>
        </DialogHeader>
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

function MoveDialog({ item, categories, current, busy, error, onClose, onSubmit }: {
  item: Item; categories: Category[]; current: string | null; busy: boolean; error: string;
  onClose: () => void; onSubmit: (to: string | null) => Promise<void>;
}) {
  const [to, setTo] = useState(current ?? "none");
  const promoting = current !== null && to === "none";
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (to !== (current ?? "none") && !busy) void onSubmit(to === "none" ? null : to); }}>
        <DialogHeader>
          <DialogTitle>Move {item.label}</DialogTitle>
          <DialogDescription>Choose where it goes. Its new category decides who can see it.</DialogDescription>
        </DialogHeader>
        <RadioGroup value={to} onValueChange={setTo} aria-label="Move to" disabled={busy}>
          {categories.map((category) => <label key={category.id} className="flex cursor-pointer items-center gap-3 py-1 max-sm:min-h-11">
            <RadioGroupItem value={category.id} /><span className="text-body">{category.label}</span>
          </label>)}
          <label className="flex cursor-pointer items-center gap-3 py-1 max-sm:min-h-11">
            <RadioGroupItem value="none" /><span className="text-body">No category</span>
          </label>
        </RadioGroup>
        {promoting && <p className="text-meta">With no category, everyone who can use the gateway can see and use it.</p>}
        {error && <p role="alert" className="text-meta text-danger">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy || to === (current ?? "none")}>Move</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}

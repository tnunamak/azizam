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
  type CategoryInput, type Item, emptySnapshot, reachText,
} from "./simple";
import { AudienceSummary, audienceNeedsReview, type Audience, type AudiencePreview } from "./audience-preview";

const root = "/admin/api/categories";
type ActiveRules = { revision: number; snapshot: Snapshot } | null;
/** A candidate the list wrote and held back: it is turned on only when the owner chooses Turn on. */
type Review = { revision: number; base: number | null; audience: Audience | null };
type Dialogs =
  | { kind: "category"; id: string | null }
  | { kind: "add"; id: string }
  | { kind: "move"; item: Item }
  | null;

const itemKind = (item: Item) => (item.kind === "workflow" ? "Workflow" : "Model");
const asEntry = (item: Item): CatalogService => ({ stem: item.stem, label: item.label, kind: item.kind, capabilities: [] });

/** Write a rules snapshot as a candidate. Nothing is turned on yet; the preview says who it reaches. */
const writeCandidate = (base: number | null, snapshot: Snapshot, reason: string) =>
  adminFetch<{ revision: number; preview: AudiencePreview }>(root + "/candidates", {
    method: "POST", body: { base_revision: base, reason, snapshot },
  });
/** Turn a candidate on, but only if the active rules are still the base it was written against. */
const turnOn = (revision: number, base: number | null) =>
  adminFetch(root + "/candidates/" + revision + "/activate", {
    method: "POST", body: { expected_active_revision: base },
  });
const staleMessage = "Categories changed while you were editing. Review the latest settings before saving.";
const failure = (error: unknown) =>
  error instanceof ApiError && error.code === "stale_base"
    ? staleMessage
    : "We could not confirm that this change was saved. Reload the categories before trying again.";

export function CategoryList({ active, catalog, presets, catalogLoading, catalogError, onCatalogRetry, onSaved, adding, onAddingChange, picking, onPickingChange }: {
  active: ActiveRules; catalog: CatalogService[]; presets: Preset[]; catalogLoading: boolean; catalogError: boolean; onCatalogRetry: () => void;
  onSaved: () => Promise<unknown> | void; adding: boolean; onAddingChange: (open: boolean) => void; picking: boolean; onPickingChange: (open: boolean) => void;
}) {
  const { data: plans, isError: plansError } = useQuery({ queryKey: queryKeys.accessPlans, queryFn: api.accessPlans.list });
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
  const [review, setReview] = useState<Review | null>(null);
  const snapshot = active?.snapshot ?? emptySnapshot();
  const categories = plainCategories(snapshot);
  // Which access plans include a category is read-only here: each plan's categories are set under People.
  const reach = (category: Category) => {
    if (plansError) return "Access plans couldn't be loaded";
    if (!plans) return "Loading access plans…";
    return reachText(category, plans.map((plan) => ({ name: plan.name, categories: plan.plan.categories })));
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
  /** Show a failed save on the open dialog, refresh the rules, and reload the form after a stale base. */
  async function reportFailure(cause: unknown) {
    setError(failure(cause));
    await onSaved();
    if (cause instanceof ApiError && cause.code === "stale_base") setRecovery((value) => value + 1);
  }
  /** Save a rules snapshot. It turns on at once when nobody gains or loses a category; otherwise the owner confirms first. */
  async function commit(next: Snapshot, reason: string) {
    const base = active?.revision ?? null;
    setBusy(true);
    setError("");
    try {
      const candidate = await writeCandidate(base, next, reason);
      const audience = candidate.preview.audience;
      if (audienceNeedsReview(audience)) {
        setReview({ revision: candidate.revision, base, audience });
        return;
      }
      await turnOn(candidate.revision, base);
      await onSaved();
      close();
    } catch (cause) {
      await reportFailure(cause);
    } finally {
      setBusy(false);
    }
  }
  /** The owner chose Turn on for the candidate the review showed. */
  async function turnOnReviewed() {
    if (!review) return;
    setBusy(true);
    setError("");
    try {
      await turnOn(review.revision, review.base);
      setReview(null);
      await onSaved();
      close();
    } catch (cause) {
      setReview(null);
      await reportFailure(cause);
    } finally {
      setBusy(false);
    }
  }

  return <section aria-label="Categories" className="space-y-4">
    {status && <p role="status" className="text-meta">{status}</p>}
    {catalogError && <Callout as="div" severity="critical" title="Models and workflows couldn't be loaded." headerAction={<Button variant="outline" onClick={onCatalogRetry}>Try again</Button>} />}
    {!categories.length && <EmptyState icon={Layers} title="No categories yet"
      description="A category groups models and workflows. Access plans decide who can use each category. Start with one for work that is not ready, or for adult content."
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
            <p className="text-meta"><span className="text-muted-foreground">Access plans that include it: </span><span>{reach(category)}</span></p>
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
          description="Add models and workflows to put them in this category." /></div>}
      </section>;
    })}

    {picking && <PresetDialog presets={presets} snapshot={snapshot} busy={busy} error={error} onClose={close}
      onAdd={(preset) => commit(addPreset(snapshot, preset), "Add category " + preset.label)} />}
    {categoryDialog?.kind === "category" && (!categoryDialog.id || snapshot.categories[categoryDialog.id]) && <CategoryDialog
      key={categoryDialog.id ?? "new"} recovery={recovery} category={categoryDialog.id ? snapshot.categories[categoryDialog.id] : undefined}
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
      reach={reach} busy={busy} error={error} onClose={close}
      onSubmit={(to) => commit(moveItem(snapshot, asEntry(dialog.item), to, new Date().toISOString()),
        to ? "Move " + dialog.item.label + " to " + snapshot.categories[to].label : "Take " + dialog.item.label + " out of its category")} />}
    {review && <TurnOnDialog audience={review.audience} busy={busy} onTurnOn={() => void turnOnReviewed()} onCancel={() => setReview(null)} />}
  </section>;
}

function CategoryDialog({ category, recovery, busy, error, blocker, onClose, onSubmit, onDelete }: {
  category?: Category; recovery: number;
  busy: boolean; error: string; blocker: string | null; onClose: () => void;
  onSubmit: (input: CategoryInput) => Promise<void>; onDelete?: () => Promise<void>;
}) {
  const [label, setLabel] = useState(category?.label ?? "");
  const [description, setDescription] = useState(category?.description ?? "");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [recoveredCategory, setRecoveredCategory] = useState(category);
  const [appliedRecovery, setAppliedRecovery] = useState(recovery);
  // After a stale-save recovery the latest saved values replace the form. The recovered
  // category and applied counter record what the form last took, so this runs once per
  // recovery or category change, during render.
  if (recovery && (recoveredCategory !== category || appliedRecovery !== recovery)) {
    setLabel(category?.label ?? "");
    setDescription(category?.description ?? "");
    setConfirmDelete(false);
    setRecoveredCategory(category);
    setAppliedRecovery(recovery);
  }
  const changed = label !== (category?.label ?? "") || description !== (category?.description ?? "");
  const valid = (!recovery || changed) && label.trim().length > 0;
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (valid && !busy) void onSubmit({ label, description }); }}>
        <DialogHeader>
          <DialogTitle>{category ? "Edit " + category.label : "Add category"}</DialogTitle>
          <DialogDescription>{category ? "Change its name or its one-line meaning." : "Name it and say what it is for."}</DialogDescription>
        </DialogHeader>
        <Field label="Name" required><Input value={label} onChange={(event) => setLabel(event.target.value)} disabled={busy} autoComplete="off" /></Field>
        <Field label="What it is for" help="One short line, shown under the name."><Input value={description} onChange={(event) => setDescription(event.target.value)} disabled={busy} autoComplete="off" /></Field>
        <p className="text-meta text-muted-foreground">Access plans decide who can use what is in a category. Each plan lists its categories under People.</p>
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
        description="Its models and workflows stay where they are but are no longer in a category. Anyone who could not use them before will be able to." confirmLabel="Delete category"
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
  const leaving = picked.filter((stem) => Boolean(elsewhere(stem))).length;
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (picked.length && !busy) void onSubmit(catalog.filter((entry) => picked.includes(entry.stem))); }}>
        <DialogHeader>
          <DialogTitle>Add items to {category.label}</DialogTitle>
          <DialogDescription>Choose the models and workflows to put in this category. An item can be in one category at a time.</DialogDescription>
        </DialogHeader>
        <p className="text-meta">{leaving} selected {leaving === 1 ? "item" : "items"} will leave another category.</p>
        {picked.map((stem) => <p key={stem} className="text-meta">Move {catalog.find((entry) => entry.stem === stem)?.label} to {category.label}.</p>)}
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

function MoveDialog({ item, categories, current, reach, busy, error, onClose, onSubmit }: {
  item: Item; categories: Category[]; current: string | null; reach: (category: Category) => string; busy: boolean; error: string;
  onClose: () => void; onSubmit: (to: string | null) => Promise<void>;
}) {
  const initial = current === null ? "uncategorized" : "category:" + current;
  const [to, setTo] = useState(initial);
  const destination = to === "uncategorized" ? null : to.startsWith("category:") ? to.slice("category:".length) : undefined;
  const toCategory = categories.find((category) => category.id === destination);
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <DialogContent>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (to !== initial && destination !== undefined && !busy) void onSubmit(destination); }}>
        <DialogHeader>
          <DialogTitle>Move {item.label}</DialogTitle>
          <DialogDescription>Choose where it goes. The access plans that include its new category decide who can use it.</DialogDescription>
        </DialogHeader>
        <RadioGroup value={to} onValueChange={setTo} aria-label="Move to" disabled={busy}>
          {categories.map((category) => <label key={category.id} className="flex cursor-pointer items-center gap-3 py-1 max-sm:min-h-11">
            <RadioGroupItem value={"category:" + category.id} aria-label={category.label} /><span className="text-body">{category.label}</span><span className="text-meta text-muted-foreground">{reach(category)}</span>
          </label>)}
          <label className="flex cursor-pointer items-center gap-3 py-1 max-sm:min-h-11">
            <RadioGroupItem value="uncategorized" aria-label="No category" /><span className="text-body">No category</span><span className="text-meta text-muted-foreground">Not limited by any category</span>
          </label>
        </RadioGroup>
        <p className="text-meta">Move {item.label} to {toCategory?.label ?? "No category"}.</p>
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
        <DialogDescription>Add a ready-made category. It is an ordinary category: rename it or delete it afterwards. Access plans decide who can use it.</DialogDescription>
      </DialogHeader>
      <ul className="divide-y rounded-control border">
        {presets.map((preset) => {
          const added = presetAdded(snapshot, preset);
          return <li key={preset.id} className="flex flex-wrap items-center justify-between gap-3 px-3 py-3">
            <div className="min-w-0">
              <p className="text-body font-medium">{preset.label}</p>
              <p className="text-meta text-muted-foreground">{preset.description}</p>
            </div>
            {added ? <span className="text-meta text-muted-foreground">Added<span className="sr-only">: {preset.label} is already added</span></span>
              : <Button variant="outline" disabled={busy} aria-label={"Add " + preset.label} onClick={() => void onAdd(preset)}>Add</Button>}
          </li>;
        })}
      </ul>
      {error && <p role="alert" className="text-meta text-danger">{error}</p>}
      <DialogFooter><Button type="button" variant="outline" disabled={busy} onClick={onClose}>Close</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}

/** The owner's last look before a change turns on. Cancel turns nothing on and returns to the form, edits kept. */
function TurnOnDialog({ audience, busy, onTurnOn, onCancel }: {
  audience: Audience | null; busy: boolean; onTurnOn: () => void; onCancel: () => void;
}) {
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onCancel(); }}>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Turn on these category rules?</DialogTitle>
        <DialogDescription>Nothing changes until you choose Turn on.</DialogDescription>
      </DialogHeader>
      <AudienceSummary audience={audience} />
      <DialogFooter>
        <Button type="button" variant="outline" disabled={busy} onClick={onCancel}>Cancel</Button>
        <Button type="button" disabled={busy} onClick={onTurnOn}>Turn on</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

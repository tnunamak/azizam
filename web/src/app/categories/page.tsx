"use client";

import { useContext, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Lock, Plus } from "lucide-react";
import { adminFetch } from "@/lib/api";
import { ViewerContext } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { Callout, StatusLine } from "@/components/product/status-ui";
import { AdvancedRules, loadActive } from "./advanced";
import { CategoryList } from "./category-list";
import type { CatalogService } from "./editor";
import { plainCategories, type Preset } from "./simple";

const root = "/admin/api/categories";

/** Kept in step with the explainer at the top of docs/design/CATEGORIES_AND_PLANS.md. */
const HOW_IT_WORKS = [
  "A category is a group of models and workflows with one rule: who may see what is in it. You decide what a category means. The gateway does not know what “Adult” or “Experimental” means. It only follows the rule you set. Nothing is filed automatically; an item is in a category only when you put it there.",
  "A category can be visible to everyone, to you alone, or to you and the people and apps on the access plans you choose. Someone who cannot see a category does not find its items in model lists, workflow lists or the showcase, and cannot run them. You see everything when you use your own key or this console. An app you run for other people is treated like any other app.",
  "Items in no category are unaffected. Move an item out of its category and it is available again.",
];

export default function CategoriesPage() {
  const viewer = useContext(ViewerContext);
  if (!viewer?.authenticated || viewer.role !== "owner") return <div className="space-y-6">
    <PageHeader title="Categories" />
    <EmptyState icon={Lock} title="Only the owner can manage categories" description="Ask the gateway owner to review these rules." />
  </div>;
  return <OwnerCategories />;
}

function OwnerCategories() {
  const [adding, setAdding] = useState(false);
  const [picking, setPicking] = useState(false);
  const { data: catalogData, isFetching: catalogLoading, isError: catalogError, refetch: catalogRefetch } = useQuery({
    queryKey: ["categories", "services"], queryFn: () => adminFetch<{ services: CatalogService[]; presets?: Preset[] }>(root + "/services"),
    retry: false, refetchOnWindowFocus: false,
  });
  const { data: active, isError: activeError, isPending: activePending, refetch: activeRefetch } = useQuery({
    queryKey: ["categories", "active"], queryFn: loadActive, retry: false, refetchOnWindowFocus: false,
  });
  const ready = !activePending && !activeError && active !== undefined;
  const hasCategories = ready && active !== null && plainCategories(active.snapshot).length > 0;
  return <div className="max-w-(--content-read) space-y-8">
    <PageHeader title="Categories" subtitle="Group models and workflows; choose who sees them."
      primaryAction={hasCategories
        ? <Button onClick={() => setAdding(true)}><Plus aria-hidden="true" />Add category</Button> : undefined}
      actions={hasCategories && catalogData?.presets?.length
        ? <Button variant="outline" onClick={() => setPicking(true)}>Start from a preset</Button> : undefined} />
    <details className="text-meta -mt-4">
      <summary className="flex min-h-8 w-fit cursor-pointer items-center max-sm:min-h-11">How categories work</summary>
      <div className="text-muted-foreground mt-2 max-w-2xl space-y-3">{HOW_IT_WORKS.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}</div>
    </details>
    {activePending && <div aria-busy="true" aria-label="Loading categories"><StatusLine tone="unknown" text="Loading categories" compact /></div>}
    {activeError && <Callout as="div" severity="critical" title="Couldn't load categories" headerAction={<Button variant="outline" onClick={() => void activeRefetch()}>Try again</Button>} />}
    {ready && <CategoryList active={active ?? null} catalog={catalogData?.services ?? []} presets={catalogData?.presets ?? []} catalogLoading={catalogLoading} catalogError={catalogError}
      onCatalogRetry={() => void catalogRefetch()} onSaved={activeRefetch} adding={adding} onAddingChange={setAdding} picking={picking} onPickingChange={setPicking} />}
    <details>
      <summary className="text-meta flex min-h-8 w-fit cursor-pointer items-center max-sm:min-h-11">Advanced: rules for specific people and apps</summary>
      <div className="mt-4"><AdvancedRules /></div>
    </details>
  </div>;
}

"use client";
import { useId, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  api,
  queryKeys,
  type PlanCaps,
  type AccessPlan,
  type AccessPreviewEntry,
  type AccessRule,
  type AccessPlanDocument,
  type AccessPlanEditorContract,
  type CapabilityId,
  type PlanItem,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { ErrorState } from "@/components/product";
import { FilterToggle } from "@/components/product/filter-toggle";
import { StateBadge } from "@/components/product/status-ui";

const errorMessage = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The change could not be saved. Try again.";

const DOCUMENT_FIELDS = [
  "name",
  "can_use",
  "direct_engine_access",
  "paid_providers",
  "provider_fallback",
  "caps",
  "can_load_models",
  "receipt_detail",
  "media_metadata",
] as const;

/** Every field is required (design §3.1); the server validates the rest. */
export function planShapeError(value: unknown): string | null {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    return "Enter a valid JSON object before saving.";
  const missing = DOCUMENT_FIELDS.find((field) => !(field in value));
  return missing ? `The plan needs the field ${missing}.` : null;
}

const CAP_FIELDS: { key: keyof PlanCaps; label: string }[] = [
  { key: "usd_per_request", label: "Per request" },
  { key: "usd_per_day", label: "Per day" },
  { key: "usd_per_month", label: "Per month" },
];

/** Blank means no cap; the server checks that a cap is a positive amount. */
function SpendingCaps({
  caps,
  change,
  disabled,
}: {
  caps: PlanCaps;
  change: (caps: PlanCaps) => void;
  disabled: boolean;
}) {
  return (
    <fieldset className="space-y-3">
      <legend className="text-label">Spending caps</legend>
      <p className="text-muted-foreground text-meta">
        In US dollars, for paid models. The cap counts what the person, or the
        app, has spent in total. Leave a box empty for no cap.
      </p>
      <div className="grid gap-3 sm:grid-cols-3">
        {CAP_FIELDS.map(({ key, label }) => (
          <div key={key} className="space-y-2">
            <Label htmlFor={`plan-cap-${key}`}>{label}</Label>
            <Input
              id={`plan-cap-${key}`}
              inputMode="decimal"
              value={caps[key] ?? ""}
              disabled={disabled}
              onChange={(e) =>
                change({ ...caps, [key]: e.target.value.trim() || null })
              }
            />
          </div>
        ))}
      </div>
    </fieldset>
  );
}

const itemKey = (item: PlanItem) =>
  "model" in item ? `model:${item.model}` : `workflow:${item.workflow}`;

function CanUse({
  contract,
  value,
  change,
}: {
  contract: AccessPlanEditorContract;
  value: AccessPlanDocument["can_use"];
  change: (value: AccessPlanDocument["can_use"]) => void;
}) {
  const set = (id: CapabilityId, grant: { items: "all" | PlanItem[] } | null) => {
    const next = { ...value };
    if (grant === null) delete next[id];
    else next[id] = grant;
    change(next);
  };
  return (
    <fieldset className="space-y-3">
      <legend className="text-label">Can use</legend>
      <p className="text-muted-foreground text-meta">
        Nothing is allowed unless it is listed here.
      </p>
      {contract.capabilities.map((capability) => {
        const grant = value[capability.id];
        const listed =
          grant && grant.items !== "all"
            ? new Set(grant.items.map(itemKey))
            : null;
        return (
          <div key={capability.id} className="space-y-2">
            <label className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11">
              <Checkbox
                checked={grant !== undefined}
                onCheckedChange={(checked) =>
                  set(capability.id, checked ? { items: "all" } : null)
                }
              />
              {capability.label}
            </label>
            {grant && capability.items.length > 0 && (
              <div className="space-y-2 pl-7">
                <label className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11">
                  <Checkbox
                    checked={grant.items === "all"}
                    onCheckedChange={(checked) =>
                      set(capability.id, { items: checked ? "all" : [] })
                    }
                  />
                  All {capability.label.toLowerCase()} models and workflows,
                  including ones added later
                </label>
                {listed && (
                  <div className="flex flex-wrap gap-x-5 gap-y-2">
                    {capability.items.map((item) => {
                      const entry: PlanItem =
                        item.kind === "model"
                          ? { model: item.id }
                          : { workflow: item.id };
                      const key = itemKey(entry);
                      return (
                        <label
                          key={key}
                          className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11"
                        >
                          <Checkbox
                            checked={listed.has(key)}
                            onCheckedChange={(checked) => {
                              const items = (grant.items as PlanItem[]).filter(
                                (i) => itemKey(i) !== key,
                              );
                              set(capability.id, {
                                items: checked ? [...items, entry] : items,
                              });
                            }}
                          />
                          <span className="font-mono">{item.id}</span>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </fieldset>
  );
}

export function PlanEditor({
  plan,
  close,
  saved,
  restoreFocus,
}: {
  plan: AccessPlan | null;
  restoreFocus?: (event: Event) => void;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: queryKeys.accessPlanEditor,
    queryFn: api.accessPlans.editor,
  });
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent
        onCloseAutoFocus={restoreFocus}
        className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle>
            {plan ? "Edit access plan" : "New access plan"}
          </DialogTitle>
          <DialogDescription>
            Choose what this plan allows. An app can use only what both its
            access plan and its person&apos;s access plan allow.
          </DialogDescription>
        </DialogHeader>
        {isError ? (
          <ErrorState
            message="Available choices could not be loaded."
            onRetry={() => void refetch()}
          />
        ) : isPending || !data ? (
          <p role="status">Loading available choices…</p>
        ) : (
          <PlanForm
            plan={plan}
            contract={data}
            close={close}
            saved={saved}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function PlanForm({
  plan,
  contract,
  close,
  saved,
}: {
  plan: AccessPlan | null;
  contract: AccessPlanEditorContract;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [doc, setDoc] = useState<AccessPlanDocument>(
    structuredClone(plan?.plan ?? contract.new_plan),
  );
  const [raw, setRaw] = useState<string | null>(null);
  const [rawError, setRawError] = useState("");
  // Any form change, or Discard, returns the JSON view to the latest valid draft.
  const discardRaw = () => {
    setRaw(null);
    setRawError("");
  };
  const update = (changes: Partial<AccessPlanDocument>) => {
    setDoc((old) => ({ ...old, ...changes }));
    discardRaw();
  };
  const mutation = useMutation({
    mutationFn: () => {
      const body = { ...doc, name: doc.name.trim() };
      delete body.id;
      return plan
        ? api.accessPlans.update(plan.id, body)
        : api.accessPlans.create(body);
    },
    onSuccess: async () => {
      await saved();
      close();
    },
  });
  const direct = doc.direct_engine_access;
  const directOn = direct.engines === "all" || direct.engines.length > 0;
  return (
    <form
      className="space-y-6"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      <div className="space-y-2">
        <Label htmlFor="plan-name">Name</Label>
        <Input
          id="plan-name"
          required
          value={doc.name}
          onChange={(e) => update({ name: e.target.value })}
          disabled={mutation.isPending}
        />
      </div>
      <CanUse
        contract={contract}
        value={doc.can_use}
        change={(can_use) => update({ can_use })}
      />
      <fieldset className="space-y-3">
        <legend className="text-label">Paid providers</legend>
        {contract.paid_providers.length === 0 && (
          <p className="text-muted-foreground text-meta">
            No paid providers are set up.
          </p>
        )}
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          {contract.paid_providers.map((provider) => (
            <label
              key={provider.id}
              className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11"
            >
              <Checkbox
                checked={doc.paid_providers.includes(provider.id)}
                onCheckedChange={(checked) =>
                  update({
                    paid_providers: checked
                      ? [...doc.paid_providers, provider.id]
                      : doc.paid_providers.filter((id) => id !== provider.id),
                  })
                }
              />
              {provider.label}
            </label>
          ))}
        </div>
        <label className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11">
          <Checkbox
            checked={doc.provider_fallback}
            onCheckedChange={(checked) =>
              update({ provider_fallback: checked === true })
            }
          />
          Use a paid provider when the engine is unavailable
        </label>
        <p className="text-muted-foreground text-meta">
          Paid models can incur provider charges within the spending caps
          below and the provider and gateway caps.
        </p>
      </fieldset>
      <SpendingCaps
        caps={doc.caps}
        change={(caps) => update({ caps })}
        disabled={mutation.isPending}
      />
      <details className="border-t pt-4">
        <summary className="flex min-h-8 cursor-pointer items-center text-meta font-medium max-sm:min-h-11">
          Details
        </summary>
        <div className="space-y-6 pt-4">
          <fieldset className="space-y-3">
            <legend className="text-label">Direct engine access</legend>
            <label className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11">
              <Checkbox
                checked={directOn}
                onCheckedChange={(checked) =>
                  update({
                    direct_engine_access: checked
                      ? { engines: "all", operations: ["*"] }
                      : { engines: [], operations: [] },
                  })
                }
              />
              Apps may call engines through the engine&apos;s own API
            </label>
            {directOn && (
              <>
                <p className="text-muted-foreground text-meta">
                  With “*”, an app can do anything the engine&apos;s own API
                  allows, including loading or deleting models.
                </p>
                <div className="space-y-2">
                  <Label htmlFor="plan-engines">
                    Engines (one per line, or “all”)
                  </Label>
                  <Textarea
                    id="plan-engines"
                    rows={3}
                    className="font-mono text-xs"
                    value={
                      direct.engines === "all" ? "all" : direct.engines.join("\n")
                    }
                    onChange={(e) => {
                      const lines = e.target.value
                        .split("\n")
                        .map((line) => line.trim())
                        .filter(Boolean);
                      update({
                        direct_engine_access: {
                          ...direct,
                          engines:
                            lines.length === 1 && lines[0] === "all" ? "all" : lines,
                        },
                      });
                    }}
                  />
                  <p className="text-muted-foreground text-meta">
                    Engines on this gateway:{" "}
                    {contract.engines.map((engine) => engine.id).join(", ") || "none"}
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="plan-operations">
                    Operations (one per line, such as GET /props or *)
                  </Label>
                  <Textarea
                    id="plan-operations"
                    rows={3}
                    className="font-mono text-xs"
                    value={direct.operations.join("\n")}
                    onChange={(e) =>
                      update({
                        direct_engine_access: {
                          ...direct,
                          operations: e.target.value
                            .split("\n")
                            .map((line) => line.trim())
                            .filter(Boolean),
                        },
                      })
                    }
                  />
                </div>
              </>
            )}
          </fieldset>
          <label className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11">
            <Checkbox
              checked={doc.can_load_models}
              onCheckedChange={(checked) =>
                update({ can_load_models: checked === true })
              }
            />
            May load models that aren&apos;t running
          </label>
          <fieldset className="space-y-2">
            <legend className="text-label">Receipt detail</legend>
            {contract.receipt_detail.map((choice) => (
              <label
                key={choice.id}
                className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11"
              >
                <input
                  type="radio"
                  name="receipt-detail"
                  checked={doc.receipt_detail === choice.id}
                  onChange={() =>
                    update({
                      receipt_detail: choice.id as AccessPlanDocument["receipt_detail"],
                    })
                  }
                />
                {choice.label}
              </label>
            ))}
          </fieldset>
          <fieldset className="space-y-2">
            <legend className="text-label">Metadata in delivered files</legend>
            {contract.media_metadata.map((choice) => (
              <label
                key={choice.id}
                className="flex min-h-8 items-center gap-3 text-meta max-sm:min-h-11"
              >
                <input
                  type="radio"
                  name="media-metadata"
                  checked={doc.media_metadata === choice.id}
                  onChange={() =>
                    update({
                      media_metadata: choice.id as AccessPlanDocument["media_metadata"],
                    })
                  }
                />
                {choice.label}
              </label>
            ))}
          </fieldset>
          <details>
            <summary className="flex min-h-8 cursor-pointer items-center text-meta font-medium max-sm:min-h-11">
              Complete plan document
            </summary>
            <Label className="sr-only" htmlFor="policy-json">
              Plan JSON
            </Label>
            <Textarea
              id="policy-json"
              aria-invalid={!!rawError}
              aria-describedby={rawError ? "policy-json-error" : undefined}
              rows={12}
              className="font-mono text-xs"
              value={raw ?? JSON.stringify(doc, null, 2)}
              onChange={(e) => {
                setRaw(e.target.value);
                try {
                  const parsed = JSON.parse(e.target.value);
                  const shapeError = planShapeError(parsed);
                  if (shapeError) {
                    setRawError(shapeError);
                    return;
                  }
                  setDoc(parsed);
                  setRawError("");
                } catch {
                  setRawError("Enter a valid JSON object before saving.");
                }
              }}
            />
            {rawError && (
              <>
                <p
                  id="policy-json-error"
                  role="alert"
                  className="text-destructive text-meta"
                >
                  {rawError}
                </p>
                <Button type="button" variant="ghost" onClick={discardRaw}>
                  Discard invalid JSON changes
                </Button>
              </>
            )}
          </details>
        </div>
      </details>
      {mutation.isError && (
        <p role="alert" className="text-destructive text-meta">
          {errorMessage(mutation.error)}
        </p>
      )}
      <DialogFooter>
        {rawError && (
          <p
            id="plan-save-error"
            className="text-destructive self-center text-meta"
          >
            Save is blocked. Fix or discard invalid JSON changes in Details.
          </p>
        )}
        <Button
          type="button"
          variant="ghost"
          onClick={close}
          disabled={mutation.isPending}
        >
          Cancel
        </Button>
        <Button
          disabled={!doc.name.trim() || !!rawError || mutation.isPending}
          aria-describedby={rawError ? "plan-save-error" : undefined}
        >
          {mutation.isPending ? "Saving…" : "Save access plan"}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** Where a rule is edited, if this viewer may edit it. Plans open in place when the page says how. */
function editLink(rule: AccessRule | null, canEdit: boolean): { href: string; label: string; planId?: string } | null {
  if (!rule || !canEdit) return null;
  if (rule.kind === "plan")
    return rule.id
      ? {
          href: `/people/?plan=${encodeURIComponent(rule.id)}`,
          label: rule.name ? `Edit the ${rule.name} plan` : "Edit the plan",
          planId: rule.id,
        }
      : null;
  if (rule.kind === "category")
    return rule.id
      ? {
          href: `/categories/?category=${encodeURIComponent(rule.id)}`,
          label: rule.name ? `Edit the ${rule.name} category` : "Edit the category",
        }
      : { href: "/categories/", label: "Open Categories" };
  return { href: "/providers/", label: "Open Providers" };
}

const stateWord = (state: "allowed" | "blocked" | "unknown") =>
  state === "allowed" ? "Why allowed" : state === "unknown" ? "Can't tell" : "Blocked";

function groupCounts(counts: { total: number; usable: number; blocked: number; unknown: number }) {
  return [
    `${counts.usable} of ${counts.total} usable`,
    counts.blocked ? `${counts.blocked} blocked` : null,
    counts.unknown ? `${counts.unknown} can't tell` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Check access (design §3.1): everything this plan can and cannot use, without running anything. */
export function CheckAccess({
  plan,
  close,
  restoreFocus,
  canEdit = false,
  onEditPlan,
}: {
  plan: AccessPlan;
  restoreFocus?: (event: Event) => void;
  close: () => void;
  /** The owner may edit the plan and the rules behind a row; an administrator may only read. */
  canEdit?: boolean;
  /** Open the plan editor in place instead of following the link. */
  onEditPlan?: (planId: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [blockedOnly, setBlockedOnly] = useState(false);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const { data, isPending, isError, error, refetch, isFetching } = useQuery({
    queryKey: queryKeys.accessPreview(plan.id),
    queryFn: () => api.accessPlans.preview(plan.id),
    staleTime: 0,
  });
  const entries = data?.entries ?? [];
  const needle = filter.trim().toLowerCase();
  const shown = entries.filter(
    (entry) =>
      (!blockedOnly || entry.state !== "allowed") &&
      (!needle ||
        `${entry.name} ${entry.id} ${entry.group_label} ${entry.points_to.map((target) => target.name).join(" ")}`
          .toLowerCase()
          .includes(needle)),
  );
  const groups = shown.reduce<{ key: string; label: string; entries: AccessPreviewEntry[] }[]>(
    (found, entry) => {
      const group = found.find((item) => item.key === entry.group);
      if (group) group.entries.push(entry);
      else found.push({ key: entry.group, label: entry.group_label, entries: [entry] });
      return found;
    },
    [],
  );
  const toggle = (key: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent onCloseAutoFocus={restoreFocus} className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Check access · {plan.name}</DialogTitle>
          <DialogDescription>
            Previewing {plan.name} for both the person and the app. This does
            not assign the plan or change Try. No model request is made and
            no provider charges are incurred.
          </DialogDescription>
        </DialogHeader>
        {isPending ? (
          <p role="status" className="text-muted-foreground text-meta">
            Checking what this plan can use…
          </p>
        ) : isError ? (
          <ErrorState
            message="We couldn't check this plan. Try again."
            error={error}
            onRetry={() => refetch()}
            retrying={isFetching}
          />
        ) : !data || entries.length === 0 ? (
          <p className="text-muted-foreground text-meta">
            The gateway has no models or workflows to check yet.
          </p>
        ) : (
          <div className="space-y-4">
            <div className="space-y-1">
              <p className="text-body font-medium">
                Can use {data.summary.usable} of {data.summary.total}
              </p>
              {(data.summary.blocked > 0 || data.summary.unknown > 0) && (
                <p className="text-muted-foreground text-meta">
                  {data.summary.blocked > 0 && <span>{data.summary.blocked} blocked</span>}
                  {data.summary.blocked > 0 && data.summary.unknown > 0 && " · "}
                  {data.summary.unknown > 0 && <span>{data.summary.unknown} can&apos;t tell</span>}
                </p>
              )}
              <p className="text-muted-foreground text-meta">
                This list shows what the plan allows. It does not check that an
                engine is running, that a spending cap has room, or that a
                stopped model could be started.
              </p>
            </div>
            {entries.length > 6 && (
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  type="search"
                  aria-label="Filter models and workflows"
                  placeholder="Filter by name"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  className="min-w-0 flex-1 sm:max-w-72"
                />
                <FilterToggle pressed={blockedOnly} onClick={() => setBlockedOnly((on) => !on)}>
                  Show blocked only
                </FilterToggle>
              </div>
            )}
            <div
              role="region"
              aria-label="What this plan can use"
              tabIndex={0}
              className="max-h-[50vh] space-y-4 overflow-y-auto pr-1"
            >
              {groups.length === 0 ? (
                <p className="text-muted-foreground text-meta">Nothing matches.</p>
              ) : (
                groups.map((group) => {
                  const counts = data.groups.find((item) => item.group === group.key);
                  return (
                    <section key={group.key} aria-labelledby={`access-group-${group.key}`}>
                      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                        <h3 id={`access-group-${group.key}`} className="text-meta font-medium">
                          {group.label}
                        </h3>
                        {counts && (
                          <span className="text-muted-foreground text-meta">{groupCounts(counts)}</span>
                        )}
                      </div>
                      <ul className="divide-y">
                        {group.entries.map((entry) => {
                          const key = `${entry.alias}:${entry.id}`;
                          return (
                            <AccessEntryRow
                              key={key}
                              entry={entry}
                              expanded={open.has(key)}
                              toggle={() => toggle(key)}
                              canEdit={canEdit}
                              onEditPlan={onEditPlan}
                            />
                          );
                        })}
                      </ul>
                    </section>
                  );
                })
              )}
            </div>
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" type="button" onClick={close}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const linkClass =
  "text-meta inline-flex min-h-8 items-center underline underline-offset-4 max-sm:min-h-11";

function RuleLink({
  rule,
  canEdit,
  onEditPlan,
}: {
  rule: AccessRule | null;
  canEdit: boolean;
  onEditPlan?: (planId: string) => void;
}) {
  const link = editLink(rule, canEdit);
  if (!link) return null;
  return (
    <Link
      prefetch={false}
      href={link.href}
      className={linkClass}
      onClick={(event) => {
        if (link.planId && onEditPlan) {
          event.preventDefault();
          onEditPlan(link.planId);
        }
      }}
    >
      {link.label}
    </Link>
  );
}

/** What one kind of work (or the whole row) decided: its state, sentence and rule. */
interface Verdict {
  label: string | null;
  state: "allowed" | "blocked" | "unknown";
  /** The sentence; a decision that recorded none reads as the plain default. */
  text: string | null | undefined;
  rule: AccessRule | null;
}

const sentenceOf = (item: Verdict) =>
  item.text ?? (item.state === "allowed" ? "This plan allows it." : "This plan doesn't include it.");

function verdicts(entry: AccessPreviewEntry): Verdict[] {
  if (entry.kinds.length > 0)
    return entry.kinds.map((kind) => ({
      label: kind.label,
      state: kind.state,
      text: kind.allowed ? kind.rule?.plain_reason : kind.reason,
      rule: kind.rule,
    }));
  const state = entry.state === "allowed" ? "allowed" : entry.state === "unknown" ? "unknown" : "blocked";
  return [
    {
      label: null,
      state,
      text: state === "allowed" ? entry.rule?.plain_reason : entry.reason,
      rule: entry.rule,
    },
  ];
}

function AccessEntryRow({
  entry,
  expanded,
  toggle,
  canEdit,
  onEditPlan,
}: {
  entry: AccessPreviewEntry;
  expanded: boolean;
  toggle: () => void;
  canEdit: boolean;
  onEditPlan?: (planId: string) => void;
}) {
  const detailId = useId();
  const all = verdicts(entry);
  const shut = all.filter((item) => item.state !== "allowed");
  return (
    <li className="py-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className="text-body font-medium break-words">{entry.name}</p>
          {entry.id !== entry.name && (
            <p className="text-muted-foreground text-meta break-all">{entry.id}</p>
          )}
          {entry.points_to.length > 0 && (
            <p className="text-meta">
              Points to{" "}
              {entry.points_to.map((target, index) => (
                <span key={target.id}>
                  {index > 0 && ", "}
                  <Link
                    prefetch={false}
                    href={`/models/?model=${encodeURIComponent(target.id)}`}
                    className="underline underline-offset-4"
                  >
                    {target.name}
                  </Link>
                </span>
              ))}
            </p>
          )}
          {entry.kinds.length > 0 && (
            <p className="text-meta">
              {entry.kinds.map((kind, index) => (
                <span key={kind.kind}>
                  {index > 0 && " · "}
                  {kind.label}{" "}
                  <span aria-hidden="true">
                    {kind.state === "allowed" ? "✓" : kind.state === "unknown" ? "?" : "✗"}
                  </span>
                  <span className="sr-only">
                    {kind.state === "allowed" ? "allowed" : kind.state === "unknown" ? "can't tell" : "blocked"}
                  </span>
                </span>
              ))}
            </p>
          )}
          {!expanded &&
            shut.map((item) => (
              <div key={item.label ?? "row"} className="space-y-0.5">
                <p className="text-muted-foreground text-meta break-words">
                  {item.label ? `${item.label}: ${sentenceOf(item)}` : `${stateWord(item.state)}: ${sentenceOf(item)}`}
                </p>
                <RuleLink rule={item.rule} canEdit={canEdit} onEditPlan={onEditPlan} />
              </div>
            ))}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          {entry.state === "allowed" ? (
            <StateBadge state="ready" word="Allowed" />
          ) : entry.state === "partial" ? (
            <StateBadge state="degraded" word="Partly allowed" />
          ) : entry.state === "unknown" ? (
            <StateBadge state="unknown" word="Can't tell" />
          ) : (
            <StateBadge state="blocked" word="Blocked" />
          )}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label={`Why ${entry.name}`}
            aria-expanded={expanded}
            aria-controls={detailId}
            onClick={toggle}
          >
            Why
          </Button>
        </div>
      </div>
      {expanded && (
        <div id={detailId} className="bg-muted/40 mt-2 space-y-3 rounded-md p-3">
          {all.map((item) => (
            <div key={item.label ?? "row"} className="space-y-1">
              <p className="text-meta break-words">
                {item.label ? `${item.label}: ` : ""}
                {stateWord(item.state)}: {sentenceOf(item)}
              </p>
              {item.rule && (
                <p className="text-muted-foreground font-mono text-xs break-all">{item.rule.code}</p>
              )}
              <RuleLink rule={item.rule} canEdit={canEdit} onEditPlan={onEditPlan} />
            </div>
          ))}
        </div>
      )}
    </li>
  );
}

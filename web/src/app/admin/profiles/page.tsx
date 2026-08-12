"use client";

/** Admin → Profiles — surface spec (docs/design/WEB_UI_STEERING.md §Profiles).
 *
 * List-first IA: the profiles table opens the section; creation/editing lives
 * behind Dialogs only. The presentation law applies throughout:
 *   - RelativeTime for every timestamp
 *   - EntityRef for every id
 *   - ConfirmDialog (typed-name) for delete
 *   - OverflowMenu for row actions
 *   - Designed loading / empty / error states
 *
 * Access summary ("4 models · chat, images") is derived client-side from the
 * profile's `protocols` / `workflows` / `profile.allow.models` fields.
 *
 * "Apps using it" is a client-side count of apps whose `default_profile_id`
 * matches the profile id (contract note in web-api-contract.md §Surface 4).
 *
 * Create/Edit dialog is progressively disclosed: name + description first,
 * then protocols/workflows in a collapsible "Advanced" section.
 *
 * "Test access" disclosure calls POST /admin/api/policy/simulate and renders
 * allowed/denied plus the verbatim reason string. It uses an inline profile
 * id ref so no unsaved changes are needed before testing.
 *
 * All mutations use api.profiles.* and api.policy.simulate from lib/api.ts. */

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type Profile,
  type PolicyEditorChoice,
  type PolicyEditorContract,
  type ProfilePolicyDocument,
  type SimulateResult,
} from "@/lib/api";
import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  EmptyOperationalState,
  Field,
  IncidentBanner,
  Input,
  NoticeBanner,
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
  RelativeTime,
  SectionActionHeader,
  Select,
  Skeleton,
  SummaryStrip,
  toast,
} from "@/components/ui";

// ---------------------------------------------------------------------------
// Helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

function FormErrorSummary({ message }: { message: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  useEffect(() => ref.current?.focus(), [message]);
  return (
    <p
      ref={ref}
      role="alert"
      tabIndex={-1}
      className="text-vf-small text-vf-bad"
    >
      {message}
    </p>
  );
}

function normalizedRouteTarget(value: string | undefined): string {
  return (value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-");
}

function profileMatchesRouteTarget(profile: Profile, target: string): boolean {
  const wanted = normalizedRouteTarget(target);
  return [profile.id, profile.name].some(
    (candidate) => normalizedRouteTarget(candidate) === wanted,
  );
}

function RouteProfileTargetSync({
  onChange,
}: {
  onChange: (target: string | null) => void;
}) {
  const searchParams = useSearchParams();
  useEffect(() => {
    onChange(searchParams.get("profile"));
  }, [onChange, searchParams]);
  return null;
}

/** Derive the human access summary from a profile.
 *
 * Produces e.g. "4 models · chat, images" or "all models · chat"
 * or "all access" when unrestricted. */
function accessSummary(profile: Profile): string {
  const intents: string[] = [];

  // The allow block lives in the policy document
  const allow = profile.profile?.allow as
    | { intents?: string[] }
    | undefined;

  if (allow?.intents && allow.intents.length > 0) {
    for (const intent of allow.intents) {
      if (!intents.includes(intent)) intents.push(intent);
    }
  } else if (profile.workflows && profile.workflows.length > 0) {
    // workflows can substitute for explicit intents
    intents.push(...profile.workflows.map((w) => w.replace(/-/g, " ")));
  }

  const models = profile.profile.models;
  let modelPart: string;
  if (!models || models.length === 0) {
    modelPart = "all models";
  } else {
    modelPart = `${models.length} ${models.length === 1 ? "model" : "models"}`;
  }

  if (intents.length === 0 && modelPart === "all models") {
    // No restrictions: check protocols
    if (profile.protocols.length === 0) return "all access";
    return `${profile.protocols.join(", ")}`;
  }

  const intentPart = intents.slice(0, 3).join(", ");
  const intentSuffix = intents.length > 3 ? ` +${intents.length - 3}` : "";

  if (intents.length === 0) return modelPart;
  return `${modelPart} · ${intentPart}${intentSuffix}`;
}

/** Structured access facets for an operational row (badges, not just a string). */
interface ProfileFacets {
  /** "all models" or "N models". */
  modelScope: string;
  /** Whether the model scope is unrestricted (used for tone). */
  unrestrictedModels: boolean;
  /** Human capability labels (chat, images, …) — capped for display. */
  capabilities: string[];
  /** Protocol front doors (openai, anthropic, …). */
  protocols: string[];
  /** True when nothing is restricted at all. */
  fullAccess: boolean;
}

function profileFacets(profile: Profile): ProfileFacets {
  const allow = profile.profile?.allow as
    | { intents?: string[] }
    | undefined;

  const capabilities: string[] = [];
  if (allow?.intents && allow.intents.length > 0) {
    for (const intent of allow.intents) {
      if (!capabilities.includes(intent)) capabilities.push(intent);
    }
  } else if (profile.workflows && profile.workflows.length > 0) {
    capabilities.push(...profile.workflows.map((w) => w.replace(/-/g, " ")));
  }

  const models = profile.profile.models;
  const unrestrictedModels = !models || models.length === 0;
  const modelScope = unrestrictedModels
    ? "all models"
    : `${models!.length} ${models!.length === 1 ? "model" : "models"}`;

  const fullAccess =
    unrestrictedModels &&
    capabilities.length === 0 &&
    profile.protocols.length === 0;

  return {
    modelScope,
    unrestrictedModels,
    capabilities,
    protocols: profile.protocols,
    fullAccess,
  };
}

// ---------------------------------------------------------------------------
// The surface

export default function AdminProfilesPage() {
  const queryClient = useQueryClient();
  const [routeProfileTarget, setRouteProfileTarget] = useState<string | null>(
    null,
  );
  const consumedProfileTargetRef = useRef<string | null>(null);

  const {
    data: profilesData,
    isPending: profilesPending,
    isError: profilesIsError,
    error: profilesError,
    refetch: refetchProfiles,
  } = useQuery({ queryKey: queryKeys.profiles, queryFn: api.profiles.list });
  const {
    data: editor,
    isError: editorIsError,
    isPending: editorPending,
  } = useQuery({ queryKey: queryKeys.policyEditor, queryFn: api.policyEditor.get });

  const {
    data: appsData,
    isPending: appsPending,
    isError: appsIsError,
  } = useQuery({
    queryKey: queryKeys.apps,
    queryFn: api.apps.list,
  });
  const {
    data: mappingsData,
    isPending: mappingsPending,
    isError: mappingsIsError,
  } = useQuery({
    queryKey: queryKeys.runtimeProfileMappings,
    queryFn: api.runtimeProfileMappings.list,
  });

  // Client-side count: apps per profile (contract §Surface 4)
  const appCountByProfile = useMemo(() => {
    const counts = new Map<string, number>();
    for (const app of appsData ?? []) {
      if (!app.default_profile_id) continue;
      counts.set(
        app.default_profile_id,
        (counts.get(app.default_profile_id) ?? 0) + 1,
      );
    }
    return counts;
  }, [appsData]);

  // Dialog state
  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Profile | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Profile | null>(null);
  const [testTarget, setTestTarget] = useState<Profile | null>(null);
  // Header-level "Test access" — no pre-selected profile; user picks inside the dialog.
  const [testGlobalOpen, setTestGlobalOpen] = useState(false);

  // Delete mutation
  const deleteProfile = useMutation({
    mutationFn: (profile: Profile) => api.profiles.delete(profile.id),
    onSuccess: async (_, profile) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.profiles });
      toast.success(`${profile.name} deleted`);
      setDeleteTarget(null);
    },
    onError: async (error) => {
      if (
        error instanceof ApiError &&
        (error.status === 409 || error.status === 412)
      ) {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: queryKeys.profiles }),
          queryClient.invalidateQueries({ queryKey: queryKeys.apps }),
          queryClient.invalidateQueries({
            queryKey: queryKeys.runtimeProfileMappings,
          }),
        ]);
      }
      // 409 profile_in_use: the server blocks the delete — we surface the
      // reason in the dialog and close on success only.
      toast.error("Couldn't delete profile", {
        description:
          error instanceof ApiError && error.status === 412
            ? `${errorMessage(error)} The latest dependencies were refreshed; review them and try again.`
            : errorMessage(error),
      });
    },
  });

  const profiles = useMemo(
    () => [...(profilesData ?? [])].sort((a, b) => b.created_at - a.created_at),
    [profilesData],
  );
  const appsUsingProfiles = useMemo(
    () =>
      Array.from(appCountByProfile.values()).reduce(
        (total, count) => total + count,
        0,
      ),
    [appCountByProfile],
  );
  const unrestrictedProfiles = profiles.filter(
    (profile) => accessSummary(profile) === "all access",
  ).length;
  const protocolCount = new Set(
    profiles.flatMap((profile) => profile.protocols),
  ).size;

  const loading = profilesPending;
  const failed = profilesIsError && !profilesData;

  const routeSelectedProfile = useMemo(() => {
    if (!routeProfileTarget) return null;
    return (
      profiles.find((profile) =>
        profileMatchesRouteTarget(profile, routeProfileTarget),
      ) ?? null
    );
  }, [profiles, routeProfileTarget]);

  useEffect(() => {
    if (!routeProfileTarget) {
      consumedProfileTargetRef.current = null;
      return;
    }
    if (consumedProfileTargetRef.current === routeProfileTarget) return;
    if (!routeSelectedProfile) return;
    setEditTarget(routeSelectedProfile);
    consumedProfileTargetRef.current = routeProfileTarget;
  }, [routeProfileTarget, routeSelectedProfile]);

  const missingRouteProfile =
    routeProfileTarget !== null &&
    routeSelectedProfile === null &&
    !profilesPending &&
    profiles.length > 0;

  return (
    <div className="flex w-full flex-col gap-8">
      <Suspense fallback={null}>
        <RouteProfileTargetSync onChange={setRouteProfileTarget} />
      </Suspense>
      <section className="flex flex-col gap-4">
        <SectionActionHeader
          kicker="access"
          title="Profiles"
          count={profiles.length > 0 ? profiles.length : undefined}
          description="A profile defines what a caller is allowed to reach — which models, protocols, and capabilities."
          action={
            <div className="flex gap-2">
              <Button
                variant="secondary"
                onClick={() => setTestGlobalOpen(true)}
                disabled={profiles.length === 0}
              >
                Test access
              </Button>
              <Button variant="primary" onClick={() => setCreateOpen(true)}>
                Create profile
              </Button>
            </div>
          }
        />

        <SummaryStrip
          items={[
            {
              label: "profiles",
              value: String(profiles.length),
              detail: "access policies",
              tone: "info",
              badge: "live",
            },
            {
              label: "assigned apps",
              value: appsData
                ? String(appsUsingProfiles)
                : appsIsError
                  ? "Unavailable"
                  : "…",
              detail: appsData ? "explicit profile" : "app assignment data",
              tone: "accent",
              badge: appsData ? "live" : appsIsError ? "degraded" : "loading",
            },
            {
              label: "protocols",
              value: String(protocolCount),
              detail: "represented",
              tone: "neutral",
              badge: "scope",
            },
            {
              label: "unrestricted",
              value: String(unrestrictedProfiles),
              detail: "broad access",
              tone: unrestrictedProfiles > 0 ? "warn" : "ok",
              badge: unrestrictedProfiles > 0 ? "review" : "none",
            },
          ]}
        />
        {missingRouteProfile && (
          <IncidentBanner
            tone="warn"
            title="No profile matches this link"
            detail={`The profile target "${routeProfileTarget}" is not in the current profile list.`}
          />
        )}

        {failed ? (
          <ErrorBanner onRetry={() => void refetchProfiles()}>
            Couldn&apos;t load profiles. {errorMessage(profilesError)}
          </ErrorBanner>
        ) : loading ? (
          <div className="vf-feed-panel flex flex-col gap-0" aria-busy="true">
            <Skeleton className="h-16 w-full rounded-none border-b border-[var(--vf-border)]" />
            <Skeleton className="h-16 w-full rounded-none border-b border-[var(--vf-border)]" />
            <Skeleton className="h-16 w-full rounded-none" />
          </div>
        ) : profiles.length === 0 ? (
          <EmptyOperationalState
            title="No profiles yet"
            detail="Create one to control what callers can reach."
            action={
              <Button onClick={() => setCreateOpen(true)}>
                Create profile
              </Button>
            }
          />
        ) : (
          <div className="vf-feed-panel overflow-hidden">
            {profiles.map((profile) => (
              <ProfileRow
                key={profile.id}
                profile={profile}
                appCount={appCountByProfile.get(profile.id) ?? 0}
                appsPending={appsPending}
                appsUnavailable={appsIsError && !appsData}
                onEdit={() => setEditTarget(profile)}
                onTest={() => setTestTarget(profile)}
                onDelete={() => setDeleteTarget(profile)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Create dialog */}
      <ProfileDialog
        mode="create"
        open={createOpen}
        onOpenChange={setCreateOpen}
        editor={editor}
        editorStale={editorIsError && Boolean(editor)}
        editorUnavailable={!editor && !editorPending}
      />

      {/* Edit dialog */}
      <ProfileDialog
        mode="edit"
        profile={editTarget ?? undefined}
        open={editTarget !== null}
        onOpenChange={(open) => {
          if (!open) setEditTarget(null);
        }}
        editor={editor}
        editorStale={editorIsError && Boolean(editor)}
        editorUnavailable={!editor && !editorPending}
      />

      {/* Test access dialog — opened from row overflow (fixed profile) */}
      {testTarget && (
        <TestAccessDialog
          profile={testTarget}
          open={testTarget !== null}
          onOpenChange={(open) => {
            if (!open) setTestTarget(null);
          }}
        />
      )}

      {/* Test access dialog — opened from header (user picks profile inside) */}
      <TestAccessDialog
        profiles={profiles}
        open={testGlobalOpen}
        onOpenChange={setTestGlobalOpen}
      />

      {/* Delete confirm (typed-name, blocks on in-use) */}
      <DeleteProfileDialog
        profile={deleteTarget}
        appsUsingIt={
          deleteTarget ? (appCountByProfile.get(deleteTarget.id) ?? 0) : 0
        }
        mappingsUsingIt={
          deleteTarget
            ? (mappingsData ?? []).filter(
                (mapping) =>
                  mapping.profile_id === deleteTarget.id &&
                  mapping.disabled_at === null,
              ).length
            : 0
        }
        dependenciesLoading={appsPending || mappingsPending}
        dependenciesUnavailable={(!appsData && !appsPending) || mappingsIsError}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        onConfirm={() =>
          deleteTarget
            ? deleteProfile.mutateAsync(deleteTarget).then(() => undefined)
            : Promise.resolve()
        }
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ProfileRow — operational list row (replaces a flat table cell)
//
// Line 1: name, apps-using count, overflow menu.
// Line 2: access facets as badges (model scope, capabilities, protocols) —
//         derived from existing profile fields only; no invented policy data.

function ProfileRow({
  profile,
  appCount,
  appsPending,
  appsUnavailable,
  onEdit,
  onTest,
  onDelete,
}: {
  profile: Profile;
  appCount: number;
  appsPending: boolean;
  appsUnavailable: boolean;
  onEdit: () => void;
  onTest: () => void;
  onDelete: () => void;
}) {
  const facets = profileFacets(profile);
  const caps = facets.capabilities.slice(0, 4);
  const capOverflow = facets.capabilities.length - caps.length;

  return (
    <div className="vf-service-row">
      {/* Top line: identity + apps-using + actions */}
      <div className="vf-service-row-main">
        <button
          type="button"
          className="min-w-0 flex-1 cursor-pointer text-left"
          onClick={onEdit}
          aria-label={`${profile.name} — edit access`}
        >
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-vf-body text-vf-text shrink-0 font-semibold">
              {profile.name}
            </span>
          </span>
        </button>
        <div
          className="vf-service-row-actions shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          <span className="text-vf-micro text-vf-muted hidden font-mono tabular-nums sm:inline">
            {appsPending ? (
              <Skeleton className="h-4 w-10" />
            ) : appsUnavailable ? (
              <span className="text-vf-subtle">unavailable</span>
            ) : appCount === 0 ? (
              <span className="text-vf-subtle">unused</span>
            ) : (
              `${appCount} ${appCount === 1 ? "app" : "apps"}`
            )}
          </span>
          <OverflowMenu label={`Actions for ${profile.name}`}>
            <OverflowMenuItem onSelect={onEdit}>Edit…</OverflowMenuItem>
            <OverflowMenuItem onSelect={onTest}>Test access…</OverflowMenuItem>
            <OverflowMenuSeparator />
            <OverflowMenuItem danger onSelect={onDelete}>
              Delete…
            </OverflowMenuItem>
          </OverflowMenu>
        </div>
      </div>

      {/* Second line: access facets as badges */}
      <div className="flex min-h-0 flex-wrap items-center gap-1.5">
        {facets.fullAccess ? (
          <Badge sm tone="warn">
            all access
          </Badge>
        ) : (
          <>
            <Badge sm tone={facets.unrestrictedModels ? "neutral" : "info"}>
              {facets.modelScope}
            </Badge>
            {caps.map((cap) => (
              <Badge key={cap} sm tone="neutral">
                {cap}
              </Badge>
            ))}
            {capOverflow > 0 && (
              <span className="text-vf-micro text-vf-muted font-mono">
                +{capOverflow}
              </span>
            )}
            {facets.protocols.map((p) => (
              <span
                key={p}
                className="text-vf-micro text-vf-subtle font-mono"
                title={`protocol: ${p}`}
              >
                {p}
              </span>
            ))}
          </>
        )}
        <span className="text-vf-micro text-vf-muted ml-auto font-mono">
          <RelativeTime ts={profile.created_at} />
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Error banner

function ErrorBanner({
  children,
  onRetry,
}: {
  children: ReactNode;
  onRetry: () => void;
}) {
  return (
    <NoticeBanner
      tone="bad"
      action={
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      }
    >
      {children}
    </NoticeBanner>
  );
}

// ---------------------------------------------------------------------------
// Create / Edit dialog (progressively disclosed)

interface ProfileFormState {
  name: string;
  description: string;
  protocols: string[] | undefined;
  workflows: string[] | undefined;
  models: string[] | undefined;
  intents: string[] | undefined;
  tags: string[] | undefined;
  statusVisibility: string;
}

function defaultFormState(editor: PolicyEditorContract | undefined, profile?: Profile): ProfileFormState {
  const document = profile?.profile ?? editor?.profile.create.profile;
  return {
    name: profile?.name ?? "",
    description: profile?.description ?? "",
    protocols: document?.protocols,
    workflows: document?.workflows,
    models: document?.models,
    intents: document?.allow?.intents,
    tags: document?.allow?.tags,
    statusVisibility: document?.status_visibility?.level ?? "",
  };
}

function profileFormSummary(form: ProfileFormState): string {
  const protocols = form.protocols === undefined ? "all protocols" : form.protocols.length ? form.protocols.join(", ") : "no protocols";
  const modelCount = form.models?.length ?? 0;
  const models =
    form.models === undefined
      ? "all models"
      : `${modelCount} ${modelCount === 1 ? "model" : "models"}`;
  const capabilities =
    form.intents === undefined
      ? "all capabilities"
      : `${form.intents.length} ${form.intents.length === 1 ? "capability" : "capabilities"}`;
  return `${protocols} · ${models} · ${capabilities}`;
}

function ProfileDialog({
  mode,
  profile,
  open,
  onOpenChange,
  editor,
  editorStale,
  editorUnavailable,
}: {
  mode: "create" | "edit";
  profile?: Profile;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  editor: PolicyEditorContract | undefined;
  editorStale: boolean;
  editorUnavailable: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <ProfileDialogContent
          mode={mode}
          profile={profile}
          onOpenChange={onOpenChange}
          editor={editor}
          editorStale={editorStale}
          editorUnavailable={editorUnavailable}
        />
      )}
    </Dialog>
  );
}

function ProfileDialogContent({
  mode,
  profile,
  onOpenChange,
  editor,
  editorStale,
  editorUnavailable,
}: {
  mode: "create" | "edit";
  profile?: Profile;
  onOpenChange: (open: boolean) => void;
  editor: PolicyEditorContract | undefined;
  editorStale: boolean;
  editorUnavailable: boolean;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ProfileFormState>(() =>
    defaultFormState(editor, profile),
  );
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<{ name?: string }>({});
  const [formError, setFormError] = useState<string | null>(null);

  function setField<K extends keyof ProfileFormState>(
    key: K,
    value: ProfileFormState[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function buildProfileDoc(): ProfilePolicyDocument {
    const source = profile?.profile ?? editor?.profile.create.profile ?? {};
    const document = structuredClone(source);
    const allow = { ...(document.allow ?? {}) };
    for (const [field, value] of Object.entries({ protocols: form.protocols, workflows: form.workflows, models: form.models }) as Array<["protocols" | "workflows" | "models", string[] | undefined]>) {
      if (value === undefined) delete document[field];
      else document[field] = value;
    }
    for (const [field, value] of Object.entries({ intents: form.intents, tags: form.tags }) as Array<["intents" | "tags", string[] | undefined]>) {
      if (value === undefined) delete allow[field];
      else allow[field] = value;
    }
    if (Object.keys(allow).length) document.allow = allow;
    else delete document.allow;
    if (form.statusVisibility) document.status_visibility = { ...document.status_visibility, level: form.statusVisibility };
    return document;
  }

  const save = useMutation({
    mutationFn: () => {
      const profileDoc = buildProfileDoc();
      if (mode === "create") {
        return api.profiles.create({
          name: form.name.trim(),
          description: form.description.trim() || undefined,
          profile: profileDoc,
        });
      } else {
        return api.profiles.update(profile!.id, {
          name: form.name.trim(),
          description: form.description.trim() || undefined,
          profile: profileDoc,
        });
      }
    },
    onSuccess: async (savedProfile) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.profiles });
      toast.success(
        mode === "create"
          ? `${savedProfile.name} created`
          : `${savedProfile.name} updated`,
      );
      onOpenChange(false);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const errors: { name?: string } = {};
    if (!form.name.trim()) errors.name = "Give the profile a name.";
    setFieldErrors(errors);
    setFormError(null);
    if (Object.keys(errors).length > 0) return;
    save.mutate();
  }

  const title = mode === "create" ? "Create profile" : "Edit profile";
  const actionLabel = mode === "create" ? "Create profile" : "Save changes";

  return (
    <DialogContent>
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {mode === "create"
              ? "Define what callers using this profile are allowed to reach."
              : `Update access rules for ${profile?.name}.`}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field label="Name" required error={fieldErrors.name}>
            <Input
              value={form.name}
              onChange={(e) => setField("name", e.target.value)}
              placeholder="e.g. Trusted local"
              autoFocus
              disabled={save.isPending}
            />
          </Field>
          <Field
            label="Description"
            help="Optional. Shown to admins for context."
          >
            <Input
              value={form.description}
              onChange={(e) => setField("description", e.target.value)}
              placeholder="e.g. Full access for on-prem devices"
              disabled={save.isPending}
            />
          </Field>

          <div className="rounded-vf-md border-vf-border bg-vf-surface-2/50 flex flex-col gap-1 border p-3">
            <span className="text-vf-micro text-vf-subtle font-mono uppercase">
              Current access
            </span>
            <span className="text-vf-small text-vf-text">
              {profileFormSummary(form)}
            </span>
          </div>

          {/* Progressive disclosure: customization rules */}
          <div className="border-vf-border rounded-vf-md border">
            <button
              type="button"
              className="text-vf-small text-vf-muted hover:text-vf-text rounded-vf-md flex w-full items-center justify-between px-4 py-3 transition-colors"
              onClick={() => setAdvancedOpen((v) => !v)}
              aria-expanded={advancedOpen}
            >
              <span className="font-medium">Customize access</span>
              <span className="text-vf-micro font-mono uppercase">
                {advancedOpen ? "hide" : "edit"}
              </span>
            </button>

            {advancedOpen && (
              <div className="border-vf-border flex flex-col gap-4 border-t px-4 py-4">
                {editorUnavailable ? <NoticeBanner tone="warn">Unavailable — policy choices could not be read. Saved values remain visible, but discovery controls are disabled.</NoticeBanner> : editorStale ? <NoticeBanner tone="warn">Stale — showing prior choices. You may remove retained values, but cannot add a stale choice.</NoticeBanner> : null}
                <PolicySetField label="API protocols" choices={editor?.profile.choices.protocols ?? []} value={form.protocols} onChange={(value) => setField("protocols", value)} disabled={save.isPending || editorUnavailable} stale={editorStale} />
                <PolicySetField label="Capabilities" choices={editor?.profile.choices.intents ?? []} value={form.intents} onChange={(value) => setField("intents", value)} disabled={save.isPending || editorUnavailable} stale={editorStale} />
                <PolicySetField label="Workflows" choices={editor?.profile.choices.workflows ?? []} value={form.workflows} onChange={(value) => setField("workflows", value)} disabled={save.isPending || editorUnavailable} stale={editorStale} />
                <PolicySetField label="Allowed models" choices={editor?.profile.choices.models ?? []} value={form.models} onChange={(value) => setField("models", value)} disabled={save.isPending || editorUnavailable} stale={editorStale} />
                <PolicySetField label="Tags" choices={editor?.profile.choices.tags ?? []} value={form.tags} onChange={(value) => setField("tags", value)} disabled={save.isPending || editorUnavailable} stale={editorStale} />
                <Field label="Status visibility" help="How much queue/status info callers can see.">
                  <Select value={form.statusVisibility} onChange={(e) => setField("statusVisibility", e.target.value)} disabled={save.isPending || editorUnavailable}>
                    <option value="">Select status visibility…</option>
                    {(editor?.profile.choices.status_visibility ?? []).map((choice) => <option key={choice.id} value={choice.id}>{choice.label}</option>)}
                  </Select>
                </Field>
              </div>
            )}
          </div>

          {formError && <FormErrorSummary message={formError} />}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={save.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            {actionLabel}
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

function PolicySetField({
  label,
  choices,
  value,
  onChange,
  disabled,
  stale,
}: {
  label: string;
  choices: PolicyEditorChoice[];
  value: string[] | undefined;
  onChange: (value: string[] | undefined) => void;
  disabled: boolean;
  stale: boolean;
}) {
  const selected = value ?? [];
  const advertised = new Map(choices.map((choice) => [choice.id, choice]));
  const visible = [...choices, ...selected.filter((id) => !advertised.has(id)).map((id) => ({ id, label: `${id} — Not currently advertised`, description: "Retaining this value retains the grant if it becomes active again.", available: false }))];
  const changeMode = (mode: "unrestricted" | "deny") => onChange(mode === "unrestricted" ? undefined : []);
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  return (
    <Field label={label} help="No restriction omits this field. Deny all stores an explicit empty set.">
      <div className="flex flex-col gap-2">
        <div className="flex gap-3 text-vf-small">
          <label><input type="radio" checked={value === undefined} onChange={() => changeMode("unrestricted")} disabled={disabled} /> No restriction</label>
          <label><input type="radio" checked={value !== undefined} onChange={() => changeMode("deny")} disabled={disabled} /> Restrict</label>
        </div>
        {value !== undefined && <div className="flex flex-wrap gap-3">
          {visible.map((choice) => {
            const checked = selected.includes(choice.id);
            const mayAdd = !stale && choice.available !== false;
            return <Checkbox key={choice.id} checked={checked} onCheckedChange={() => toggle(choice.id)} disabled={disabled || (!checked && !mayAdd)}>{choice.label}</Checkbox>;
          })}
        </div>}
      </div>
    </Field>
  );
}

// ---------------------------------------------------------------------------
// Delete profile dialog (explains + blocks when apps still reference it)

function DeleteProfileDialog({
  profile,
  appsUsingIt,
  mappingsUsingIt,
  dependenciesLoading,
  dependenciesUnavailable,
  onOpenChange,
  onConfirm,
}: {
  profile: Profile | null;
  appsUsingIt: number;
  mappingsUsingIt: number;
  dependenciesLoading: boolean;
  dependenciesUnavailable: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const inUse = appsUsingIt > 0 || mappingsUsingIt > 0;

  if (inUse) {
    // Block the delete: show an informational dialog, no confirm button.
    return (
      <Dialog open={profile !== null} onOpenChange={onOpenChange}>
        {profile && (
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>Can&apos;t delete {profile.name}</DialogTitle>
              <DialogDescription>
                {appsUsingIt > 0 && (
                  <>
                    {appsUsingIt} {appsUsingIt === 1 ? "app is" : "apps are"}{" "}
                    still using this profile. Reassign{" "}
                    {appsUsingIt === 1 ? "that app" : "those apps"}
                    {mappingsUsingIt > 0 ? " and " : " "}
                  </>
                )}
                {mappingsUsingIt > 0 && (
                  <>
                    {mappingsUsingIt} active{" "}
                    {mappingsUsingIt === 1
                      ? "profile mapping is"
                      : "profile mappings are"}{" "}
                    still assigning it. Reassign or disable{" "}
                    {mappingsUsingIt === 1 ? "that mapping" : "those mappings"}
                  </>
                )}
                {" before deleting this profile."}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="primary" onClick={() => onOpenChange(false)}>
                Got it
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    );
  }

  if (dependenciesLoading || dependenciesUnavailable) {
    return (
      <Dialog open={profile !== null} onOpenChange={onOpenChange}>
        {profile && (
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>Check dependencies first</DialogTitle>
              <DialogDescription>
                {dependenciesLoading
                  ? "Checking whether apps or profile mappings still use this profile."
                  : "The profile dependency check is unavailable. Refresh the route before attempting deletion again."}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="primary" onClick={() => onOpenChange(false)}>
                Close
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    );
  }

  return (
    <ConfirmDialog
      open={profile !== null}
      onOpenChange={onOpenChange}
      title="Delete profile?"
      description={
        profile ? (
          <>
            <strong>{profile.name}</strong> will be permanently deleted. No apps
            or active profile mappings currently reference it.
          </>
        ) : undefined
      }
      confirmLabel="Delete profile"
      typedName={profile?.name}
      onConfirm={onConfirm}
    />
  );
}

// ---------------------------------------------------------------------------
// Test access dialog (POST /policy/simulate)

/**
 * TestAccessDialog — two calling modes:
 *   1. Row overflow: pass `profile` (fixed) + `open` + `onOpenChange`.
 *   2. Section header: pass `profiles` (list) + `open` + `onOpenChange`;
 *      the user picks the profile inside the dialog.
 */
function TestAccessDialog(
  props:
    | {
        profile: Profile;
        profiles?: never;
        open: boolean;
        onOpenChange: (open: boolean) => void;
      }
    | {
        profile?: never;
        profiles: Profile[];
        open: boolean;
        onOpenChange: (open: boolean) => void;
      },
) {
  const { open, onOpenChange } = props;
  // When a fixed profile is provided (row mode) we don't render the profile Select.
  const fixedProfile = props.profile ?? null;
  const profileList = useMemo(
    () => props.profiles ?? (fixedProfile ? [fixedProfile] : []),
    [fixedProfile, props.profiles],
  );

  // Retain the user's choice, but derive its usable value from the current
  // list. A refetch can replace that list while this dialog is open.
  const [selectedProfileId, setSelectedProfileId] = useState<string>(
    fixedProfile?.id ?? profileList[0]?.id ?? "",
  );
  const visibleSelectedProfileId = fixedProfile
    ? fixedProfile.id
    : profileList.some((profile) => profile.id === selectedProfileId)
      ? selectedProfileId
      : (profileList[0]?.id ?? "");
  // Never submit a stale id: selection is valid only while it identifies a
  // currently visible profile (or the fixed row profile).
  const activeProfile =
    profileList.find((profile) => profile.id === visibleSelectedProfileId) ??
    null;
  const activeProfileId = activeProfile?.id ?? "";

  const [intent, setIntent] = useState<string>("");
  const [protocol, setProtocol] = useState<string>("");
  const [model, setModel] = useState<string>("");
  const [result, setResult] = useState<SimulateResult | null>(null);
  const [resultProfileId, setResultProfileId] = useState<string | null>(null);
  const [simError, setSimError] = useState<string | null>(null);
  const [simErrorProfileId, setSimErrorProfileId] = useState<string | null>(
    null,
  );
  const [expanded, setExpanded] = useState(false);

  const simulate = useMutation({
    mutationFn: () => {
      if (!activeProfileId)
        return Promise.reject(new Error("No profile selected"));
      return api.policy.simulate({
        profile: activeProfileId,
        request: {
          protocol: protocol || null,
          intent: intent || null,
          model: model.trim() || null,
          workflow: null,
          backend: null,
        },
      });
    },
    onSuccess: (data) => {
      setResult(data);
      setResultProfileId(activeProfileId);
      setSimError(null);
      setSimErrorProfileId(null);
    },
    onError: (error) => {
      setSimError(errorMessage(error));
      setSimErrorProfileId(activeProfileId);
      setResult(null);
      setResultProfileId(null);
    },
  });

  const titleSuffix = activeProfile ? ` — ${activeProfile.name}` : "";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Test access{titleSuffix}</DialogTitle>
          <DialogDescription>
            Simulate a request against this profile and see whether it would be
            allowed or denied, with the exact reason.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {/* Profile select — only shown in header (list) mode */}
          {!fixedProfile && profileList.length > 0 && (
            <Field label="Profile">
              <Select
                value={visibleSelectedProfileId}
                onChange={(e) => {
                  setSelectedProfileId(e.target.value);
                }}
                disabled={simulate.isPending}
              >
                {profileList.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </Select>
            </Field>
          )}

          {/* Disclosure toggle */}
          <button
            type="button"
            className="text-vf-small text-vf-muted hover:text-vf-text flex w-full items-center justify-between"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
          >
            <span className="font-medium">Request parameters</span>
            <span className="text-vf-micro font-mono uppercase">
              {expanded ? "collapse" : "expand"}
            </span>
          </button>

          {expanded && (
            <div className="flex flex-col gap-3">
              <Field label="Protocol" help="Optional — leave blank to omit.">
                <Input
                  value={protocol}
                  onChange={(e) => setProtocol(e.target.value)}
                  disabled={simulate.isPending}
                />
              </Field>
              <Field
                label="Capability / intent"
                help="Optional — leave blank to omit."
              >
                <Input
                  value={intent}
                  onChange={(e) => setIntent(e.target.value)}
                  disabled={simulate.isPending}
                />
              </Field>
              <Field label="Model" help="Optional — leave blank to omit.">
                <Input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="Model ID"
                  disabled={simulate.isPending}
                />
              </Field>
            </div>
          )}

          {/* Result */}
          {result && resultProfileId === activeProfileId && (
            <SimulateResultView result={result} />
          )}

          {simError && simErrorProfileId === activeProfileId && (
            <FormErrorSummary message={simError} />
          )}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={simulate.isPending}
          >
            Close
          </Button>
          <Button
            variant="primary"
            loading={simulate.isPending}
            disabled={!activeProfileId}
            onClick={() => simulate.mutate()}
          >
            Run test
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function SimulateResultView({ result }: { result: SimulateResult }) {
  const { decision } = result;
  const allowed = decision.allowed;

  return (
    <div className="rounded-vf-md border-vf-border flex flex-col gap-3 border p-4">
      <div className="flex items-center gap-2">
        <Badge sm tone={allowed ? "ok" : "bad"}>
          {decision.decision}
        </Badge>
        <span className="text-vf-small text-vf-text font-medium">
          {allowed ? "Request would be allowed" : "Request would be denied"}
        </span>
      </div>
      {decision.reasons.length > 0 && (
        <ul className="flex flex-col gap-1">
          {decision.reasons.map((reason, i) => (
            <li key={i} className="text-vf-small text-vf-muted font-mono">
              {reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

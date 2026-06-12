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

import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type Profile,
  type SimulateResult,
} from "@/lib/api";
import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  DataTable,
  type DataTableColumn,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  EntityRef,
  Field,
  Input,
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
  SectionHeader,
  Select,
  Skeleton,
  toast,
} from "@/components/ui";

// ---------------------------------------------------------------------------
// Helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

/** Known gateway protocols in a human order. */
const KNOWN_PROTOCOLS = ["openai", "anthropic", "google", "azure"];

/** Known intents mapped to human-readable capability names. */
const INTENT_LABELS: Record<string, string> = {
  "chat.completions": "chat",
  "completions": "completions",
  "embeddings": "embeddings",
  "images.generate": "images",
  "images.edit": "image edit",
  "audio.speech": "speech",
  "audio.transcriptions": "transcriptions",
  "video.generate": "video",
  "moderations": "moderations",
};

/** Derive the human access summary from a profile.
 *
 * Produces e.g. "4 models · chat, images" or "all models · chat"
 * or "all access" when unrestricted. */
function accessSummary(profile: Profile): string {
  const intents: string[] = [];

  // The allow block lives in the policy document
  const allow = profile.profile?.allow as
    | { intents?: string[]; models?: string[] }
    | undefined;

  if (allow?.intents && allow.intents.length > 0) {
    for (const intent of allow.intents) {
      const label = INTENT_LABELS[intent] ?? intent.replace(".", " ");
      if (!intents.includes(label)) intents.push(label);
    }
  } else if (profile.workflows && profile.workflows.length > 0) {
    // workflows can substitute for explicit intents
    intents.push(...profile.workflows.map((w) => w.replace(/-/g, " ")));
  }

  const models = allow?.models;
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

// ---------------------------------------------------------------------------
// The surface

export default function AdminProfilesPage() {
  const queryClient = useQueryClient();

  const {
    data: profilesData,
    isPending: profilesPending,
    isError: profilesIsError,
    error: profilesError,
    refetch: refetchProfiles,
  } = useQuery({ queryKey: queryKeys.profiles, queryFn: api.profiles.list });

  const { data: appsData, isPending: appsPending } = useQuery({
    queryKey: queryKeys.apps,
    queryFn: api.apps.list,
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
    onError: (error) => {
      // 409 profile_in_use: the server blocks the delete — we surface the
      // reason in the dialog and close on success only.
      toast.error("Couldn't delete profile", {
        description: errorMessage(error),
      });
    },
  });

  const profiles = useMemo(
    () =>
      [...(profilesData ?? [])].sort((a, b) => b.created_at - a.created_at),
    [profilesData],
  );

  const loading = profilesPending;
  const failed = profilesIsError && !profilesData;

  const columns: DataTableColumn<Profile>[] = [
    {
      id: "profile",
      header: "Profile",
      cell: (profile) => (
        <EntityRef name={profile.name} id={profile.id} />
      ),
    },
    {
      id: "access",
      header: "Access",
      cell: (profile) => (
        <span className="text-vf-small text-vf-muted">{accessSummary(profile)}</span>
      ),
    },
    {
      id: "apps",
      header: "Apps using it",
      cell: (profile) => {
        if (appsPending) return <Skeleton className="h-4 w-8" />;
        const count = appCountByProfile.get(profile.id) ?? 0;
        return (
          <span className="text-vf-small text-vf-muted tabular-nums">
            {count === 0 ? (
              <span className="text-vf-subtle">—</span>
            ) : (
              `${count} ${count === 1 ? "app" : "apps"}`
            )}
          </span>
        );
      },
    },
    {
      id: "created",
      header: "Created",
      value: (profile) => profile.created_at,
      hideOnMobile: true,
    },
  ];

  return (
    <main className="mx-auto flex w-full max-w-[1080px] flex-col gap-10 px-4 py-8 md:px-6">
      <section className="flex flex-col gap-4">
        <SectionHeader
          kicker="access"
          title="Profiles"
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

        {failed ? (
          <ErrorBanner onRetry={() => void refetchProfiles()}>
            Couldn&apos;t load profiles. {errorMessage(profilesError)}
          </ErrorBanner>
        ) : (
          <DataTable
            label="Profiles"
            columns={columns}
            rows={profiles}
            rowKey={(profile) => profile.id}
            loading={loading}
            empty={
              <EmptyState
                action={
                  <Button onClick={() => setCreateOpen(true)}>
                    Create profile
                  </Button>
                }
              >
                No profiles yet. Create one to control what callers can reach.
              </EmptyState>
            }
            rowMenu={(profile) => (
              <OverflowMenu label={`Actions for ${profile.name}`}>
                <OverflowMenuItem onSelect={() => setEditTarget(profile)}>
                  Edit…
                </OverflowMenuItem>
                <OverflowMenuItem onSelect={() => setTestTarget(profile)}>
                  Test access…
                </OverflowMenuItem>
                <OverflowMenuSeparator />
                <OverflowMenuItem
                  danger
                  onSelect={() => setDeleteTarget(profile)}
                >
                  Delete…
                </OverflowMenuItem>
              </OverflowMenu>
            )}
          />
        )}
      </section>

      {/* Create dialog */}
      <ProfileDialog
        mode="create"
        open={createOpen}
        onOpenChange={setCreateOpen}
      />

      {/* Edit dialog */}
      <ProfileDialog
        mode="edit"
        profile={editTarget ?? undefined}
        open={editTarget !== null}
        onOpenChange={(open) => {
          if (!open) setEditTarget(null);
        }}
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
          deleteTarget
            ? (appCountByProfile.get(deleteTarget.id) ?? 0)
            : 0
        }
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        onConfirm={() => {
          if (deleteTarget) deleteProfile.mutate(deleteTarget);
        }}
      />
    </main>
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
    <div
      role="alert"
      className="rounded-vf-md border-vf-bad/40 bg-vf-bad/10 flex flex-wrap items-center justify-between gap-3 border p-4"
    >
      <p className="text-vf-small text-vf-text">{children}</p>
      <Button size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create / Edit dialog (progressively disclosed)

interface ProfileFormState {
  name: string;
  description: string;
  protocols: string[];
  /** Comma-separated model list (free-form; '' = unrestricted). */
  models: string;
  intents: string[];
  statusVisibility: string;
}

function defaultFormState(profile?: Profile): ProfileFormState {
  if (!profile) {
    return {
      name: "",
      description: "",
      protocols: ["openai"],
      models: "",
      intents: [],
      statusVisibility: "basic",
    };
  }
  const allow = profile.profile?.allow as
    | { intents?: string[]; models?: string[] }
    | undefined;
  return {
    name: profile.name,
    description: profile.description ?? "",
    protocols: profile.protocols ?? ["openai"],
    models: (allow?.models ?? []).join(", "),
    intents: allow?.intents ?? [],
    statusVisibility: profile.status_visibility ?? "basic",
  };
}

function ProfileDialog({
  mode,
  profile,
  open,
  onOpenChange,
}: {
  mode: "create" | "edit";
  profile?: Profile;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <ProfileDialogContent
          mode={mode}
          profile={profile}
          onOpenChange={onOpenChange}
        />
      )}
    </Dialog>
  );
}

function ProfileDialogContent({
  mode,
  profile,
  onOpenChange,
}: {
  mode: "create" | "edit";
  profile?: Profile;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ProfileFormState>(() =>
    defaultFormState(profile),
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

  function toggleProtocol(protocol: string) {
    setForm((prev) => ({
      ...prev,
      protocols: prev.protocols.includes(protocol)
        ? prev.protocols.filter((p) => p !== protocol)
        : [...prev.protocols, protocol],
    }));
  }

  function toggleIntent(intent: string) {
    setForm((prev) => ({
      ...prev,
      intents: prev.intents.includes(intent)
        ? prev.intents.filter((i) => i !== intent)
        : [...prev.intents, intent],
    }));
  }

  function buildProfileDoc() {
    const modelsRaw = form.models
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean);

    const allow: Record<string, unknown> = {};
    if (form.intents.length > 0) allow.intents = form.intents;
    if (modelsRaw.length > 0) allow.models = modelsRaw;

    return {
      protocols: form.protocols,
      status_visibility: { level: form.statusVisibility },
      ...(Object.keys(allow).length > 0 ? { allow } : {}),
    };
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
      <form onSubmit={handleSubmit}>
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

          {/* Progressive disclosure: advanced access rules */}
          <div className="border-vf-border rounded-vf-md border">
            <button
              type="button"
              className="text-vf-small text-vf-muted hover:text-vf-text flex w-full items-center justify-between rounded-[inherit] px-4 py-3 transition-colors"
              onClick={() => setAdvancedOpen((v) => !v)}
              aria-expanded={advancedOpen}
            >
              <span className="font-medium">Access rules</span>
              <span className="text-vf-micro font-mono tracking-wider uppercase">
                {advancedOpen ? "collapse" : "expand"}
              </span>
            </button>

            {advancedOpen && (
              <div className="border-vf-border flex flex-col gap-4 border-t px-4 py-4">
                {/* Protocols */}
                <Field
                  label="Protocols"
                  help="Which API protocols this profile allows. At least one required."
                >
                  <div className="flex flex-wrap gap-3">
                    {KNOWN_PROTOCOLS.map((p) => (
                      <Checkbox
                        key={p}
                        checked={form.protocols.includes(p)}
                        onCheckedChange={() => toggleProtocol(p)}
                        disabled={save.isPending}
                      >
                        {p}
                      </Checkbox>
                    ))}
                  </div>
                </Field>

                {/* Capabilities / intents */}
                <Field
                  label="Capabilities"
                  help="Leave all unchecked to allow all capabilities."
                >
                  <div className="flex flex-wrap gap-3">
                    {Object.entries(INTENT_LABELS).map(([intent, label]) => (
                      <Checkbox
                        key={intent}
                        checked={form.intents.includes(intent)}
                        onCheckedChange={() => toggleIntent(intent)}
                        disabled={save.isPending}
                      >
                        {label}
                      </Checkbox>
                    ))}
                  </div>
                </Field>

                {/* Model allowlist */}
                <Field
                  label="Model allowlist"
                  help="Comma-separated model IDs. Leave blank to allow all models."
                >
                  <Input
                    value={form.models}
                    onChange={(e) => setField("models", e.target.value)}
                    placeholder="e.g. gpt-4o, claude-3-5-sonnet"
                    disabled={save.isPending}
                  />
                </Field>

                {/* Status visibility */}
                <Field
                  label="Status visibility"
                  help="How much queue/status info callers can see."
                >
                  <Select
                    value={form.statusVisibility}
                    onChange={(e) =>
                      setField("statusVisibility", e.target.value)
                    }
                    disabled={save.isPending}
                  >
                    <option value="none">None</option>
                    <option value="basic">Basic</option>
                    <option value="full">Full</option>
                  </Select>
                </Field>
              </div>
            )}
          </div>

          {formError && (
            <p role="alert" className="text-vf-small text-vf-bad">
              {formError}
            </p>
          )}
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
      </form>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Delete profile dialog (explains + blocks when apps still reference it)

function DeleteProfileDialog({
  profile,
  appsUsingIt,
  onOpenChange,
  onConfirm,
}: {
  profile: Profile | null;
  appsUsingIt: number;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const inUse = appsUsingIt > 0;

  if (inUse) {
    // Block the delete: show an informational dialog, no confirm button.
    return (
      <Dialog open={profile !== null} onOpenChange={onOpenChange}>
        {profile && (
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>Can&apos;t delete {profile.name}</DialogTitle>
              <DialogDescription>
                {appsUsingIt}{" "}
                {appsUsingIt === 1 ? "app is" : "apps are"} still using this
                profile. Reassign{" "}
                {appsUsingIt === 1 ? "that app" : "those apps"} to a different
                profile before deleting this one.
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

  return (
    <ConfirmDialog
      open={profile !== null}
      onOpenChange={onOpenChange}
      title="Delete profile?"
      description={
        profile ? (
          <>
            <strong>{profile.name}</strong> will be permanently deleted. Any
            app that was using it will fall back to the gateway default.
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

const SIMULATE_INTENTS = Object.keys(INTENT_LABELS);
const SIMULATE_PROTOCOLS = KNOWN_PROTOCOLS;

/**
 * TestAccessDialog — two calling modes:
 *   1. Row overflow: pass `profile` (fixed) + `open` + `onOpenChange`.
 *   2. Section header: pass `profiles` (list) + `open` + `onOpenChange`;
 *      the user picks the profile inside the dialog.
 */
function TestAccessDialog(
  props:
    | { profile: Profile; profiles?: never; open: boolean; onOpenChange: (open: boolean) => void }
    | { profile?: never; profiles: Profile[]; open: boolean; onOpenChange: (open: boolean) => void },
) {
  const { open, onOpenChange } = props;
  // When a fixed profile is provided (row mode) we don't render the profile Select.
  const fixedProfile = props.profile ?? null;
  const profileList = props.profiles ?? (fixedProfile ? [fixedProfile] : []);

  // Selected profile id — default to the fixed profile or the first in the list.
  const [selectedProfileId, setSelectedProfileId] = useState<string>(
    fixedProfile?.id ?? profileList[0]?.id ?? "",
  );

  // Keep selected id in sync when the list or fixed profile changes (e.g. after refetch).
  const activeProfileId = fixedProfile?.id ?? selectedProfileId;
  const activeProfile =
    profileList.find((p) => p.id === activeProfileId) ?? fixedProfile ?? null;

  const [intent, setIntent] = useState<string>("chat.completions");
  const [protocol, setProtocol] = useState<string>(
    activeProfile?.protocols[0] ?? "openai",
  );
  const [model, setModel] = useState<string>("");
  const [result, setResult] = useState<SimulateResult | null>(null);
  const [simError, setSimError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const simulate = useMutation({
    mutationFn: () => {
      if (!activeProfileId) return Promise.reject(new Error("No profile selected"));
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
      setSimError(null);
    },
    onError: (error) => {
      setSimError(errorMessage(error));
      setResult(null);
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
                value={selectedProfileId}
                onChange={(e) => {
                  setSelectedProfileId(e.target.value);
                  setResult(null);
                  setSimError(null);
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
            <span className="text-vf-micro font-mono tracking-wider uppercase">
              {expanded ? "collapse" : "expand"}
            </span>
          </button>

          {expanded && (
            <div className="flex flex-col gap-3">
              <Field label="Protocol">
                <Select
                  value={protocol}
                  onChange={(e) => setProtocol(e.target.value)}
                  disabled={simulate.isPending}
                >
                  {SIMULATE_PROTOCOLS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Capability / intent">
                <Select
                  value={intent}
                  onChange={(e) => setIntent(e.target.value)}
                  disabled={simulate.isPending}
                >
                  {SIMULATE_INTENTS.map((i) => (
                    <option key={i} value={i}>
                      {INTENT_LABELS[i]} ({i})
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Model" help="Optional — leave blank to omit.">
                <Input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="e.g. gpt-4o"
                  disabled={simulate.isPending}
                />
              </Field>
            </div>
          )}

          {/* Result */}
          {result && (
            <SimulateResultView result={result} />
          )}

          {simError && (
            <p role="alert" className="text-vf-small text-vf-bad">
              {simError}
            </p>
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
            <li
              key={i}
              className="text-vf-small text-vf-muted font-mono"
            >
              {reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

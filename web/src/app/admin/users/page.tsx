"use client";

/** Admin → Users — surface spec (docs/design/WEB_UI_STEERING.md).
 *
 * People table (no CRUD — identities come from the sign-in provider; the
 * empty/explainer copy says exactly that). Apps count is client-side from
 * the apps list. Below the table: "Default access" group — user-access-defaults
 * editor (PUT /operator/user-access-defaults) + runtime profile mappings
 * (list, create Dialog, disable via OverflowMenu + ConfirmDialog). Copy
 * answers "what does a brand-new user get?".
 *
 * Presentation law observed:
 *  - RelativeTime for every timestamp
 *  - Human names first; IDs stay in secondary copy treatment
 *  - No window.confirm()
 *  - Destructive actions in OverflowMenu + ConfirmDialog
 *  - Loading skeleton + designed empty + error states
 *  - React Query via the same patterns as the apps page
 *  - All data via api.* from lib/api.ts */

import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type Profile,
  type User,
  type UserAccessDefaults,
  type RuntimeProfileMapping,
  type PolicyEditorContract,
} from "@/lib/api";
import {
  Badge,
  Button,
  ConfirmDialog,
  DataTable,
  type DataTableColumn,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  EmptyOperationalState,
  EntityRef,
  Field,
  Input,
  NoticeBanner,
  OverflowMenu,
  OverflowMenuItem,
  RelativeTime,
  SectionActionHeader,
  Select,
  Skeleton,
  SummaryStrip,
  Switch,
  toast,
} from "@/components/ui";

// ---------------------------------------------------------------------------
// Stable query key aliases (aliased from global queryKeys for local readability)

const QK_OVERRIDES = queryKeys.operatorOverrides;
const QK_MAPPINGS = queryKeys.runtimeProfileMappings;

// ---------------------------------------------------------------------------
// Helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

function userDisplayName(user: User): string {
  return user.display_name ?? user.name ?? user.email ?? "Identity unavailable";
}

function roleBadgeTone(
  role: string,
): "ok" | "warn" | "bad" | "info" | "accent" | "neutral" {
  if (role === "owner") return "accent";
  if (role === "admin") return "info";
  return "neutral";
}

function sourceDescriptor(
  editor: PolicyEditorContract | undefined,
  sourceKind: string,
) {
  return editor?.runtime_mappings.source_kinds.find(
    (source) => source.id === sourceKind,
  );
}

function mappingLabel(
  mapping: RuntimeProfileMapping,
  editor?: PolicyEditorContract,
): string {
  const source = sourceDescriptor(editor, mapping.source_kind);
  if (!source)
    return `${mapping.source_kind} — Not currently advertised: ${mapping.source_value}`;
  if (source.value_mode === "fixed") return source.label;
  return `${source.label}: ${mapping.source_value}`;
}

function profileName(
  profileId: string,
  profiles: Profile[],
  editor?: PolicyEditorContract,
): string {
  return (
    profiles.find((p) => p.id === profileId)?.name ??
    (editor?.runtime_mappings.fallback_profile.id === profileId
      ? editor.runtime_mappings.fallback_profile.label
      : "Profile unavailable")
  );
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

// ---------------------------------------------------------------------------
// Surface

export default function AdminUsersPage() {
  const queryClient = useQueryClient();

  const {
    data: usersData,
    isPending: usersPending,
    isError: usersIsError,
    error: usersError,
    refetch: refetchUsers,
  } = useQuery({ queryKey: queryKeys.users, queryFn: api.users.list });

  const {
    data: appsData,
    isPending: appsPending,
    isError: appsIsError,
    refetch: refetchApps,
  } = useQuery({ queryKey: queryKeys.apps, queryFn: api.apps.list });

  const {
    data: defaultsData,
    isPending: defaultsPending,
    isError: defaultsIsError,
    refetch: refetchDefaults,
  } = useQuery({ queryKey: QK_OVERRIDES, queryFn: api.userAccessDefaults.get });

  const {
    data: mappingsData,
    isPending: mappingsPending,
    isError: mappingsIsError,
    refetch: refetchMappings,
  } = useQuery({
    queryKey: QK_MAPPINGS,
    queryFn: api.runtimeProfileMappings.list,
  });

  const { data: profilesData } = useQuery({
    queryKey: queryKeys.profiles,
    queryFn: api.profiles.list,
  });
  const {
    data: editor,
    isError: editorIsError,
    isPending: editorPending,
  } = useQuery({
    queryKey: queryKeys.policyEditor,
    queryFn: api.policyEditor.get,
  });

  const profiles = profilesData ?? [];

  // Client-side: apps count per user
  const appCountByUser = useMemo(() => {
    const counts = new Map<string, number>();
    for (const app of appsData ?? []) {
      if (app.owner_user_id) {
        counts.set(app.owner_user_id, (counts.get(app.owner_user_id) ?? 0) + 1);
      }
    }
    return counts;
  }, [appsData]);

  const users = useMemo(
    () => [...(usersData ?? [])].sort((a, b) => a.created_at - b.created_at),
    [usersData],
  );
  const adminCount = users.filter((user) =>
    user.roles.some((role) => role === "admin" || role === "owner"),
  ).length;

  const mappings = useMemo(
    () => [...(mappingsData ?? [])].sort((a, b) => a.created_at - b.created_at),
    [mappingsData],
  );

  // Dialog state
  const [createMappingOpen, setCreateMappingOpen] = useState(false);
  const [disableMappingTarget, setDisableMappingTarget] =
    useState<RuntimeProfileMapping | null>(null);

  // Optimistic disable mapping
  const disableMappingMutation = useMutation({
    mutationFn: (mapping: RuntimeProfileMapping) =>
      api.runtimeProfileMappings.disable(mapping.id),
    onMutate: async (mapping) => {
      await queryClient.cancelQueries({ queryKey: QK_MAPPINGS });
      const previous =
        queryClient.getQueryData<RuntimeProfileMapping[]>(QK_MAPPINGS);
      queryClient.setQueryData<RuntimeProfileMapping[]>(QK_MAPPINGS, (rows) =>
        rows?.filter((r) => r.id !== mapping.id),
      );
      return { previous };
    },
    onError: (error, mapping, context) => {
      if (context?.previous) {
        queryClient.setQueryData(QK_MAPPINGS, context.previous);
      }
      toast.error(`Couldn't disable mapping`, {
        description: errorMessage(error),
      });
    },
    onSuccess: () => {
      toast.success("Mapping disabled");
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: QK_MAPPINGS }),
  });

  const loading = usersPending || appsPending;
  const tableError = usersIsError && !usersData;
  const appsJoinError = !tableError && appsIsError && !appsData;

  function retryAll() {
    void refetchUsers();
    void refetchApps();
    void refetchDefaults();
    void refetchMappings();
  }

  const userColumns: DataTableColumn<User>[] = [
    {
      id: "user",
      header: "User",
      cell: (user) => (
        <span className="flex min-w-0 flex-col gap-0.5">
          <EntityRef
            name={userDisplayName(user)}
            id={user.id}
            idVisibility="hover"
          />
          {user.email && user.email !== userDisplayName(user) && (
            <span className="text-vf-micro text-vf-subtle">{user.email}</span>
          )}
        </span>
      ),
    },
    {
      id: "role",
      header: "Role",
      cell: (user) => (
        <span className="flex flex-wrap gap-1">
          {user.roles.length === 0 ? (
            <Badge sm>user</Badge>
          ) : (
            user.roles.map((role) => (
              <Badge key={role} sm tone={roleBadgeTone(role)}>
                {role}
              </Badge>
            ))
          )}
        </span>
      ),
    },
    {
      id: "apps",
      header: "Apps",
      cell: (user) => {
        if (appsPending) return <Skeleton className="h-4 w-8" />;
        if (appsJoinError)
          return (
            <span className="text-vf-small text-vf-muted">unavailable</span>
          );
        const count = appCountByUser.get(user.id) ?? 0;
        return (
          <span className="text-vf-small text-vf-muted">
            {count} {count === 1 ? "app" : "apps"}
          </span>
        );
      },
      hideOnMobile: true,
    },
    {
      id: "last_login",
      header: "Last sign-in",
      cell: (user) => <RelativeTime ts={user.last_login_at} fallback="Never" />,
      hideOnMobile: true,
    },
    {
      id: "created",
      header: "Joined",
      value: (user) => user.created_at,
      hideOnMobile: true,
    },
  ];

  return (
    <div className="flex w-full flex-col gap-8">
      {/* People table */}
      <section className="flex flex-col gap-4">
        <SectionActionHeader
          kicker="people"
          title="Users"
          count={usersData && users.length > 0 ? users.length : undefined}
          description="Signed-in people, their gateway roles, and the default access new users receive."
        />

        <SummaryStrip
          items={[
            {
              label: "users",
              value: usersData
                ? String(users.length)
                : usersIsError
                  ? "Unavailable"
                  : "…",
              detail: usersData ? "signed in" : "user data",
              tone: "info",
              badge: usersData ? "live" : usersIsError ? "degraded" : "loading",
            },
            {
              label: "admins",
              value: usersData
                ? String(adminCount)
                : usersIsError
                  ? "Unavailable"
                  : "…",
              detail: usersData ? "owner or admin" : "user data",
              tone: usersData && adminCount > 0 ? "accent" : "neutral",
              badge: usersData
                ? adminCount > 0
                  ? "set"
                  : "review"
                : usersIsError
                  ? "degraded"
                  : "loading",
            },
            {
              label: "mappings",
              value: mappingsData
                ? String(mappings.length)
                : mappingsIsError
                  ? "Unavailable"
                  : "…",
              detail: mappingsData ? "profile rules" : "profile rule data",
              tone: "neutral",
              badge: mappingsData
                ? "auto"
                : mappingsIsError
                  ? "degraded"
                  : "loading",
            },
            {
              label: "self-service",
              value: defaultsData
                ? defaultsData.self_service_apps
                  ? "on"
                  : "off"
                : defaultsIsError
                  ? "Unavailable"
                  : "…",
              detail: defaultsData ? "app creation" : "settings unavailable",
              tone: !defaultsData
                ? "neutral"
                : defaultsData.self_service_apps
                  ? "ok"
                  : "warn",
              badge: !defaultsData
                ? defaultsIsError
                  ? "degraded"
                  : "loading"
                : defaultsData.self_service_apps
                  ? "enabled"
                  : "off",
            },
          ]}
        />

        {appsJoinError && (
          <ErrorBanner onRetry={retryAll}>
            App counts couldn&apos;t load — the Apps column may be incomplete.
          </ErrorBanner>
        )}

        {tableError ? (
          <ErrorBanner onRetry={retryAll}>
            Couldn&apos;t load users. {errorMessage(usersError)}
          </ErrorBanner>
        ) : (
          <DataTable
            label="Users"
            columns={userColumns}
            rows={users}
            rowKey={(user) => user.id}
            loading={loading}
            skeletonRows={4}
            empty={
              <EmptyOperationalState
                title="No users yet"
                detail="Identities appear here after the first sign-in — they come from your sign-in provider, not from this page."
              />
            }
          />
        )}
      </section>

      {/* Default access group */}
      <section className="flex flex-col gap-6">
        <SectionActionHeader
          kicker="policy"
          title="Default access"
          description="What a brand-new user gets the moment they sign in for the first time."
        />

        {/* User-access-defaults editor */}
        <div className="flex flex-col gap-4">
          <SectionActionHeader
            title="Self-service settings"
            description="Controls what new users can do without admin action."
          />

          {defaultsIsError && !defaultsData ? (
            <ErrorBanner onRetry={refetchDefaults}>
              Couldn&apos;t load default access settings.
            </ErrorBanner>
          ) : (
            <DefaultsEditor
              defaults={defaultsData ?? null}
              loading={defaultsPending}
            />
          )}
        </div>

        <div className="border-vf-border border-t" />

        {/* Runtime profile mappings */}
        <div className="flex flex-col gap-4">
          <SectionActionHeader
            title="Profile mappings"
            count={mappings.length > 0 ? mappings.length : undefined}
            description="Map a sign-in attribute — OIDC group, email, domain — to a profile. New users matching a rule get that profile automatically."
            action={
              <Button
                size="sm"
                onClick={() => setCreateMappingOpen(true)}
                disabled={!editor && !editorPending}
              >
                Add mapping
              </Button>
            }
          />

          {mappingsIsError && !mappingsData ? (
            <ErrorBanner onRetry={refetchMappings}>
              Couldn&apos;t load profile mappings.
            </ErrorBanner>
          ) : (
            <MappingsTable
              mappings={mappings}
              profiles={profiles}
              users={users}
              editor={editor}
              loading={mappingsPending}
              onDisable={setDisableMappingTarget}
            />
          )}
        </div>
      </section>

      {/* Dialogs */}
      <CreateMappingDialog
        open={createMappingOpen}
        onOpenChange={setCreateMappingOpen}
        profiles={profiles}
        editor={editor}
        editorStale={editorIsError && Boolean(editor)}
        editorUnavailable={!editor && !editorPending}
      />

      {/* Destructive confirm */}
      <ConfirmDialog
        open={disableMappingTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDisableMappingTarget(null);
        }}
        title="Disable mapping?"
        description={
          disableMappingTarget
            ? `New users matching "${mappingLabel(disableMappingTarget, editor)}" will no longer be assigned the "${profileName(disableMappingTarget.profile_id, profiles, editor)}" profile automatically. Existing users are not affected.`
            : undefined
        }
        confirmLabel="Disable mapping"
        onConfirm={() =>
          disableMappingTarget
            ? disableMappingMutation
                .mutateAsync(disableMappingTarget)
                .then(() => undefined)
            : Promise.resolve()
        }
      />
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
// User-access-defaults inline editor

function DefaultsEditor({
  defaults,
  loading,
}: {
  defaults: UserAccessDefaults | null;
  loading: boolean;
}) {
  const queryClient = useQueryClient();
  const [localDefaults, setLocalDefaults] = useState<UserAccessDefaults | null>(
    null,
  );
  const [formError, setFormError] = useState<string | null>(null);

  const [saved, setSaved] = useState(false);

  // The "working copy" is either the local override (pending save) or the
  // server state. When loading completes we reset local state.
  const effective = localDefaults ?? defaults;

  const save = useMutation({
    mutationFn: (input: Partial<UserAccessDefaults>) =>
      api.userAccessDefaults.update(input),
    onSuccess: (serverDefaults) => {
      queryClient.setQueryData<UserAccessDefaults>(
        QK_OVERRIDES,
        serverDefaults,
      );
      setLocalDefaults(null);
      setFormError(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
      toast.success("Default access settings saved");
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSave(event: FormEvent) {
    event.preventDefault();
    if (!localDefaults) return;
    setFormError(null);
    save.mutate(localDefaults);
  }

  function toggle(key: keyof UserAccessDefaults, value: boolean) {
    setLocalDefaults((prev) => ({
      ...(prev ??
        defaults ?? {
          self_service_apps: false,
          auto_create_personal_app: false,
        }),
      [key]: value,
    }));
    setSaved(false);
  }

  if (loading) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-10 w-full max-w-sm" />
        <Skeleton className="h-10 w-full max-w-sm" />
      </div>
    );
  }

  const isDirty = localDefaults !== null;

  return (
    <form onSubmit={handleSave} className="flex flex-col gap-4">
      <div className="flex flex-col gap-3">
        <SwitchRow
          label="Allow self-service apps"
          description="New users can create their own apps without admin approval."
          checked={effective?.self_service_apps ?? false}
          disabled={save.isPending}
          onCheckedChange={(value) => toggle("self_service_apps", value)}
        />
        <SwitchRow
          label="Auto-create personal app"
          description="A personal app is created automatically the first time a user signs in."
          checked={effective?.auto_create_personal_app ?? false}
          disabled={save.isPending}
          onCheckedChange={(value) => toggle("auto_create_personal_app", value)}
        />
      </div>

      {formError && <FormErrorSummary message={formError} />}

      <div className="flex items-center gap-3">
        <Button
          type="submit"
          size="sm"
          loading={save.isPending}
          disabled={!isDirty || save.isPending}
        >
          Save
        </Button>
        {saved && <span className="text-vf-small text-vf-ok">Saved</span>}
      </div>
    </form>
  );
}

// A single labeled switch row
function SwitchRow({
  label,
  description,
  checked,
  disabled,
  onCheckedChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  disabled: boolean;
  onCheckedChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex flex-col gap-0.5">
        <span className="text-vf-small text-vf-text font-medium">{label}</span>
        <span className="text-vf-small text-vf-muted">{description}</span>
      </div>
      <Switch
        checked={checked}
        disabled={disabled}
        onCheckedChange={onCheckedChange}
        aria-label={label}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Mappings table

interface MappingRow {
  mapping: RuntimeProfileMapping;
  profileLabel: string;
}

function MappingsTable({
  mappings,
  profiles,
  users,
  editor,
  loading,
  onDisable,
}: {
  mappings: RuntimeProfileMapping[];
  profiles: Profile[];
  users: User[];
  editor: PolicyEditorContract | undefined;
  loading: boolean;
  onDisable: (mapping: RuntimeProfileMapping) => void;
}) {
  const rows: MappingRow[] = mappings.map((mapping) => ({
    mapping,
    profileLabel: profileName(mapping.profile_id, profiles),
  }));

  const columns: DataTableColumn<MappingRow>[] = [
    {
      id: "match",
      header: "Match",
      cell: ({ mapping }) => (
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="text-vf-small text-vf-text">
            {mappingLabel(mapping, editor)}
          </span>
          <span className="text-vf-micro text-vf-subtle font-mono">
            {sourceDescriptor(editor, mapping.source_kind)?.label ??
              "Not currently advertised"}
          </span>
        </span>
      ),
    },
    {
      id: "profile",
      header: "Assign profile",
      cell: ({ profileLabel: label }) => (
        <span className="text-vf-small text-vf-text">{label}</span>
      ),
    },
    {
      id: "created_by",
      header: "Created by",
      cell: ({ mapping }) => (
        <EntityRef
          name={
            users.find((user) => user.id === mapping.created_by)
              ? userDisplayName(
                  users.find((user) => user.id === mapping.created_by)!,
                )
              : "Identity unavailable"
          }
          id={mapping.created_by}
          idVisibility="hover"
        />
      ),
      hideOnMobile: true,
    },
    {
      id: "created_at",
      header: "Created",
      value: ({ mapping }) => mapping.created_at,
      hideOnMobile: true,
    },
  ];

  return (
    <DataTable
      label="Profile mappings"
      columns={columns}
      rows={rows}
      rowKey={({ mapping }) => mapping.id}
      loading={loading}
      skeletonRows={3}
      empty={
        <EmptyOperationalState
          title="No profile mappings"
          detail="Add one to automatically assign a profile to new users based on their sign-in attributes."
        />
      }
      rowMenu={({ mapping }) => (
        <OverflowMenu
          label={`Actions for mapping ${mappingLabel(mapping, editor)}`}
        >
          <OverflowMenuItem danger onSelect={() => onDisable(mapping)}>
            Disable mapping…
          </OverflowMenuItem>
        </OverflowMenu>
      )}
    />
  );
}

// ---------------------------------------------------------------------------
// Create mapping dialog

function CreateMappingDialog({
  open,
  onOpenChange,
  profiles,
  editor,
  editorStale,
  editorUnavailable,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
  editor: PolicyEditorContract | undefined;
  editorStale: boolean;
  editorUnavailable: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <CreateMappingDialogContent
          onOpenChange={onOpenChange}
          profiles={profiles}
          editor={editor}
          editorStale={editorStale}
          editorUnavailable={editorUnavailable}
        />
      )}
    </Dialog>
  );
}

function CreateMappingDialogContent({
  onOpenChange,
  profiles,
  editor,
  editorStale,
  editorUnavailable,
}: {
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
  editor: PolicyEditorContract | undefined;
  editorStale: boolean;
  editorUnavailable: boolean;
}) {
  const queryClient = useQueryClient();
  const [sourceKind, setSourceKind] = useState("");
  const [sourceValue, setSourceValue] = useState("");
  const [profileId, setProfileId] = useState("");
  const [fieldErrors, setFieldErrors] = useState<{
    source_kind?: string;
    source_value?: string;
    profile_id?: string;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);

  const source = sourceDescriptor(editor, sourceKind);
  const sourceValueRequired = source?.value_mode === "required";

  const create = useMutation({
    mutationFn: () =>
      api.runtimeProfileMappings.create({
        source_kind: sourceKind,
        source_value:
          source?.value_mode === "fixed"
            ? (source.fixed_value ?? "")
            : sourceValue.trim(),
        profile_id: profileId,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: QK_MAPPINGS });
      toast.success("Profile mapping created");
      onOpenChange(false);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const errors: {
      source_kind?: string;
      source_value?: string;
      profile_id?: string;
    } = {};
    if (!source) errors.source_kind = "Choose a source to match.";
    if (sourceValueRequired && !sourceValue.trim()) {
      errors.source_value = "Enter a value to match against.";
    }
    if (!profileId) errors.profile_id = "Choose a profile to assign.";
    setFieldErrors(errors);
    setFormError(null);
    if (Object.keys(errors).length > 0) return;
    create.mutate();
  }

  return (
    <DialogContent>
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Add profile mapping</DialogTitle>
          <DialogDescription>
            New users whose sign-in matches this rule will be assigned the
            chosen profile automatically.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field
            label="Match on"
            help="The sign-in attribute to inspect."
            error={fieldErrors.source_kind}
          >
            <Select
              value={sourceKind}
              onChange={(event) => setSourceKind(event.target.value)}
              disabled={create.isPending || editorUnavailable}
            >
              <option value="">Select a source…</option>
              {(editor?.runtime_mappings.source_kinds ?? []).map((opt) => (
                <option key={opt.id} value={opt.id} disabled={editorStale}>
                  {opt.label}
                </option>
              ))}
            </Select>
          </Field>

          {sourceValueRequired && (
            <Field
              label="Value"
              required
              error={fieldErrors.source_value}
              help={source?.description ?? "Value to match"}
            >
              <Input
                value={sourceValue}
                onChange={(event) => setSourceValue(event.target.value)}
                placeholder="value"
                autoFocus
                disabled={create.isPending}
              />
            </Field>
          )}

          <Field
            label="Assign profile"
            help="The profile new matching users will receive."
            error={fieldErrors.profile_id}
          >
            <Select
              value={profileId}
              onChange={(event) => setProfileId(event.target.value)}
              disabled={create.isPending || editorUnavailable}
            >
              <option value="">Select a profile…</option>
              {editor?.runtime_mappings.fallback_profile.assignable && (
                <option value={editor.runtime_mappings.fallback_profile.id}>
                  {editor.runtime_mappings.fallback_profile.label}
                </option>
              )}
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name}
                </option>
              ))}
            </Select>
          </Field>

          {formError && <FormErrorSummary message={formError} />}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={create.isPending || editorUnavailable || editorStale}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={create.isPending}>
            Add mapping
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

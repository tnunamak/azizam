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
 *  - EntityRef for every id
 *  - No window.confirm()
 *  - Destructive actions in OverflowMenu + ConfirmDialog
 *  - Loading skeleton + designed empty + error states
 *  - React Query via the same patterns as the apps page
 *  - All data via api.* from lib/api.ts */

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
  type User,
  type UserAccessDefaults,
  type RuntimeMappingSourceKind,
  type RuntimeProfileMapping,
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
  EmptyState,
  EntityRef,
  Field,
  Input,
  OverflowMenu,
  OverflowMenuItem,
  RelativeTime,
  SectionHeader,
  Select,
  Skeleton,
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
  return user.display_name ?? user.name ?? user.email ?? user.id;
}

function roleBadgeTone(
  role: string,
): "ok" | "warn" | "bad" | "info" | "accent" | "neutral" {
  if (role === "owner") return "accent";
  if (role === "admin") return "info";
  return "neutral";
}

const SOURCE_KIND_LABELS: Record<RuntimeMappingSourceKind, string> = {
  oidc_group: "OIDC group",
  oidc_role: "OIDC role",
  email: "Email",
  domain: "Domain",
  gateway_role: "Gateway role",
  all_users: "All users",
};

function mappingLabel(mapping: RuntimeProfileMapping): string {
  if (mapping.source_kind === "all_users") return "Everyone (catch-all)";
  return `${SOURCE_KIND_LABELS[mapping.source_kind]}: ${mapping.source_value}`;
}

const FALLBACK_PROFILE_ID = "default-local";
const FALLBACK_PROFILE_LABEL = "Gateway default";

function profileName(profileId: string, profiles: Profile[]): string {
  if (profileId === FALLBACK_PROFILE_ID) return FALLBACK_PROFILE_LABEL;
  return profiles.find((p) => p.id === profileId)?.name ?? profileId;
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
  } = useQuery({ queryKey: QK_MAPPINGS, queryFn: api.runtimeProfileMappings.list });

  const { data: profilesData } = useQuery({
    queryKey: queryKeys.profiles,
    queryFn: api.profiles.list,
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
    () =>
      [...(usersData ?? [])].sort((a, b) => a.created_at - b.created_at),
    [usersData],
  );

  const mappings = useMemo(
    () =>
      [...(mappingsData ?? [])].sort((a, b) => a.created_at - b.created_at),
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
      const previous = queryClient.getQueryData<RuntimeProfileMapping[]>(QK_MAPPINGS);
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
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: QK_MAPPINGS }),
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
          <EntityRef name={userDisplayName(user)} id={user.id} />
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
        if (appsPending)
          return <Skeleton className="h-4 w-8" />;
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
      cell: (user) => (
        <RelativeTime ts={user.last_login_at} fallback="Never" />
      ),
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
    <main className="mx-auto flex w-full max-w-[1080px] flex-col gap-10 px-4 py-8 md:px-6">
      {/* People table */}
      <section className="flex flex-col gap-4">
        <SectionHeader
          kicker="people"
          title="Users"
          description="Everyone who has signed in. Accounts are created automatically on first sign-in — there's no manual invite flow."
        />

        {appsJoinError && (
          <ErrorBanner onRetry={retryAll}>
            App counts couldn&apos;t load — the Apps column may be incomplete.
          </ErrorBanner>
        )}

        {tableError ? (
          <ErrorBanner onRetry={retryAll}>
            Couldn&apos;t load users.{" "}
            {errorMessage(usersError)}
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
              <EmptyState>
                No users yet. Identities appear here after the first sign-in —
                they come from your sign-in provider, not from this page.
              </EmptyState>
            }
          />
        )}
      </section>

      {/* Default access group */}
      <section className="flex flex-col gap-6">
        <div className="flex flex-col gap-1">
          <h3 className="text-vf-heading text-vf-text">Default access</h3>
          <p className="text-vf-small text-vf-muted">
            What a brand-new user gets the moment they sign in for the first
            time.
          </p>
        </div>

        {/* User-access-defaults editor */}
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <h4 className="text-vf-body text-vf-text font-medium">
              Self-service settings
            </h4>
            <p className="text-vf-small text-vf-muted">
              Controls what new users can do without admin action.
            </p>
          </div>

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
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="flex min-w-0 flex-col gap-1">
              <h4 className="text-vf-body text-vf-text font-medium">
                Profile mappings
              </h4>
              <p className="text-vf-small text-vf-muted">
                Map a sign-in attribute — OIDC group, email, domain — to a
                profile. New users matching a rule get that profile
                automatically.
              </p>
            </div>
            <Button size="sm" onClick={() => setCreateMappingOpen(true)}>
              Add mapping
            </Button>
          </div>

          {mappingsIsError && !mappingsData ? (
            <ErrorBanner onRetry={refetchMappings}>
              Couldn&apos;t load profile mappings.
            </ErrorBanner>
          ) : (
            <MappingsTable
              mappings={mappings}
              profiles={profiles}
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
            ? `New users matching "${mappingLabel(disableMappingTarget)}" will no longer be assigned the "${profileName(disableMappingTarget.profile_id, profiles)}" profile automatically. Existing users are not affected.`
            : undefined
        }
        confirmLabel="Disable mapping"
        onConfirm={() => {
          if (disableMappingTarget)
            disableMappingMutation.mutate(disableMappingTarget);
          setDisableMappingTarget(null);
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
// User-access-defaults inline editor

function DefaultsEditor({
  defaults,
  loading,
}: {
  defaults: UserAccessDefaults | null;
  loading: boolean;
}) {
  const queryClient = useQueryClient();
  const [localDefaults, setLocalDefaults] =
    useState<UserAccessDefaults | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // The "working copy" is either the local override (pending save) or the
  // server state. When loading completes we reset local state.
  const effective = localDefaults ?? defaults;

  const save = useMutation({
    mutationFn: (input: Partial<UserAccessDefaults>) =>
      api.userAccessDefaults.update(input),
    onSuccess: (serverDefaults) => {
      queryClient.setQueryData<UserAccessDefaults>(QK_OVERRIDES, serverDefaults);
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

  function toggle(
    key: keyof UserAccessDefaults,
    value: boolean,
  ) {
    setLocalDefaults((prev) => ({
      ...(prev ?? defaults ?? {
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
          onCheckedChange={(value) =>
            toggle("auto_create_personal_app", value)
          }
        />
      </div>

      {formError && (
        <p role="alert" className="text-vf-small text-vf-bad">
          {formError}
        </p>
      )}

      <div className="flex items-center gap-3">
        <Button
          type="submit"
          size="sm"
          loading={save.isPending}
          disabled={!isDirty || save.isPending}
        >
          Save
        </Button>
        {saved && (
          <span className="text-vf-small text-vf-ok">Saved</span>
        )}
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

const SOURCE_KIND_OPTIONS: { value: RuntimeMappingSourceKind; label: string }[] =
  [
    { value: "oidc_group", label: "OIDC group" },
    { value: "oidc_role", label: "OIDC role" },
    { value: "email", label: "Email address" },
    { value: "domain", label: "Email domain" },
    { value: "gateway_role", label: "Gateway role" },
    { value: "all_users", label: "All users (catch-all)" },
  ];

interface MappingRow {
  mapping: RuntimeProfileMapping;
  profileLabel: string;
}

function MappingsTable({
  mappings,
  profiles,
  loading,
  onDisable,
}: {
  mappings: RuntimeProfileMapping[];
  profiles: Profile[];
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
            {mappingLabel(mapping)}
          </span>
          <span className="text-vf-micro text-vf-subtle font-mono">
            {SOURCE_KIND_LABELS[mapping.source_kind]}
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
        <EntityRef name={mapping.created_by} id={mapping.created_by} idVisibility="hover" />
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
        <EmptyState>
          No profile mappings. Add one to automatically assign a profile to new
          users based on their sign-in attributes.
        </EmptyState>
      }
      rowMenu={({ mapping }) => (
        <OverflowMenu label={`Actions for mapping ${mappingLabel(mapping)}`}>
          <OverflowMenuItem
            danger
            onSelect={() => onDisable(mapping)}
          >
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
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <CreateMappingDialogContent
          onOpenChange={onOpenChange}
          profiles={profiles}
        />
      )}
    </Dialog>
  );
}

function CreateMappingDialogContent({
  onOpenChange,
  profiles,
}: {
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
}) {
  const queryClient = useQueryClient();
  const [sourceKind, setSourceKind] =
    useState<RuntimeMappingSourceKind>("oidc_group");
  const [sourceValue, setSourceValue] = useState("");
  const [profileId, setProfileId] = useState(FALLBACK_PROFILE_ID);
  const [fieldErrors, setFieldErrors] = useState<{
    source_value?: string;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);

  const sourceValueRequired = sourceKind !== "all_users";

  const create = useMutation({
    mutationFn: () =>
      api.runtimeProfileMappings.create({
        source_kind: sourceKind,
        source_value: sourceKind === "all_users" ? "" : sourceValue.trim(),
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
    const errors: { source_value?: string } = {};
    if (sourceValueRequired && !sourceValue.trim()) {
      errors.source_value = "Enter a value to match against.";
    }
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
          <Field label="Match on" help="The sign-in attribute to inspect.">
            <Select
              value={sourceKind}
              onChange={(event) =>
                setSourceKind(event.target.value as RuntimeMappingSourceKind)
              }
              disabled={create.isPending}
            >
              {SOURCE_KIND_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
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
              help={
                sourceKind === "email"
                  ? "Exact email address, e.g. tim@example.com"
                  : sourceKind === "domain"
                    ? "Domain without @, e.g. example.com"
                    : sourceKind === "oidc_group"
                      ? "Group name as it appears in the OIDC token"
                      : sourceKind === "oidc_role"
                        ? "Role name as it appears in the OIDC token"
                        : "Value to match"
              }
            >
              <Input
                value={sourceValue}
                onChange={(event) => setSourceValue(event.target.value)}
                placeholder={
                  sourceKind === "email"
                    ? "tim@example.com"
                    : sourceKind === "domain"
                      ? "example.com"
                      : "value"
                }
                autoFocus
                disabled={create.isPending}
              />
            </Field>
          )}

          <Field
            label="Assign profile"
            help="The profile new matching users will receive."
          >
            <Select
              value={profileId}
              onChange={(event) => setProfileId(event.target.value)}
              disabled={create.isPending}
            >
              <option value={FALLBACK_PROFILE_ID}>
                {FALLBACK_PROFILE_LABEL}
              </option>
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name}
                </option>
              ))}
            </Select>
          </Field>

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
            disabled={create.isPending}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={create.isPending}
          >
            Add mapping
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

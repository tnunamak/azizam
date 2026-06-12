"use client";

/** Admin → Apps & Keys — surface spec #1 (docs/design/WEB_UI_STEERING.md).
 *
 * List-first IA: the apps table opens the section; creation lives behind the
 * one accent Button. Owner names and key counts are client-side joins over
 * the users/credentials caches (web-api-contract.md). The presentation law
 * applies throughout: RelativeTime for every timestamp, EntityRef for every
 * id, SecretMask for key prefixes, ConfirmDialog (typed-name) for the
 * destructive actions, one OverflowMenu per row. The one-time minted key is
 * shown exactly once in the reveal dialog and never logged. */

import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  api,
  ApiError,
  queryKeys,
  type App,
  type AppKind,
  type Credential,
  type MintedCredential,
  type Profile,
  type User,
} from "@/lib/api";
import {
  Badge,
  Button,
  ConfirmDialog,
  CopyButton,
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
  RadioCard,
  RadioCards,
  RelativeTime,
  SecretMask,
  SectionHeader,
  Select,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
  toast,
} from "@/components/ui";

// ---------------------------------------------------------------------------
// Shared helpers

/** Built-in fallback profile; not in GET /profiles, so selects prepend it. */
const FALLBACK_PROFILE_ID = "default-local";
const FALLBACK_PROFILE_LABEL = "Gateway default";

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

function userLabel(user: User | undefined): string {
  return user?.display_name ?? user?.email ?? "Unknown user";
}

function isActiveKey(credential: Credential): boolean {
  return credential.revoked_at === null;
}

/** Operator-facing profile name. The built-in fallback's stored name is the
 * internal id ("default-local"); never show that as cell text (vocab law). */
function profileLabel(app: App): string {
  return app.effective_profile_source === "fallback"
    ? FALLBACK_PROFILE_LABEL
    : app.effective_profile_name;
}

/** Active keys first, newest first within each group. */
function sortKeys(keys: Credential[]): Credential[] {
  return [...keys].sort((a, b) => {
    if (isActiveKey(a) !== isActiveKey(b)) return isActiveKey(a) ? -1 : 1;
    return b.created_at - a.created_at;
  });
}

interface RevealedKey {
  credential: MintedCredential;
  /** e.g. "for brother-laptop" / "for CI deploys" — shown in the dialog. */
  context: string;
  kind: "app" | "admin";
}

// ---------------------------------------------------------------------------
// The surface (QueryClientProvider + Toaster are in the admin layout)

export default function AdminAppsPage() {
  const queryClient = useQueryClient();

  const {
    data: appsData,
    isPending: appsPending,
    isError: appsIsError,
    error: appsError,
    refetch: refetchApps,
  } = useQuery({ queryKey: queryKeys.apps, queryFn: api.apps.list });
  const {
    data: usersData,
    isPending: usersPending,
    isError: usersIsError,
    refetch: refetchUsers,
  } = useQuery({ queryKey: queryKeys.users, queryFn: api.users.list });
  const {
    data: credentialsData,
    isPending: credentialsPending,
    isError: credentialsIsError,
    error: credentialsError,
    refetch: refetchCredentials,
  } = useQuery({
    queryKey: queryKeys.credentials,
    queryFn: api.credentials.list,
  });
  const { data: profilesData, refetch: refetchProfiles } = useQuery({
    queryKey: queryKeys.profiles,
    queryFn: api.profiles.list,
  });
  const { data: sessionData } = useQuery({
    queryKey: queryKeys.session,
    queryFn: api.session,
  });

  const apps = useMemo(
    () => [...(appsData ?? [])].sort((a, b) => b.created_at - a.created_at),
    [appsData],
  );
  const usersById = useMemo(
    () => new Map((usersData ?? []).map((user) => [user.id, user])),
    [usersData],
  );
  const appKeysByApp = useMemo(() => {
    const grouped = new Map<string, Credential[]>();
    for (const credential of credentialsData ?? []) {
      if (credential.subject_kind !== "app" || !credential.app_id) continue;
      const list = grouped.get(credential.app_id) ?? [];
      list.push(credential);
      grouped.set(credential.app_id, list);
    }
    return grouped;
  }, [credentialsData]);
  const adminKeys = useMemo(
    () =>
      sortKeys(
        (credentialsData ?? []).filter(
          (credential) => credential.subject_kind !== "app",
        ),
      ),
    [credentialsData],
  );

  // Dialog / panel state. Targets hold the full row so dialogs can show
  // names without re-deriving.
  const [createOpen, setCreateOpen] = useState(false);
  const [adminKeyOpen, setAdminKeyOpen] = useState(false);
  const [mintTarget, setMintTarget] = useState<App | null>(null);
  const [profileTarget, setProfileTarget] = useState<App | null>(null);
  const [disableTarget, setDisableTarget] = useState<App | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<Credential | null>(null);
  const [keysAppId, setKeysAppId] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<RevealedKey | null>(null);

  const keysApp = keysAppId
    ? (apps.find((app) => app.id === keysAppId) ?? null)
    : null;

  // Optimistic disable: flip disabled_at in the cache immediately, roll back
  // on error. The contract has no re-enable endpoint, so no undo is offered.
  const disableApp = useMutation({
    mutationFn: (app: App) => api.apps.disable(app.id),
    onMutate: async (app) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.apps });
      const previous = queryClient.getQueryData<App[]>(queryKeys.apps);
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) =>
          row.id === app.id ? { ...row, disabled_at: nowSeconds() } : row,
        ),
      );
      return { previous };
    },
    onError: (error, app, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queryKeys.apps, context.previous);
      }
      toast.error(`Couldn't disable ${app.name}`, {
        description: errorMessage(error),
      });
    },
    onSuccess: (serverApp) => {
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) => (row.id === serverApp.id ? serverApp : row)),
      );
      toast.success(`${serverApp.name} disabled`, {
        description: "Its keys stop working immediately.",
        action: {
          label: "Undo",
          onClick: () => enableApp.mutate(serverApp),
        },
      });
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apps }),
  });

  // Re-enable: the undo for optimistic disable.
  const enableApp = useMutation({
    mutationFn: (app: App) => api.apps.enable(app.id),
    onMutate: async (app) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.apps });
      const previous = queryClient.getQueryData<App[]>(queryKeys.apps);
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) =>
          row.id === app.id ? { ...row, disabled_at: null } : row,
        ),
      );
      return { previous };
    },
    onError: (error, app, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queryKeys.apps, context.previous);
      }
      toast.error(`Couldn't re-enable ${app.name}`, {
        description: errorMessage(error),
      });
    },
    onSuccess: (serverApp) => {
      queryClient.setQueryData<App[]>(queryKeys.apps, (rows) =>
        rows?.map((row) => (row.id === serverApp.id ? serverApp : row)),
      );
      toast.success(`${serverApp.name} re-enabled`);
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apps }),
  });

  // Optimistic revoke, same shape, on the credentials cache.
  const revokeKey = useMutation({
    mutationFn: (credential: Credential) =>
      api.credentials.revoke(credential.id),
    onMutate: async (credential) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.credentials });
      const previous = queryClient.getQueryData<Credential[]>(
        queryKeys.credentials,
      );
      queryClient.setQueryData<Credential[]>(queryKeys.credentials, (rows) =>
        rows?.map((row) =>
          row.id === credential.id ? { ...row, revoked_at: nowSeconds() } : row,
        ),
      );
      return { previous };
    },
    onError: (error, credential, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queryKeys.credentials, context.previous);
      }
      toast.error(`Couldn't revoke ${credential.display_name}`, {
        description: errorMessage(error),
      });
    },
    onSuccess: (serverCredential) => {
      queryClient.setQueryData<Credential[]>(queryKeys.credentials, (rows) =>
        rows?.map((row) =>
          row.id === serverCredential.id ? serverCredential : row,
        ),
      );
      toast.success(`${serverCredential.display_name} revoked`, {
        description: "Revoked keys can't be restored.",
      });
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.credentials }),
  });

  const loading = appsPending || usersPending || credentialsPending;
  const appsFailed = appsIsError && !appsData;
  const joinsFailed = !appsFailed && (usersIsError || credentialsIsError);

  function retryAll() {
    void refetchApps();
    void refetchUsers();
    void refetchCredentials();
    void refetchProfiles();
  }

  const appColumns: DataTableColumn<App>[] = [
    {
      id: "app",
      header: "App",
      cell: (app) => (
        <span className="flex min-w-0 items-start gap-2">
          <EntityRef name={app.name} id={app.id} />
          <span className="mt-0.5 flex shrink-0 items-center gap-1">
            <Badge sm>{app.kind === "personal" ? "personal" : "shared"}</Badge>
            {app.disabled_at !== null && (
              <Badge sm tone="warn">
                disabled
              </Badge>
            )}
          </span>
        </span>
      ),
    },
    {
      id: "owner",
      header: "Owner",
      cell: (app) =>
        app.owner_user_id ? (
          <EntityRef
            name={userLabel(usersById.get(app.owner_user_id))}
            id={app.owner_user_id}
            idVisibility="hover"
          />
        ) : (
          <span className="text-vf-muted">Shared</span>
        ),
    },
    {
      id: "profile",
      header: "Profile",
      cell: (app) => (
        <span className="inline-flex items-center gap-1">
          <span className="min-w-0 truncate">{profileLabel(app)}</span>
          <Button
            variant="ghost"
            size="sm"
            className="text-vf-small h-6 shrink-0 px-1.5"
            onClick={() => setProfileTarget(app)}
            aria-label={`Change profile for ${app.name}`}
          >
            Change
          </Button>
        </span>
      ),
    },
    {
      id: "keys",
      header: "Keys",
      cell: (app) => {
        if (credentialsPending) return <Skeleton className="h-4 w-10" />;
        const count = (appKeysByApp.get(app.id) ?? []).filter(
          isActiveKey,
        ).length;
        return (
          <Button
            variant="ghost"
            size="sm"
            className="text-vf-small h-6 px-1.5"
            onClick={() => setKeysAppId(app.id)}
            aria-label={`View keys for ${app.name}`}
          >
            {count} {count === 1 ? "key" : "keys"}
          </Button>
        );
      },
    },
    {
      id: "created",
      header: "Created",
      value: (app) => app.created_at,
      hideOnMobile: true,
    },
  ];

  return (
    <main className="mx-auto flex w-full max-w-[1080px] flex-col gap-10 px-4 py-8 md:px-6">
      <section className="flex flex-col gap-4">
        <SectionHeader
          kicker="access"
          title="Apps & keys"
          description="Apps connect callers to the gateway; every key belongs to an app."
          action={
            <Button variant="primary" onClick={() => setCreateOpen(true)}>
              Create app
            </Button>
          }
        />

        {joinsFailed && (
          <ErrorBanner onRetry={retryAll}>
            Some details couldn&apos;t load — owner names or key counts may be
            incomplete.
          </ErrorBanner>
        )}

        {appsFailed ? (
          <ErrorBanner onRetry={retryAll}>
            Couldn&apos;t load apps. {errorMessage(appsError)}
          </ErrorBanner>
        ) : (
          <DataTable
            label="Apps"
            columns={appColumns}
            rows={apps}
            rowKey={(app) => app.id}
            loading={loading}
            empty={
              <EmptyState
                action={
                  <Button onClick={() => setCreateOpen(true)}>
                    Create app
                  </Button>
                }
              >
                No apps yet. Create one to hand out its first key.
              </EmptyState>
            }
            rowMenu={(app) => (
              <OverflowMenu label={`Actions for ${app.name}`}>
                <OverflowMenuItem
                  disabled={app.disabled_at !== null}
                  onSelect={() => setMintTarget(app)}
                >
                  Mint key…
                </OverflowMenuItem>
                <OverflowMenuItem onSelect={() => setKeysAppId(app.id)}>
                  View keys
                </OverflowMenuItem>
                <OverflowMenuItem onSelect={() => setProfileTarget(app)}>
                  Change profile…
                </OverflowMenuItem>
                <OverflowMenuSeparator />
                <OverflowMenuItem
                  danger
                  disabled={app.disabled_at !== null}
                  onSelect={() => setDisableTarget(app)}
                >
                  Disable app…
                </OverflowMenuItem>
              </OverflowMenu>
            )}
          />
        )}
      </section>

      {/* Admin automation keys — quiet subsection, same patterns. */}
      <section className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <h3 className="text-vf-heading text-vf-text">
              Admin automation keys
            </h3>
            <p className="text-vf-small text-vf-muted">
              For scripts and CI that call the admin API. Not tied to an app.
            </p>
          </div>
          <Button size="sm" onClick={() => setAdminKeyOpen(true)}>
            Create admin key
          </Button>
        </div>

        {credentialsIsError && !credentialsData ? (
          <ErrorBanner onRetry={retryAll}>
            Couldn&apos;t load admin keys. {errorMessage(credentialsError)}
          </ErrorBanner>
        ) : (
          <DataTable
            label="Admin automation keys"
            columns={adminKeyColumns}
            rows={adminKeys}
            rowKey={(credential) => credential.id}
            loading={credentialsPending}
            skeletonRows={2}
            empty={<EmptyState>No admin automation keys.</EmptyState>}
            rowMenu={(credential) => (
              <OverflowMenu label={`Actions for ${credential.display_name}`}>
                <OverflowMenuItem
                  danger
                  disabled={!isActiveKey(credential)}
                  onSelect={() => setRevokeTarget(credential)}
                >
                  Revoke key…
                </OverflowMenuItem>
              </OverflowMenu>
            )}
          />
        )}
      </section>

      {/* App keys detail panel */}
      <Sheet
        open={keysApp !== null}
        onOpenChange={(open) => {
          if (!open) setKeysAppId(null);
        }}
      >
        {keysApp && (
          <SheetContent>
            <SheetHeader>
              <SheetTitle>{keysApp.name}</SheetTitle>
              <SheetDescription>
                Keys this app uses to call the gateway.
              </SheetDescription>
            </SheetHeader>
            <AppKeysPanel
              app={keysApp}
              keys={sortKeys(appKeysByApp.get(keysApp.id) ?? [])}
              loading={credentialsPending}
              onMint={() => setMintTarget(keysApp)}
              onRevoke={setRevokeTarget}
            />
          </SheetContent>
        )}
      </Sheet>

      {/* Dialogs */}
      <CreateAppDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        users={usersData ?? []}
        profiles={profilesData ?? []}
      />
      <ChangeProfileDialog
        app={profileTarget}
        onOpenChange={(open) => {
          if (!open) setProfileTarget(null);
        }}
        profiles={profilesData ?? []}
      />
      <MintKeyDialog
        app={mintTarget}
        onOpenChange={(open) => {
          if (!open) setMintTarget(null);
        }}
        onMinted={(credential, app) => {
          setMintTarget(null);
          setRevealed({ credential, context: `for ${app.name}`, kind: "app" });
        }}
      />
      <CreateAdminKeyDialog
        open={adminKeyOpen}
        onOpenChange={setAdminKeyOpen}
        canMintOwner={sessionData?.actor.roles.includes("owner") ?? false}
        onMinted={(credential) => {
          setAdminKeyOpen(false);
          setRevealed({
            credential,
            context: `for ${credential.display_name}`,
            kind: "admin",
          });
        }}
      />
      <RevealKeyDialog revealed={revealed} onClose={() => setRevealed(null)} />

      {/* Destructive confirms (typed-name) */}
      <ConfirmDialog
        open={disableTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDisableTarget(null);
        }}
        title="Disable app?"
        description={
          disableTarget
            ? `${disableTarget.name} and all of its keys stop working immediately. There is no re-enable.`
            : undefined
        }
        confirmLabel="Disable app"
        typedName={disableTarget?.name}
        onConfirm={() => {
          // Optimistic: the cache flips immediately; errors roll back + toast.
          if (disableTarget) disableApp.mutate(disableTarget);
        }}
      />
      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
        title="Revoke key?"
        description={
          revokeTarget
            ? `Anything still using ${revokeTarget.display_name} is cut off immediately. Revoked keys can't be restored.`
            : undefined
        }
        confirmLabel="Revoke key"
        typedName={revokeTarget?.display_name}
        onConfirm={() => {
          if (revokeTarget) revokeKey.mutate(revokeTarget);
        }}
      />
    </main>
  );
}

// ---------------------------------------------------------------------------
// Admin keys table columns (module scope: no per-render identity churn)

const adminKeyColumns: DataTableColumn<Credential>[] = [
  {
    id: "key",
    header: "Key",
    cell: (credential) => (
      <EntityRef name={credential.display_name} id={credential.id} />
    ),
  },
  {
    id: "secret",
    header: "Secret",
    cell: (credential) => <SecretMask prefix={credential.prefix} />,
  },
  {
    id: "last_used",
    header: "Last used",
    cell: (credential) => (
      <RelativeTime ts={credential.last_used_at} fallback="Never" />
    ),
    hideOnMobile: true,
  },
  {
    id: "status",
    header: "Status",
    cell: (credential) => <KeyStatusBadge credential={credential} />,
  },
];

function KeyStatusBadge({ credential }: { credential: Credential }) {
  return isActiveKey(credential) ? (
    <Badge sm tone="ok">
      active
    </Badge>
  ) : (
    <Badge sm>revoked</Badge>
  );
}

// ---------------------------------------------------------------------------
// Designed error state (spec #4: error banner with retry)

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
// App keys panel (inside the detail Sheet)

function AppKeysPanel({
  app,
  keys,
  loading,
  onMint,
  onRevoke,
}: {
  app: App;
  keys: Credential[];
  loading: boolean;
  onMint: () => void;
  onRevoke: (credential: Credential) => void;
}) {
  if (loading) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  if (keys.length === 0) {
    return (
      <EmptyState
        action={
          app.disabled_at === null ? (
            <Button size="sm" onClick={onMint}>
              Mint key
            </Button>
          ) : undefined
        }
      >
        No keys yet.
      </EmptyState>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {app.disabled_at === null && (
        <div>
          <Button size="sm" onClick={onMint}>
            Mint key
          </Button>
        </div>
      )}
      <ul className="divide-vf-border/60 flex flex-col divide-y">
        {keys.map((credential) => (
          <li
            key={credential.id}
            className="flex items-start justify-between gap-3 py-3"
          >
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-vf-body text-vf-text truncate">
                {credential.display_name}
              </span>
              <SecretMask prefix={credential.prefix} />
              <span className="text-vf-micro text-vf-subtle">
                Last used{" "}
                <RelativeTime ts={credential.last_used_at} fallback="never" />
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <KeyStatusBadge credential={credential} />
              <OverflowMenu label={`Actions for ${credential.display_name}`}>
                <OverflowMenuItem
                  danger
                  disabled={!isActiveKey(credential)}
                  onSelect={() => onRevoke(credential)}
                >
                  Revoke key…
                </OverflowMenuItem>
              </OverflowMenu>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create app dialog

function CreateAppDialog({
  open,
  onOpenChange,
  users,
  profiles,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  users: User[];
  profiles: Profile[];
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open so form state starts fresh each time. */}
      {open && (
        <CreateAppDialogContent
          onOpenChange={onOpenChange}
          users={users}
          profiles={profiles}
        />
      )}
    </Dialog>
  );
}

function CreateAppDialogContent({
  onOpenChange,
  users,
  profiles,
}: {
  onOpenChange: (open: boolean) => void;
  users: User[];
  profiles: Profile[];
}) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<AppKind>("service");
  const [name, setName] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [profileId, setProfileId] = useState(FALLBACK_PROFILE_ID);
  const [fieldErrors, setFieldErrors] = useState<{
    name?: string;
    owner?: string;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: api.apps.create,
    onSuccess: async (app) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.apps });
      toast.success(`${app.name} created`, {
        description: "Mint a key to let it call the gateway.",
      });
      onOpenChange(false);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    const errors: { name?: string; owner?: string } = {};
    if (!trimmed) errors.name = "Give the app a name.";
    if (kind === "personal" && !ownerId)
      errors.owner = "Pick who this app belongs to.";
    setFieldErrors(errors);
    setFormError(null);
    if (Object.keys(errors).length > 0) return;
    create.mutate({
      name: trimmed,
      kind,
      // The fallback profile is the server default; omit it from the create.
      ...(profileId !== FALLBACK_PROFILE_ID ? { profile_id: profileId } : {}),
      ...(kind === "personal" ? { owner_user_id: ownerId } : {}),
    });
  }

  return (
    <DialogContent>
      <form onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Create app</DialogTitle>
          <DialogDescription>
            An app groups the keys for one caller — a service, a script, or a
            person&apos;s device.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <RadioCards
            value={kind}
            onValueChange={(value) => setKind(value as AppKind)}
            aria-label="App type"
          >
            <RadioCard
              value="service"
              title="Shared"
              description="Used by a service or several people."
            />
            <RadioCard
              value="personal"
              title="Personal"
              description="Belongs to one person."
            />
          </RadioCards>
          <Field label="Name" required error={fieldErrors.name}>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. brother-laptop"
              autoFocus
              disabled={create.isPending}
            />
          </Field>
          {kind === "personal" && (
            <Field label="Owner" required error={fieldErrors.owner}>
              <Select
                value={ownerId}
                onChange={(event) => setOwnerId(event.target.value)}
                disabled={create.isPending}
              >
                <option value="">Select a person…</option>
                {users.map((user) => (
                  <option key={user.id} value={user.id}>
                    {userLabel(user)}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          <Field
            label="Profile"
            help="Controls what the app can reach. You can change it later."
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
          <Button type="submit" variant="primary" loading={create.isPending}>
            Create app
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Change profile dialog (never an inline select-per-row)

function ChangeProfileDialog({
  app,
  onOpenChange,
  profiles,
}: {
  app: App | null;
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
}) {
  return (
    <Dialog open={app !== null} onOpenChange={onOpenChange}>
      {app && (
        <ChangeProfileDialogContent
          app={app}
          onOpenChange={onOpenChange}
          profiles={profiles}
        />
      )}
    </Dialog>
  );
}

function ChangeProfileDialogContent({
  app,
  onOpenChange,
  profiles,
}: {
  app: App;
  onOpenChange: (open: boolean) => void;
  profiles: Profile[];
}) {
  const queryClient = useQueryClient();
  const [profileId, setProfileId] = useState(
    app.default_profile_id ?? FALLBACK_PROFILE_ID,
  );
  const [formError, setFormError] = useState<string | null>(null);

  const update = useMutation({
    mutationFn: () =>
      api.apps.update(app.id, { default_profile_id: profileId }),
    onSuccess: async (serverApp) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.apps });
      toast.success(`${serverApp.name} now uses ${profileLabel(serverApp)}`);
      onOpenChange(false);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    update.mutate();
  }

  return (
    <DialogContent className="max-w-md">
      <form onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Change profile</DialogTitle>
          <DialogDescription>
            New keys and existing keys of {app.name} follow this profile.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field label="Profile">
            <Select
              value={profileId}
              onChange={(event) => setProfileId(event.target.value)}
              autoFocus
              disabled={update.isPending}
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
            disabled={update.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={update.isPending}>
            Save
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Mint key dialog

function MintKeyDialog({
  app,
  onOpenChange,
  onMinted,
}: {
  app: App | null;
  onOpenChange: (open: boolean) => void;
  onMinted: (credential: MintedCredential, app: App) => void;
}) {
  return (
    <Dialog open={app !== null} onOpenChange={onOpenChange}>
      {app && (
        <MintKeyDialogContent
          app={app}
          onOpenChange={onOpenChange}
          onMinted={onMinted}
        />
      )}
    </Dialog>
  );
}

function MintKeyDialogContent({
  app,
  onOpenChange,
  onMinted,
}: {
  app: App;
  onOpenChange: (open: boolean) => void;
  onMinted: (credential: MintedCredential, app: App) => void;
}) {
  const queryClient = useQueryClient();
  const [label, setLabel] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const mint = useMutation({
    mutationFn: () =>
      api.apps.mintKey(app.id, label.trim() ? { label: label.trim() } : {}),
    onSuccess: async (credential) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.credentials });
      onMinted(credential, app);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    mint.mutate();
  }

  return (
    <DialogContent className="max-w-md">
      <form onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Mint key</DialogTitle>
          <DialogDescription>
            Creates a new key for {app.name}. The full key is shown once, right
            after minting.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field
            label="Label"
            help="Shown in lists so you can tell keys apart. Not secret."
          >
            <Input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="e.g. laptop, CI"
              autoFocus
              disabled={mint.isPending}
            />
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
            disabled={mint.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={mint.isPending}>
            Mint key
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Create admin key dialog

function CreateAdminKeyDialog({
  open,
  onOpenChange,
  canMintOwner,
  onMinted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  canMintOwner: boolean;
  onMinted: (credential: MintedCredential) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <CreateAdminKeyDialogContent
          onOpenChange={onOpenChange}
          canMintOwner={canMintOwner}
          onMinted={onMinted}
        />
      )}
    </Dialog>
  );
}

function CreateAdminKeyDialogContent({
  onOpenChange,
  canMintOwner,
  onMinted,
}: {
  onOpenChange: (open: boolean) => void;
  canMintOwner: boolean;
  onMinted: (credential: MintedCredential) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [role, setRole] = useState<"admin" | "owner">("admin");
  const [nameError, setNameError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => api.adminKeys.create({ name: name.trim(), role }),
    onSuccess: async (credential) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.credentials });
      onMinted(credential);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!name.trim()) {
      setNameError("Give the key a name.");
      return;
    }
    setNameError(null);
    create.mutate();
  }

  return (
    <DialogContent>
      <form onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Create admin key</DialogTitle>
          <DialogDescription>
            For automation that manages the gateway itself. The full key is
            shown once, right after creating.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field label="Name" required error={nameError}>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. deploy-bot"
              autoFocus
              disabled={create.isPending}
            />
          </Field>
          <RadioCards
            value={role}
            onValueChange={(value) => setRole(value as "admin" | "owner")}
            aria-label="Role"
          >
            <RadioCard
              value="admin"
              title="Admin"
              description="Manage apps, keys, and profiles."
            />
            <RadioCard
              value="owner"
              title="Owner"
              description={
                canMintOwner
                  ? "Everything, including other admin keys."
                  : "Only owners can mint owner keys."
              }
              disabled={!canMintOwner}
            />
          </RadioCards>
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
          <Button type="submit" variant="primary" loading={create.isPending}>
            Create key
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Reveal-once key dialog (mint result). The secret lives only in this
// dialog's props; closing it discards the value — masked-after-close, never
// logged, never refetchable.

function RevealKeyDialog({
  revealed,
  onClose,
}: {
  revealed: RevealedKey | null;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={revealed !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      {revealed && (
        <RevealKeyDialogContent revealed={revealed} onClose={onClose} />
      )}
    </Dialog>
  );
}

function RevealKeyDialogContent({
  revealed,
  onClose,
}: {
  revealed: RevealedKey;
  onClose: () => void;
}) {
  const { credential, context, kind } = revealed;
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  // Honest, runnable check — same pattern as the portal's reveal flow.
  const curl =
    kind === "admin"
      ? `curl ${origin}/admin/api/overview \\\n  -H "Authorization: Bearer ${credential.api_key}"`
      : `curl ${origin}/v1/models \\\n  -H "Authorization: Bearer ${credential.api_key}"`;

  // No accidental dismissal: the key is gone after close, so closing is the
  // explicit "Done" action only.
  const block = (event: Event) => event.preventDefault();

  return (
    <DialogContent
      hideClose
      onEscapeKeyDown={block}
      onPointerDownOutside={block}
      onInteractOutside={block}
    >
      <DialogHeader>
        <DialogTitle>Save your new key</DialogTitle>
        <DialogDescription>
          The key {context} is shown only this once. Copy it somewhere safe —
          after you close this it can&apos;t be retrieved.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        <CopyBlock label="Your key" value={credential.api_key} />
        <CopyBlock label="Run this" value={curl} />
      </DialogBody>
      <DialogFooter>
        <Button variant="primary" onClick={onClose}>
          Done — I saved it
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

/** Titled mono block with its own copy button (reveal dialog only). */
function CopyBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
          {label}
        </span>
        <CopyButton value={value} aria-label={`Copy ${label.toLowerCase()}`} />
      </div>
      <pre className="rounded-vf-md border-vf-border bg-vf-surface-2 text-vf-micro text-vf-text border p-3 font-mono break-all whitespace-pre-wrap">
        {value}
      </pre>
    </div>
  );
}

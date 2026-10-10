"use client";

import { Suspense, useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AppWindow, MoreHorizontal, Plus, KeyRound } from "lucide-react";
import { toast } from "sonner";
import { formatAppName } from "@/lib/format";
import {
  accountApi,
  type AccountApp,
  type AccountSnapshot,
} from "@/lib/account-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton, SkeletonRows } from "@/components/ui/skeleton";
import { ListCard, ListRow } from "@/components/product/list-card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  PageHeader,
  EmptyState,
  ErrorState,
  RelativeTime,
  SecretReveal,
  CopyField,
  CodeBlock,
} from "@/components/product";
import { preferredTerms } from "@/lib/terms";

const accountKey = ["account", "session"];
const CLIENTS = [
  "Other app",
  "Open WebUI",
  "LibreChat",
  "SillyTavern",
  "Cursor",
  "Python",
  "JavaScript",
  "curl",
];
type Reveal = { secret: string; app: string; client: string; oldIds: string[] };
type KeyAction = { kind: "rotate" | "revoke" | "remove"; app: AccountApp };

function accessPlanSummary(app: AccountApp): string {
  return app.profile?.name ?? "";
}

function AppsContents() {
  const searchParams = useSearchParams();
  const requestedApp = searchParams.get("app");
  const createFromCommand = searchParams.get("action") === "create-key";
  const commandConsumed = useRef(false);
  const queryClient = useQueryClient();
  const {
    data: accountData,
    isPending: accountPending,
    isError: accountError,
    refetch: refetchAccount,
  } = useQuery({
    queryKey: accountKey,
    queryFn: accountApi.session,
  });
  const { data: discoveryData } = useQuery({
    queryKey: ["gateway", "discovery"],
    queryFn: accountApi.discovery,
  });
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [client, setClient] = useState(CLIENTS[0]);
  const [createdApp, setCreatedApp] = useState<AccountApp | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reveal, setReveal] = useState<Reveal | null>(null);
  const [action, setAction] = useState<KeyAction | null>(null);
  const [saved, setSaved] = useState(false);
  const refresh = () => queryClient.invalidateQueries({ queryKey: accountKey });
  const baseUrl =
    discoveryData?.recipes.openai?.canonical_base_url ??
    (typeof window === "undefined"
      ? ""
      : `${window.location.origin}/openai/v1`);

  useEffect(() => {
    if (
      createFromCommand &&
      accountData?.authenticated &&
      !commandConsumed.current
    ) {
      commandConsumed.current = true;
      setCreating(true);
    }
  }, [createFromCommand, accountData?.authenticated]);

  useEffect(() => {
    if (!requestedApp || !accountData) return;
    document.getElementById(`app-${requestedApp}`)?.focus();
  }, [requestedApp, accountData]);

  function resetDraft() {
    setName("");
    setClient(CLIENTS[0]);
    setCreatedApp(null);
    setError(null);
  }

  function startNewApp() {
    resetDraft();
    setCreating(true);
  }

  function closeCreate() {
    setCreating(false);
    resetDraft();
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const app = createdApp ?? (await accountApi.createApp(name.trim()));
      setCreatedApp(app);
      if (!createdApp) {
        queryClient.setQueryData<AccountSnapshot>(accountKey, (current) =>
          current
            ? {
                ...current,
                apps: [
                  ...current.apps.filter((item) => item.id !== app.id),
                  app,
                ],
              }
            : current,
        );
        void refresh();
      }
      const appName = formatAppName(app.name, accountData?.user?.email);
      const key = await accountApi.mintKey(
        app.id,
        client === "Other app" ? appName : client,
      );
      setReveal({ secret: key.api_key, app: appName, client, oldIds: [] });
      setSaved(false);
      setCreating(false);
      setCreatedApp(null);
      setName("");
      await refresh();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not create the app. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function performAction() {
    if (!action) return;
    setBusy(true);
    setError(null);
    try {
      const ids = action.app.credentials.map((key) => key.id);
      const appName = formatAppName(action.app.name, accountData?.user?.email);
      if (action.kind === "rotate") {
        const key = await accountApi.mintKey(
          action.app.id,
          `${appName} replacement`,
        );
        setReveal({
          secret: key.api_key,
          app: appName,
          client: "Other app",
          oldIds: ids,
        });
        setSaved(false);
      } else if (action.kind === "revoke") {
        for (const id of ids) {
          await accountApi.revokeKey(id);
          setAction((current) =>
            current
              ? {
                  ...current,
                  app: {
                    ...current.app,
                    credentials: current.app.credentials.filter(
                      (key) => key.id !== id,
                    ),
                  },
                }
              : null,
          );
        }
        toast.success("App keys revoked");
      } else {
        await accountApi.deleteApp(action.app.id);
        toast.success("App removed");
      }
      setAction(null);
      await refresh();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not update these keys. Try again.",
      );
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function saveKey() {
    if (!reveal) return;
    setSaved(true);
    setReveal({ ...reveal, secret: "" });
    setBusy(true);
    setError(null);
    try {
      for (const id of reveal.oldIds) {
        await accountApi.revokeKey(id);
        setReveal((current) =>
          current
            ? {
                ...current,
                oldIds: current.oldIds.filter((oldId) => oldId !== id),
              }
            : null,
        );
      }
      toast.success(reveal.oldIds.length ? "Key rotated" : "Key saved");
      setReveal(null);
      await refresh();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "The new key is saved, but an old key could not be revoked. Try again.",
      );
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  const apps = accountData?.apps ?? [];
  const actionAppName = action
    ? formatAppName(action.app.name, accountData?.user?.email)
    : "";
  const actionDescription = action
    ? [
        accessPlanSummary(action.app),
        action.kind === "rotate"
          ? "Save the replacement key first. Old keys stop after you confirm."
          : action.kind === "remove"
            ? "This removes the app and its keys."
            : "Apps using these keys will stop connecting.",
      ]
        .filter(Boolean)
        .join(". ")
    : "";
  return (
    <div className="space-y-8">
      <PageHeader
        title="Apps & keys"
        subtitle="Apps that use your gateway"
        primaryAction={
          <Button onClick={startNewApp} disabled={!accountData?.authenticated}>
            <Plus aria-hidden="true" />
            New app
          </Button>
        }
      />
      {accountPending ? (
        <div
          role="status"
          aria-label="Loading apps"
          aria-busy="true"
          className="bg-card rounded-panel border"
        >
          <SkeletonRows count={3} lines={2} />
        </div>
      ) : accountError ? (
        <ErrorState
          message="Your apps could not be loaded. Try again."
          onRetry={() => void refetchAccount()}
        />
      ) : !accountData?.authenticated ? (
        <EmptyState
          title="Sign in to connect your apps"
          description="Each app gets a key with your account's access plan."
          action={
            <Button asChild>
              <a href="/admin/login">Sign in</a>
            </Button>
          }
        />
      ) : !apps.length ? (
        <EmptyState
          icon={KeyRound}
          title="Connect your first app"
          description="Create a key for an app you already use."
          action={<Button onClick={startNewApp}>New app</Button>}
        />
      ) : (
        // The list template: one ListCard of 60 px rows (64 on a phone).
        <ListCard label="Apps">
          {apps.map((app) => {
            const appName = formatAppName(app.name, accountData?.user?.email);
            const lastUsed = app.credentials.reduce<number | null>(
              (latest, key) =>
                key.last_used_at != null &&
                (latest === null || key.last_used_at > latest)
                  ? key.last_used_at
                  : latest,
              null,
            );
            return (
              <ListRow
                key={app.id}
                id={`app-${app.id}`}
                tabIndex={requestedApp === app.id ? -1 : undefined}
                className={
                  requestedApp === app.id
                    ? "ring-ring ring-offset-background ring-2 ring-offset-2"
                    : undefined
                }
                lines={2}
                titleAs="h2"
                leading={<AppWindow aria-hidden="true" />}
                title={appName}
                meta={[
                  `${app.credentials.length} ${app.credentials.length === 1 ? "key" : "keys"}`,
                  accessPlanSummary(app) || null,
                ]}
                facts={[
                  lastUsed !== null ? (
                    <span key="used">
                      Last used <RelativeTime ts={lastUsed} />
                    </span>
                  ) : null,
                ]}
                trailing={
                  <>
                    {!app.credentials.length && (
                      <Button
                        variant="outline"
                        disabled={!app.can_mint_keys || busy}
                        onClick={() => {
                          resetDraft();
                          setCreatedApp(app);
                          setName(app.name);
                          setCreating(true);
                        }}
                      >
                        Create key
                      </Button>
                    )}
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Actions for ${appName}`}
                        >
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          disabled={!app.can_mint_keys || busy}
                          onSelect={async () => {
                            setBusy(true);
                            setError(null);
                            try {
                              const key = await accountApi.mintKey(
                                app.id,
                                appName,
                              );
                              setSaved(false);
                              setReveal({
                                secret: key.api_key,
                                app: appName,
                                client: "Other app",
                                oldIds: [],
                              });
                              await refresh();
                            } catch (err) {
                              toast.error(
                                err instanceof Error
                                  ? err.message
                                  : "Could not create key",
                              );
                            } finally {
                              setBusy(false);
                            }
                          }}
                        >
                          {app.credentials.length
                            ? "Create another key"
                            : "Create key"}
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          disabled={
                            !app.credentials.length || !app.can_mint_keys
                          }
                          onSelect={() => {
                            setAction({ kind: "rotate", app });
                            setError(null);
                          }}
                        >
                          Rotate keys
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          disabled={!app.credentials.length}
                          onSelect={() => {
                            setAction({ kind: "revoke", app });
                            setError(null);
                          }}
                        >
                          Revoke keys
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          variant="destructive"
                          disabled={busy}
                          onSelect={() => {
                            setAction({ kind: "remove", app });
                            setError(null);
                          }}
                        >
                          Remove app
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </>
                }
              />
            );
          })}
        </ListCard>
      )}
      <Dialog
        open={creating}
        onOpenChange={(open) => {
          if (!open && !busy) closeCreate();
        }}
      >
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{createdApp ? "Create key" : "New app"}</DialogTitle>
            <DialogDescription>
              Create access for an existing app; its key appears once.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={create} className="space-y-5">
            <div className="space-y-2">
              <label htmlFor="app-name" className="text-label">
                App name
              </label>
              <Input
                id="app-name"
                autoFocus
                required
                maxLength={120}
                placeholder="My laptop"
                value={name}
                disabled={!!createdApp || busy}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="app-client" className="text-label">
                App recipe
              </label>
              <Select value={client} onValueChange={setClient}>
                <SelectTrigger id="app-client" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CLIENTS.map((value) => (
                    <SelectItem key={value} value={value}>
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {error && (
              <p role="alert" className="text-destructive text-meta">
                {createdApp
                  ? "Your app was created, but its key could not be created. Retry to finish. "
                  : ""}
                {error}
              </p>
            )}
            <DialogFooter>
              <Button
                variant="outline"
                type="button"
                disabled={busy}
                onClick={closeCreate}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={busy || !name.trim()}>
                {busy
                  ? "Creating…"
                  : createdApp
                    ? error
                      ? "Retry key creation"
                      : "Create key"
                    : "Create app and key"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog
        open={action !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setAction(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {action?.kind === "remove"
                ? "Remove"
                : action?.kind === "rotate"
                  ? "Rotate"
                  : "Revoke"}{" "}
              {action?.kind === "remove"
                ? actionAppName
                : `keys for ${actionAppName}`}
              ?
            </DialogTitle>
            <DialogDescription>{actionDescription}</DialogDescription>
          </DialogHeader>
          {error && (
            <p role="alert" className="text-destructive text-meta">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setAction(null)}
            >
              Cancel
            </Button>
            <Button
              variant={
                action?.kind === "revoke" || action?.kind === "remove"
                  ? "destructive"
                  : "default"
              }
              disabled={busy}
              onClick={() => void performAction()}
            >
              {busy
                ? "Working…"
                : action?.kind === "rotate"
                  ? "Create replacement key"
                  : action?.kind === "remove"
                    ? "Remove app"
                    : "Revoke keys"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={reveal !== null} onOpenChange={() => {}}>
        <DialogContent
          showCloseButton={false}
          className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
          onEscapeKeyDown={(event) => event.preventDefault()}
          onPointerDownOutside={(event) => event.preventDefault()}
        >
          <DialogHeader>
            <DialogTitle>Connect {reveal?.app}</DialogTitle>
            <DialogDescription>
              Save this key; you cannot view it again.
            </DialogDescription>
          </DialogHeader>
          {reveal && (
            <div className="space-y-5">
              {!saved && (
                <SecretReveal
                  secret={reveal.secret}
                  onSaved={() => void saveKey()}
                />
              )}
              <CopyField
                label={preferredTerms.connectionAddress}
                value={baseUrl}
              />
              <p className="text-muted-foreground text-meta">
                {reveal.client === "Other app"
                  ? "Choose your app's OpenAI-compatible provider"
                  : `Use ${reveal.client} with the OpenAI-compatible API`}
                , paste this gateway address, and enter your saved key.
              </p>
              <details className="text-meta">
                <summary className="cursor-pointer font-medium">
                  Connection examples
                </summary>
                <div className="mt-3">
                  <CodeBlock
                    snippets={[
                      {
                        language: "curl",
                        code: `curl '${baseUrl}/models' \\\n  -H "Authorization: Bearer $GATEWAY_API_KEY"`,
                      },
                      {
                        language: "Python",
                        code: `import os
from openai import OpenAI

client = OpenAI(
    base_url="${baseUrl}",
    api_key=os.environ["GATEWAY_API_KEY"],
)
print(client.models.list())`,
                      },
                      {
                        language: "JavaScript",
                        code: `import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${baseUrl}",
  apiKey: process.env.GATEWAY_API_KEY,
});
console.log(await client.models.list());`,
                      },
                    ].sort(
                      (first, second) =>
                        Number(
                          second.language.toLowerCase() ===
                            reveal.client.toLowerCase(),
                        ) -
                        Number(
                          first.language.toLowerCase() ===
                            reveal.client.toLowerCase(),
                        ),
                    )}
                  />
                </div>
              </details>
              {error && (
                <div role="alert">
                  <p className="text-destructive text-meta">{error}</p>
                  <Button
                    className="mt-3"
                    size="md"
                    disabled={busy}
                    onClick={() => void saveKey()}
                  >
                    Retry revoking old keys
                  </Button>
                </div>
              )}
              {saved && busy && (
                <p role="status" className="text-muted-foreground text-meta">
                  Revoking previous keys…
                </p>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function AppsPage() {
  return (
    <Suspense fallback={<Skeleton className="h-24 w-full" />}>
      <AppsContents />
    </Suspense>
  );
}

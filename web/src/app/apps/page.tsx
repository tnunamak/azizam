"use client";

import {
  Suspense,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type MouseEvent,
} from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AppWindow, MoreHorizontal, Plus, KeyRound } from "lucide-react";
import { toast } from "@/lib/toast";
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
import { Spinner } from "@/components/ui/spinner";
import { ListCard, ListRow } from "@/components/product/list-card";
import { SignInConnections } from "@/components/product/sign-in-connections";
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
  CopyField,
} from "@/components/product";
import { cn } from "@/lib/cn";
import { keyPromptNote } from "@/lib/prompt-memory";
import { api, type AccessPlan } from "@/lib/api";
import { productApi } from "@/lib/product-api";
import { keyApps, suggestedModel } from "@/lib/app-guides";
import { useViewer } from "@/components/shell/session";
import { AppGuide } from "@/components/product/app-guide";
import { TrustedAddressesDialog } from "./trusted-addresses";

const accountKey = ["account", "session"];
/** "person": the app uses its person's own plan ("Same as you"). */
const SAME_AS_PERSON = "person";
type Reveal = {
  secret: string;
  appId: string;
  app: string;
  oldIds: string[];
  /** Whether prompts sent with the key are remembered, and for whom. */
  note?: string | null;
};
type KeyAction = { kind: "rotate" | "revoke" | "remove"; app: AccountApp };

/** The guide an app's name points at ("OpenWhispr laptop" → OpenWhispr). */
function guideForName(name: string): string {
  const lower = name.toLowerCase();
  return (
    keyApps.find((app) => lower.includes(app.name.toLowerCase()))?.id ??
    keyApps[0].id
  );
}

/** What an app may use, in one sentence: 'Access plan "Local only": Chat and Images. No paid models.' */
function planSentence(plan: { name: string; summary: string | null } | null | undefined): string {
  if (!plan) return "No access plan yet, so it can't use anything. Ask the owner to choose one.";
  return plan.summary
    ? `Access plan “${plan.name}”: ${plan.summary}`
    : `Access plan “${plan.name}”.`;
}

/** Under an "Access plan" label: "Local only. Chat and Images. No paid models." */
function planDescription(plan: { name: string; summary: string | null } | null | undefined): string {
  if (!plan) return "No access plan yet, so it can't use anything. Ask the owner to choose one.";
  return plan.summary ? `${plan.name}. ${plan.summary}` : plan.name;
}

/** "Family (yours)" or the app's own plan name (design §3.4). */
function accessPlanSummary(app: AccountApp): string {
  const plan = app.access_plan;
  if (!plan) return "";
  return plan.same_as_person ? `${plan.name} (yours)` : plan.name;
}

/** Why this app cannot get a key. The API sends only the boolean, so the
 * reason comes from the fields that set it (gateway/user_runtime.py). */
function mintBlockedReason(app: AccountApp): string {
  if (app.disabled_at) return "This app is turned off, so it can't get keys.";
  if (!app.access_plan)
    return "This app has no access plan. Ask the owner to choose one.";
  return "You can't create keys for this app. Ask the owner to update its access.";
}

// A rotation whose old keys could not all be revoked. The new key is already
// saved, so the member may finish later; the old key ids (never secrets) are
// kept until revoked, across reloads.
const unfinishedRotationsKey = "azizam:unfinished-key-rotations";
type UnfinishedRotations = Record<string, string[]>;

function readUnfinishedRotations(): UnfinishedRotations {
  if (typeof window === "undefined") return {};
  try {
    const value: unknown = JSON.parse(
      window.localStorage.getItem(unfinishedRotationsKey) ?? "{}",
    );
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    // Stored text is untrusted: keep only string ids under each app.
    const clean: UnfinishedRotations = {};
    for (const [appId, ids] of Object.entries(value)) {
      if (!Array.isArray(ids)) continue;
      const strings = ids.filter((id): id is string => typeof id === "string");
      if (strings.length) clean[appId] = strings;
    }
    return clean;
  } catch {
    return {};
  }
}

/** Returns false when the browser refused the write (blocked or full). */
function writeUnfinishedRotations(value: UnfinishedRotations): boolean {
  try {
    const entries = Object.entries(value).filter(([, ids]) => ids.length);
    if (entries.length)
      window.localStorage.setItem(
        unfinishedRotationsKey,
        JSON.stringify(Object.fromEntries(entries)),
      );
    else window.localStorage.removeItem(unfinishedRotationsKey);
    return true;
  } catch {
    return false;
  }
}

function AppsContents() {
  const searchParams = useSearchParams();
  const requestedApp = searchParams.get("app");
  const createFromCommand = searchParams.get("action") === "create-key";
  const commandConsumed = useRef(false);
  const focusedRequest = useRef<string | null>(null);
  const dialogReturnFocus = useRef<HTMLElement | null>(null);
  const dialogReturnApp = useRef<string | null>(null);
  const newAppButton = useRef<HTMLButtonElement>(null);
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
  const viewer = useViewer();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [plan, setPlan] = useState(SAME_AS_PERSON);
  const [guideApp, setGuideApp] = useState(keyApps[0].id);
  const [accessApp, setAccessApp] = useState<AccountApp | null>(null);
  const [accessPlan, setAccessPlan] = useState(SAME_AS_PERSON);
  const [createdApp, setCreatedApp] = useState<AccountApp | null>(null);
  // True only when this dialog created the app, so a key failure on an
  // existing app does not claim the app was just created.
  const [appIsNew, setAppIsNew] = useState(false);
  const [mintingAppId, setMintingAppId] = useState<string | null>(null);
  const [unfinished, setUnfinished] = useState<UnfinishedRotations>({});
  // Latest value, so updates stay pure and the write happens outside React.
  const unfinishedRef = useRef<UnfinishedRotations>({});
  const [unsavedReminder, setUnsavedReminder] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reveal, setReveal] = useState<Reveal | null>(null);
  const [action, setAction] = useState<KeyAction | null>(null);
  const [trustingApp, setTrustingApp] = useState<AccountApp | null>(null);
  const [saved, setSaved] = useState(false);
  const refresh = () => queryClient.invalidateQueries({ queryKey: accountKey });
  // Only the owner and admins may give an app a different plan.
  const { data: plans } = useQuery({
    queryKey: ["access-plans"],
    queryFn: api.accessPlans.list,
    enabled: viewer.admin && (creating || accessApp !== null),
  });
  // A guide fills in a real model name; the catalog is already scoped to
  // what this person may use.
  const { data: catalog, isPending: catalogPending } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
    enabled: reveal !== null,
  });
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const baseUrl =
    discoveryData?.recipes?.openai?.canonical_base_url ?? `${origin}/openai/v1`;
  const catalogRows = catalog?.aliases?.length ? catalog.aliases : (catalog?.models ?? []);
  const guideValues = {
    openai_url: baseUrl,
    anthropic_url:
      discoveryData?.recipes?.anthropic?.canonical_base_url ?? `${origin}/anthropic`,
    mcp_url: discoveryData?.mcp?.url ?? `${origin}/mcp`,
    chat_model: suggestedModel(catalogRows, "chat"),
    transcription_model: suggestedModel(catalogRows, "transcribe"),
  };
  const personPlan = accountData?.user?.access_plan ?? null;
  const chosenPlan = (plans ?? []).find((item) => item.id === plan);

  useEffect(() => {
    if (
      createFromCommand &&
      accountData?.authenticated &&
      !commandConsumed.current
    ) {
      commandConsumed.current = true;
      dialogReturnFocus.current = newAppButton.current;
      setCreating(true);
    }
  }, [createFromCommand, accountData?.authenticated]);

  useEffect(() => {
    const stored = readUnfinishedRotations();
    unfinishedRef.current = stored;
    setUnfinished(stored);
  }, []);

  // The in-memory record always updates; a refused write only means the
  // reminder will not survive a reload, which the page then says.
  function updateUnfinished(
    change: (current: UnfinishedRotations) => UnfinishedRotations,
  ) {
    const next = change(unfinishedRef.current);
    unfinishedRef.current = next;
    setUnfinished(next);
    setUnsavedReminder(!writeUnfinishedRotations(next));
  }

  useEffect(() => {
    if (!requestedApp) {
      focusedRequest.current = null;
      return;
    }
    if (
      !accountData ||
      creating ||
      action ||
      reveal ||
      dialogReturnFocus.current ||
      focusedRequest.current === requestedApp
    )
      return;
    const target = document.getElementById(`app-${requestedApp}`);
    if (target) {
      focusedRequest.current = requestedApp;
      target.focus();
    }
  }, [requestedApp, accountData, creating, action, reveal]);

  function rememberAppFocus(appId: string, opener?: HTMLElement) {
    dialogReturnFocus.current =
      opener ?? document.getElementById(`app-actions-${appId}`);
    dialogReturnApp.current = appId;
  }

  function restoreDialogFocus(event: Event) {
    event.preventDefault();
    // A create/rotate dialog may be handing focus to the key reveal.
    if (
      creating ||
      action ||
      reveal ||
      (!dialogReturnFocus.current && !dialogReturnApp.current)
    )
      return;
    const requestedTarget =
      requestedApp && focusedRequest.current !== requestedApp
        ? document.getElementById(`app-${requestedApp}`)
        : null;
    const appTarget = dialogReturnApp.current
      ? document.getElementById(`app-actions-${dialogReturnApp.current}`)
      : null;
    const target = [
      requestedTarget,
      dialogReturnFocus.current,
      appTarget,
      newAppButton.current,
    ].find(
      (element) =>
        element?.isConnected &&
        !element.matches(":disabled, [aria-disabled=true]"),
    );
    dialogReturnFocus.current = null;
    dialogReturnApp.current = null;
    if (target === requestedTarget) focusedRequest.current = requestedApp;
    target?.focus();
  }

  function resetDraft() {
    setName("");
    setPlan(SAME_AS_PERSON);
    setCreatedApp(null);
    setAppIsNew(false);
    setError(null);
  }

  function startNewApp(event: MouseEvent<HTMLButtonElement>) {
    dialogReturnFocus.current = event.currentTarget;
    dialogReturnApp.current = null;
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
        setAppIsNew(true);
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
      // The plan is set before any key exists, so a key never works with
      // more access than the owner chose.
      if (
        viewer.admin &&
        plan !== SAME_AS_PERSON &&
        !(app.access_plan && !app.access_plan.same_as_person && app.access_plan.id === plan)
      ) {
        await api.apps.setAccessPlan(app.id, plan);
        const chosen = (plans ?? []).find((item) => item.id === plan);
        setCreatedApp({
          ...app,
          access_plan: chosen
            ? { id: chosen.id, name: chosen.name, summary: chosen.summary, same_as_person: false }
            : app.access_plan,
        });
      }
      const appName = formatAppName(app.name, accountData?.user?.email);
      const key = await accountApi.mintKey(app.id, appName);
      setGuideApp(guideForName(app.name));
      setReveal({
        secret: key.api_key,
        appId: app.id,
        app: appName,
        oldIds: [],
        note: keyPromptNote(key.prompt_memory),
      });
      setSaved(false);
      setCreating(false);
      setCreatedApp(null);
      setAppIsNew(false);
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

  async function changeAccess() {
    if (!accessApp) return;
    setBusy(true);
    setError(null);
    try {
      await api.apps.setAccessPlan(accessApp.id, accessPlan);
      toast.success("Access updated");
      setAccessApp(null);
      await refresh();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Access could not be changed. Try again.",
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
        setGuideApp(guideForName(action.app.name));
        setReveal({
          secret: key.api_key,
          appId: action.app.id,
          app: appName,
          oldIds: ids,
          note: keyPromptNote(key.prompt_memory),
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
        dialogReturnFocus.current = newAppButton.current;
        dialogReturnApp.current = null;
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
      // Record the old key ids (never the secret) before the first revoke,
      // so a reload or a failed revoke cannot lose them.
      const { appId } = reveal;
      if (reveal.oldIds.length)
        updateUnfinished((current) => ({ ...current, [appId]: reveal.oldIds }));
      for (const id of reveal.oldIds) {
        await accountApi.revokeKey(id);
        updateUnfinished((current) => ({
          ...current,
          [appId]: (current[appId] ?? []).filter((oldId) => oldId !== id),
        }));
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
      setError(err instanceof Error ? err.message : null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  // Close the key dialog with old keys still active; the app row keeps a
  // notice and a "Revoke old keys" action until they are gone.
  function finishLater() {
    if (!reveal) return;
    const { appId, oldIds } = reveal;
    updateUnfinished((current) => ({ ...current, [appId]: oldIds }));
    setReveal(null);
    setError(null);
  }

  async function revokeUnfinished(appId: string, ids: string[]) {
    setBusy(true);
    try {
      for (const id of ids) {
        await accountApi.revokeKey(id);
        updateUnfinished((current) => ({
          ...current,
          [appId]: (current[appId] ?? []).filter((oldId) => oldId !== id),
        }));
      }
      toast.success("Old keys revoked");
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Old keys could not be revoked. Try again.",
      );
    } finally {
      await refresh();
      setBusy(false);
    }
  }

  async function mintFromMenu(app: AccountApp, appName: string) {
    setBusy(true);
    setMintingAppId(app.id);
    setError(null);
    try {
      const key = await accountApi.mintKey(app.id, appName);
      rememberAppFocus(app.id);
      setSaved(false);
      setGuideApp(guideForName(app.name));
      setReveal({
        secret: key.api_key,
        appId: app.id,
        app: appName,
        oldIds: [],
        note: keyPromptNote(key.prompt_memory),
      });
      await refresh();
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "The key could not be created. Try again.",
      );
    } finally {
      setMintingAppId(null);
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
        action.kind === "rotate" &&
        action.app.kind === "personal" &&
        action.app.credentials.some((key) => key.created_by === null)
          ? "A new key saves your prompts"
          : null,
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
          <Button
            ref={newAppButton}
            onClick={startNewApp}
            disabled={!accountData?.authenticated}
          >
            <Plus aria-hidden="true" />
            New app
          </Button>
        }
      />
      {unsavedReminder && (
        <p role="status" className="text-destructive text-meta">
          This browser could not save the reminder about old keys that still
          work. It is lost if you reload, so revoke them before you leave.
        </p>
      )}
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
              <a href="/login">Sign in</a>
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
            const activeIds = new Set(app.credentials.map((key) => key.id));
            // Old keys from a rotation finished later, still active.
            const oldKeys = (unfinished[app.id] ?? []).filter((id) =>
              activeIds.has(id),
            );
            // Keys with no recorded creator still work but cannot save the
            // member's prompts; only a personal app's key acts as its member.
            const needsNewKey =
              app.kind === "personal" &&
              app.credentials.some((key) => key.created_by === null);
            const blockedReason = app.can_mint_keys
              ? null
              : mintBlockedReason(app);
            const minting = mintingAppId === app.id;
            // A notice must stay whole on a phone, where there is no hover:
            // its meta line wraps and a row action moves under it.
            const hasAction =
              oldKeys.length > 0 || (needsNewKey && app.can_mint_keys);
            const hasNotice = hasAction || blockedReason !== null;
            return (
              <ListRow
                key={app.id}
                id={`app-${app.id}`}
                tabIndex={requestedApp === app.id ? -1 : undefined}
                className={cn(
                  requestedApp === app.id &&
                    "ring-ring ring-offset-background ring-2 ring-offset-2",
                  hasNotice &&
                    "max-sm:py-3 max-sm:[&_[data-slot=list-row-meta]]:overflow-visible max-sm:[&_[data-slot=list-row-meta]]:whitespace-normal",
                  hasAction &&
                    "max-sm:flex-wrap max-sm:[&_[data-slot=list-row-trailing]]:w-full max-sm:[&_[data-slot=list-row-trailing]]:pl-7",
                )}
                lines={2}
                titleAs="h2"
                leading={<AppWindow aria-hidden="true" />}
                title={appName}
                // A notice leads the line, so a phone's trailing action
                // cuts the key count, not the notice.
                meta={[
                  oldKeys.length
                    ? "Old keys still work"
                    : needsNewKey && app.can_mint_keys
                      ? "Needs a new key"
                      : null,
                  blockedReason ? "Can't create keys" : null,
                  `${app.credentials.length} ${app.credentials.length === 1 ? "key" : "keys"}`,
                  accessPlanSummary(app)
                    ? `Access plan: ${accessPlanSummary(app)}`
                    : null,
                ]}
                metaTitle={
                  oldKeys.length
                    ? "Your new key is saved. Some old keys still work."
                    : needsNewKey && app.can_mint_keys
                      ? "This app's key doesn't save your prompts. Replace it to use your prompt settings."
                      : (blockedReason ?? undefined)
                }
                facts={[
                  lastUsed !== null ? (
                    <span key="used">
                      Last used <RelativeTime ts={lastUsed} />
                    </span>
                  ) : null,
                ]}
                trailing={
                  <>
                    {minting ? (
                      <span
                        role="status"
                        className="text-muted-foreground text-meta inline-flex items-center gap-2"
                      >
                        <Spinner />
                        Creating key…
                      </span>
                    ) : oldKeys.length ? (
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={() => void revokeUnfinished(app.id, oldKeys)}
                      >
                        Revoke old keys
                      </Button>
                    ) : !app.credentials.length && app.can_mint_keys ? (
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={(event) => {
                          rememberAppFocus(app.id, event.currentTarget);
                          resetDraft();
                          setCreatedApp(app);
                          setName(app.name);
                          setCreating(true);
                        }}
                      >
                        Create key
                      </Button>
                    ) : needsNewKey && app.can_mint_keys ? (
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={(event) => {
                          rememberAppFocus(app.id, event.currentTarget);
                          setAction({ kind: "rotate", app });
                          setError(null);
                        }}
                      >
                        Replace key
                      </Button>
                    ) : null}
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          id={`app-actions-${app.id}`}
                          aria-label={`Actions for ${appName}`}
                        >
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {blockedReason && (
                          <p className="text-muted-foreground text-meta max-w-64 px-2 py-1.5">
                            {blockedReason}
                          </p>
                        )}
                        <DropdownMenuItem
                          disabled={!app.can_mint_keys || busy}
                          onSelect={() => void mintFromMenu(app, appName)}
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
                            rememberAppFocus(app.id);
                            setAction({ kind: "rotate", app });
                            setError(null);
                          }}
                        >
                          Rotate keys
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          disabled={!app.credentials.length}
                          onSelect={() => {
                            rememberAppFocus(app.id);
                            setAction({ kind: "revoke", app });
                            setError(null);
                          }}
                        >
                          Revoke keys
                        </DropdownMenuItem>
                        {viewer.admin && (
                          <DropdownMenuItem
                            onSelect={() => {
                              rememberAppFocus(app.id);
                              setError(null);
                              setAccessPlan(
                                !app.access_plan || app.access_plan.same_as_person
                                  ? SAME_AS_PERSON
                                  : app.access_plan.id,
                              );
                              setAccessApp(app);
                            }}
                          >
                            Change access plan
                          </DropdownMenuItem>
                        )}
                        {accountData?.user?.role === "owner" && (
                          <DropdownMenuItem
                            onSelect={() => {
                              rememberAppFocus(app.id);
                              setTrustingApp(app);
                            }}
                          >
                            Trusted addresses
                          </DropdownMenuItem>
                        )}
                        <DropdownMenuItem
                          variant="destructive"
                          disabled={busy}
                          onSelect={() => {
                            rememberAppFocus(app.id);
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
      {accountData?.authenticated ? (
        <SignInConnections
          empty={
            <>
              No connections yet. ChatGPT, Claude.ai and other AI apps that
              sign in appear here once you{" "}
              <Link prefetch={false} href="/connect" className="underline">
                connect one
              </Link>
              .
            </>
          }
        />
      ) : null}
      <Dialog
        open={creating}
        onOpenChange={(open) => {
          if (!open && !busy) closeCreate();
        }}
      >
        <DialogContent
          className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
          onCloseAutoFocus={restoreDialogFocus}
        >
          <DialogHeader>
            <DialogTitle>{createdApp ? "Create key" : "New app"}</DialogTitle>
            <DialogDescription>
              A key can use only what its app&apos;s access plan allows. Each
              app gets its own key, so you can turn one off without touching
              the others.
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
                maxLength={80}
                placeholder="Open WebUI on my laptop"
                value={name}
                disabled={!!createdApp || busy}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <fieldset className="space-y-2">
              <legend className="text-label">Access plan</legend>
              {createdApp ? (
                <p className="text-muted-foreground text-meta">
                  {planDescription(createdApp.access_plan)}
                </p>
              ) : viewer.admin ? (
                <>
                  <Select value={plan} onValueChange={setPlan} disabled={busy}>
                    <SelectTrigger id="app-plan" aria-label="Access plan" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SAME_AS_PERSON}>
                        Same as you{personPlan ? ` (${personPlan.name})` : ""}
                      </SelectItem>
                      {(plans ?? []).map((item: AccessPlan) => (
                        <SelectItem key={item.id} value={item.id}>
                          {item.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-muted-foreground text-meta">
                    {plan === SAME_AS_PERSON
                      ? (personPlan?.summary ?? planDescription(personPlan))
                      : `${chosenPlan?.summary ?? planDescription(chosenPlan)} It never gets more than your own plan allows.`}
                  </p>
                </>
              ) : (
                <p className="text-muted-foreground text-meta">
                  Same as yours: {planDescription(personPlan)} Only the owner
                  can change an app&apos;s access plan.
                </p>
              )}
            </fieldset>
            {error && (
              <p role="alert" className="text-destructive text-meta">
                {appIsNew
                  ? "Your app was created, but it isn't set up yet. Retry to finish. "
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
        <DialogContent onCloseAutoFocus={restoreDialogFocus}>
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
      <TrustedAddressesDialog
        app={trustingApp}
        appName={
          trustingApp
            ? formatAppName(trustingApp.name, accountData?.user?.email)
            : ""
        }
        onClose={() => setTrustingApp(null)}
      />
      <Dialog open={reveal !== null} onOpenChange={() => {}}>
        <DialogContent
          showCloseButton={false}
          onCloseAutoFocus={restoreDialogFocus}
          className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-[42rem]!"
          onEscapeKeyDown={(event) => event.preventDefault()}
          onPointerDownOutside={(event) => event.preventDefault()}
        >
          <DialogHeader>
            <DialogTitle>Connect {reveal?.app}</DialogTitle>
            <DialogDescription>
              Copy the key now. It is shown only once.
            </DialogDescription>
          </DialogHeader>
          {reveal && (
            <div className="space-y-6">
              {!saved && (
                <div className="space-y-2">
                  <CopyField label="Your key" value={reveal.secret} />
                  {reveal.note ? (
                    <p className="text-muted-foreground text-meta">{reveal.note}</p>
                  ) : null}
                  <p className="text-muted-foreground text-meta">
                    {planSentence(
                      apps.find((app) => app.id === reveal.appId)?.access_plan,
                    )}
                  </p>
                </div>
              )}
              <section aria-labelledby="setup-heading" className="space-y-3 border-t pt-5">
                <h3 id="setup-heading" className="text-body font-medium">
                  Set up your app
                </h3>
                <AppGuide
                  appId={guideApp}
                  onAppChange={setGuideApp}
                  values={guideValues}
                  loading={catalogPending}
                  keyHint={
                    saved
                      ? "Paste the key you saved."
                      : "Paste the key from above."
                  }
                />
                <p className="text-muted-foreground text-meta">
                  This guide stays on{" "}
                  <Link prefetch={false} href="/connect/" className="underline underline-offset-4">
                    Connect
                  </Link>
                  .
                </p>
              </section>
              {saved && !busy && reveal.oldIds.length > 0 && (
                <div role="alert">
                  <p className="text-destructive text-meta">
                    Your new key is saved. Some old keys still work.
                    {error ? ` ${error}` : ""}
                  </p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="md" onClick={() => void saveKey()}>
                      Retry revoking old keys
                    </Button>
                    <Button size="md" variant="outline" onClick={finishLater}>
                      Finish later
                    </Button>
                  </div>
                </div>
              )}
              {saved && busy && (
                <p role="status" className="text-muted-foreground text-meta">
                  Revoking previous keys…
                </p>
              )}
              {!saved && (
                <DialogFooter className="bg-background sticky -bottom-6 -mx-6 -mb-6 border-t px-6 py-4">
                  <Button size="md" onClick={() => void saveKey()}>
                    I’ve saved the key
                  </Button>
                </DialogFooter>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
      <Dialog
        open={accessApp !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setAccessApp(null);
        }}
      >
        <DialogContent onCloseAutoFocus={restoreDialogFocus}>
          <DialogHeader>
            <DialogTitle>
              Access plan for {accessApp ? formatAppName(accessApp.name, accountData?.user?.email) : ""}
            </DialogTitle>
            <DialogDescription>
              The change applies to the app&apos;s keys at once. An app never
              gets more than its person&apos;s own plan allows.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Select value={accessPlan} onValueChange={setAccessPlan} disabled={busy}>
              <SelectTrigger aria-label="Access plan" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SAME_AS_PERSON}>
                  Same as you{personPlan ? ` (${personPlan.name})` : ""}
                </SelectItem>
                {(plans ?? []).map((item: AccessPlan) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-muted-foreground text-meta">
              {accessPlan === SAME_AS_PERSON
                ? planDescription(personPlan)
                : planDescription((plans ?? []).find((item) => item.id === accessPlan))}
            </p>
          </div>
          {error && (
            <p role="alert" className="text-destructive text-meta">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={() => setAccessApp(null)}>
              Cancel
            </Button>
            <Button disabled={busy} onClick={() => void changeAccess()}>
              {busy ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
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

"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
  AudioLines,
  ChevronRight,
  CircleX,
  ImageIcon,
  KeyRound,
  Loader2,
  Pause,
  Play,
  Plus,
  ShieldCheck,
  Video,
} from "lucide-react";
import { formatAppName } from "@/lib/format";
import { useViewer } from "@/components/shell/session";
import { accountApi, type AccountApp } from "@/lib/account-api";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { RelativeTime } from "@/components/ui/relative-time";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  CodeBlock,
  CopyField,
  ErrorState,
  SecretReveal,
} from "@/components/product";
import { capabilityMeta, SectionHeading } from "@/components/product/readiness";
import { StateWord, StatusLine } from "@/components/product/status-ui";
import { AppMark } from "@/components/product/app-mark";
import { CapNotice } from "@/components/product/cap-notice";
import {
  AudioFace,
  MediaBadge,
  mediaBadgeButton,
} from "@/components/product/audio-cover";
import { PageHeader } from "@/components/product/page-header";
import {
  activeCapRefusal,
  CAP_WARNING,
  isInFlight,
  isSuccess,
  requestCapability,
  type CapabilityView,
} from "@/lib/capability-state";
import { useCapabilityViews } from "@/lib/use-capability-views";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  homeApi,
  type ConnectionResult,
  type HomeCreation,
  type HomeSnapshot,
  type HomeReceipt,
  hasHomeCreationContent,
} from "./home-api";
import { chatAlias, recipeFor, type Client } from "./recipes";

const homeKey = ["account", "home"];
const setupKey = ["gateway", "setup-progress"];
const primaryAppChoices = [
  "Open WebUI",
  "LibreChat",
  "SillyTavern",
  "Cursor",
  "Codex",
  "Claude Code",
] as const satisfies readonly Client[];
const otherClientChoices = [
  "Python",
  "JavaScript",
  "curl",
  "Phone",
] as const satisfies readonly Client[];
type AppChoice = Client | "Other";

function formatMilliseconds(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

function useHomePreviewVisible() {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(
    typeof IntersectionObserver === "undefined",
  );
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return { ref, visible };
}

function firstWords(value?: string | null): string | null {
  const words = value?.trim().split(/\s+/).filter(Boolean) ?? [];
  if (!words.length) return null;
  return words.slice(0, 12).join(" ");
}

/**
 * Home's skeleton is the page it becomes (DESIGN.md: skeletons in the final
 * layout): under the real header and a verdict placeholder, the task panel's
 * 56 px rows, and the Connect panel beside them on desktop.
 */
function HomeLoading() {
  return (
    <div role="status" aria-label="Loading Home" aria-busy="true">
      <div className="grid items-start gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,21rem)]">
        <div className="@container min-w-0">
          <Skeleton className="mb-3 h-6 w-36" />
          <div className="bg-border rounded-panel grid gap-px overflow-hidden border @2xl:grid-cols-2">
            {TASKS.map((task) => (
              <div
                key={task.key}
                className="bg-card flex h-14 items-center gap-3 px-4 max-sm:h-13"
              >
                <Skeleton className="rounded-control size-8" />
                <Skeleton className="h-4 w-32" />
              </div>
            ))}
            <div aria-hidden="true" className="bg-card hidden @2xl:block" />
          </div>
        </div>
        <Skeleton className="rounded-panel h-64 w-full lg:mt-11" />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Things to do: the member's own tasks come first                     */
/* ------------------------------------------------------------------ */

interface Task {
  key: string;
  title: string;
  tab: string;
}
/* The person's own tasks, most-asked first (brief D1.1 item 4). */
const TASKS: Task[] = [
  { key: "image", title: "Make a picture", tab: "image" },
  { key: "speech", title: "Read text aloud", tab: "speech" },
  { key: "chat", title: "Chat", tab: "chat" },
  { key: "music", title: "Make music", tab: "audio" },
  { key: "sfx", title: "Make a sound effect", tab: "audio" },
  { key: "video", title: "Make a video", tab: "video" },
  { key: "transcribe", title: "Turn a recording into text", tab: "transcribe" },
];
const USABLE = new Set(["ready", "busy", "degraded", "unknown"]);

function taskHref(task: Task, models: Record<string, string | undefined>) {
  const params = new URLSearchParams({ capability: task.tab });
  const model = models[task.key];
  if (model) params.set("model", model);
  return `/try/?${params.toString()}`;
}

/** How often, and when last, this person got a result from each task. */
function taskUse(receipts: HomeReceipt[]) {
  const uses = new Map<string, { count: number; last: number }>();
  for (const receipt of receipts) {
    if (!isSuccess(receipt.status)) continue;
    const capability = requestCapability(receipt.intent, receipt.path);
    if (!capability) continue;
    const at = receipt.accepted_at_ms
      ? receipt.accepted_at_ms / 1000
      : (receipt._stored_at ?? 0);
    const use = uses.get(capability) ?? { count: 0, last: 0 };
    uses.set(capability, {
      count: use.count + 1,
      last: Math.max(use.last, at),
    });
  }
  return uses;
}

/** A task's state word, only when it is not Ready. */
function TaskState({ view }: { view: CapabilityView }) {
  if (view.state === "ready") return null;
  return (
    <span className="flex min-w-0 items-center gap-2">
      <StateWord tone={view.tone} word={view.word} className="shrink-0" />
      {!USABLE.has(view.state) && view.detail ? (
        <span className="text-muted-foreground truncate text-sm">
          {view.detail}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The person's two most-used tasks, with real weight (D1.4: seven equal
 * rows never showed the next step; Apple Home puts favourites before the
 * rest). Picked from their own successful requests; someone new gets
 * pictures and chat, the household's most common asks.
 */
function Favourite({
  task,
  view,
  href,
  usedAt,
}: {
  task: Task;
  view: CapabilityView;
  href: string;
  usedAt: number | null;
}) {
  const meta = capabilityMeta(task.key);
  const Icon = meta.icon;
  const usable = USABLE.has(view.state);
  const body = (
    <>
      <Icon
        aria-hidden="true"
        className={cn("size-6", usable ? meta.tone : "text-muted-foreground")}
      />
      <span className="mt-auto text-base leading-snug font-semibold">
        {task.title}
      </span>
      <span className="text-muted-foreground min-h-5 text-sm">
        {view.state !== "ready" ? (
          <TaskState view={view} />
        ) : usedAt ? (
          <>
            Last used <RelativeTime ts={usedAt} fallback="" />
          </>
        ) : null}
      </span>
    </>
  );
  const tile =
    "bg-card rounded-panel flex h-32 min-w-0 flex-col items-start gap-1 border p-4";
  return usable ? (
    <li className="min-w-0">
      <Link
        href={href}
        className={cn(tile, "hover:border-input transition-colors")}
      >
        {body}
      </Link>
    </li>
  ) : (
    <li className={cn(tile, "text-muted-foreground")}>{body}</li>
  );
}

/** Every other task: a quiet row, a state only when it is not Ready. */
function MoreRow({
  task,
  view,
  href,
}: {
  task: Task;
  view: CapabilityView;
  href: string;
}) {
  const meta = capabilityMeta(task.key);
  const Icon = meta.icon;
  const usable = USABLE.has(view.state);
  const body = (
    <>
      <Icon
        aria-hidden="true"
        className={cn("size-4 shrink-0", usable ? meta.tone : undefined)}
      />
      <span className="min-w-0 flex-1 truncate">{task.title}</span>
      <TaskState view={view} />
    </>
  );
  const row = "flex min-h-11 min-w-0 items-center gap-3 py-2 text-sm";
  if (!usable)
    return (
      <li
        className={cn(row, "text-muted-foreground")}
        title={view.detail ?? undefined}
      >
        {body}
      </li>
    );
  return (
    <li className="min-w-0">
      <Link
        href={href}
        className={cn(row, "hover:text-link font-medium transition-colors")}
      >
        {body}
      </Link>
    </li>
  );
}

function Tasks({
  views,
  models,
  receipts,
}: {
  views: CapabilityView[];
  models: Record<string, string | undefined>;
  receipts: HomeReceipt[];
}) {
  const byKey = new Map(views.map((view) => [view.key, view]));
  const tasks = TASKS.filter((task) => byKey.has(task.key));
  if (!tasks.length) return null;
  const uses = taskUse(receipts);
  const usable = (task: Task) => USABLE.has(byKey.get(task.key)!.state);
  // Most-used first, then the fixed order; only usable tasks are favourites.
  const favourites = tasks
    .filter(usable)
    .map((task, index) => ({ task, index, use: uses.get(task.key) }))
    .sort(
      (a, b) =>
        (b.use?.count ?? 0) - (a.use?.count ?? 0) ||
        (b.use?.last ?? 0) - (a.use?.last ?? 0) ||
        a.index - b.index,
    )
    .slice(0, 2)
    .map(({ task }) => task);
  const rest = tasks.filter((task) => !favourites.includes(task));
  return (
    <section aria-labelledby="tasks-heading" className="@container">
      <SectionHeading id="tasks-heading" title="Make something" />
      <ul className="grid grid-cols-2 gap-2 sm:gap-3">
        {favourites.map((task) => (
          <Favourite
            key={task.key}
            task={task}
            view={byKey.get(task.key)!}
            href={taskHref(task, models)}
            usedAt={uses.get(task.key)?.last || null}
          />
        ))}
      </ul>
      {rest.length ? (
        <ul className="mt-2 grid gap-x-8 @2xl:grid-cols-2">
          {rest.map((task) => (
            <MoreRow
              key={task.key}
              task={task}
              view={byKey.get(task.key)!}
              href={taskHref(task, models)}
            />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Notices                                                             */
/* ------------------------------------------------------------------ */

function lastUsed(app: HomeSnapshot["apps"][number]): number | null {
  const times = app.credentials
    .map((key) => key.last_used_at)
    .filter((value): value is number => typeof value === "number");
  return times.length ? Math.max(...times) : null;
}

function YourApps({ home }: { home: HomeSnapshot }) {
  const apps = home.apps
    .filter((app) => !app.disabled_at)
    .toSorted((a, b) => (lastUsed(b) ?? 0) - (lastUsed(a) ?? 0));
  if (!apps.length) return null;
  return (
    <div>
      <h3 className="text-muted-foreground mb-1 text-sm font-medium">
        Your apps
      </h3>
      <ul className="divide-y">
        {apps.slice(0, 4).map((app) => {
          const used = lastUsed(app);
          return (
            <li key={app.id}>
              <Link
                href={`/apps/?app=${encodeURIComponent(app.id)}`}
                className="group flex min-h-11 items-center gap-3 py-2"
              >
                <span className="line-clamp-2 min-w-0 flex-1 text-sm font-medium break-words group-hover:underline">
                  {formatAppName(app.name, home.user.email)}
                </span>
                <span className="text-muted-foreground shrink-0 text-xs">
                  {used ? (
                    <>
                      Used <RelativeTime ts={used} fallback="" />
                    </>
                  ) : (
                    "No requests yet"
                  )}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {apps.length > 4 ? (
        <Link
          href="/apps/"
          className="text-link inline-flex min-h-11 items-center text-sm font-medium underline-offset-4 hover:underline"
        >
          All {apps.length} apps
        </Link>
      ) : null}
    </div>
  );
}

function Connect({
  home,
  startOpen,
}: {
  home: HomeSnapshot;
  startOpen: boolean;
}) {
  const queryClient = useQueryClient();
  const [chooserOpen, setChooserOpen] = useState(startOpen);
  const [appChoice, setAppChoice] = useState<AppChoice | null>(null);
  const [client, setClient] = useState<Client>("Open WebUI");
  const [selectedCredentialId, setSelectedCredentialId] = useState("new");
  const [notesOpen, setNotesOpen] = useState(false);
  const [reveal, setReveal] = useState<{ secret: string; id: string } | null>(
    null,
  );
  const [creating, setCreating] = useState(false);
  const [pendingApp, setPendingApp] = useState<AccountApp | null>(null);
  const createLock = useRef(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pastedKey, setPastedKey] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const keys = home.apps
    .flatMap((app) =>
      app.credentials.map((key) => ({
        ...key,
        appName: formatAppName(app.name, home.user.email),
      })),
    )
    .filter(
      (key) =>
        key.revoked_at == null &&
        (key.expires_at == null || key.expires_at > Date.now() / 1000),
    );
  const scopedAliases =
    selectedCredentialId === "new"
      ? (home.new_app_aliases ?? [])
      : (home.credential_aliases?.[selectedCredentialId] ?? []);
  const model = chatAlias(scopedAliases);
  const loadingKeyAccess =
    creating &&
    selectedCredentialId !== "new" &&
    !Object.hasOwn(home.credential_aliases ?? {}, selectedCredentialId);
  const canCreate =
    home.apps.some((app) => app.can_mint_keys) || home.apps.length === 0;
  const choiceLocked = creating || !!reveal || testing;
  const chosenAppName = appChoice === "Other" ? client : (appChoice ?? client);
  const selectedRecipe = recipeFor(
    client,
    home.api_base_url,
    home.public_base_url,
    model ?? "",
  );

  async function createKey() {
    if (createLock.current) return;
    createLock.current = true;
    setCreating(true);
    setKeyError(null);
    setTestResult(null);
    setSelectedCredentialId("new");
    setReveal(null);
    setPastedKey("");
    try {
      const app = pendingApp ?? (await accountApi.createApp(chosenAppName));
      setPendingApp(app);
      const key = await accountApi.mintKey(app.id, chosenAppName);
      setSelectedCredentialId(key.id);
      setReveal({ secret: key.api_key, id: key.id });
      setSaved(false);
      setPendingApp(null);
      await queryClient.invalidateQueries({ queryKey: homeKey });
      await queryClient.invalidateQueries({ queryKey: ["account", "session"] });
    } catch {
      setKeyError(
        "Your key could not be created. Try again to finish connecting this app.",
      );
    } finally {
      setCreating(false);
      createLock.current = false;
    }
  }
  async function testConnection() {
    const secret = reveal?.secret || pastedKey.trim();
    if (!secret || testing || selectedCredentialId === "new") return;
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      setTestResult(await homeApi.test(secret, selectedCredentialId));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: homeKey }),
        queryClient.invalidateQueries({ queryKey: setupKey }),
      ]);
    } catch (error) {
      setTestError(
        error instanceof ApiError
          ? error.status === 401 || error.status === 403
            ? "The gateway check could not finish. Check your key and try again."
            : `The gateway check failed (HTTP ${error.status}). Try again.`
          : "Check failed. Try again.",
      );
    } finally {
      setTesting(false);
    }
  }
  return (
    <section
      id="connect"
      aria-labelledby="connect-heading"
      className="bg-card rounded-panel scroll-mt-20 border"
    >
      <div className="space-y-5 p-5">
        <div>
          <h2 id="connect-heading" className="text-lg font-semibold">
            Connect an app
          </h2>
          <p className="text-muted-foreground text-sm">
            For chat apps, phone apps and coding tools. Each app gets its own
            key.
          </p>
        </div>
        <YourApps home={home} />
        {chooserOpen || appChoice ? (
          <div>
            <h3 className="text-muted-foreground mb-2 text-sm font-medium">
              Add an app
            </h3>
            <div className="grid grid-cols-2 gap-1.5">
              {([...primaryAppChoices, "Other"] as AppChoice[]).map((name) => (
                <button
                  key={name}
                  type="button"
                  className={cn(
                    "rounded-control flex min-h-11 items-center gap-2.5 border px-2 text-left text-sm transition-colors disabled:opacity-50",
                    appChoice === name
                      ? "border-ring bg-accent text-accent-foreground"
                      : "bg-card hover:border-input border-transparent",
                  )}
                  aria-pressed={appChoice === name}
                  disabled={choiceLocked}
                  onClick={() => {
                    setAppChoice(name);
                    setClient(name === "Other" ? "Python" : name);
                    setTestResult(null);
                    setTestError(null);
                  }}
                >
                  <AppMark app={name} />
                  <span className="truncate">
                    {name === "Other" ? "Another app" : name}
                  </span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <Button variant="outline" onClick={() => setChooserOpen(true)}>
            <Plus aria-hidden="true" />
            Add an app
          </Button>
        )}
        {appChoice && (
          <div className="space-y-4 border-t pt-5">
            {appChoice === "Other" && (
              <div className="space-y-2">
                <label htmlFor="other-client" className="text-sm font-medium">
                  Recipe
                </label>
                <Select
                  value={client}
                  disabled={choiceLocked}
                  onValueChange={(value) => {
                    setClient(value as Client);
                    setTestResult(null);
                    setTestError(null);
                  }}
                >
                  <SelectTrigger id="other-client" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {otherClientChoices.map((name) => (
                      <SelectItem key={name} value={name}>
                        {name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            {(selectedRecipe.fields[0]?.value ?? home.api_base_url) !==
            home.api_base_url ? (
              <CopyField
                label="Base URL"
                value={selectedRecipe.fields[0]?.value ?? home.api_base_url}
              />
            ) : (
              <CopyField label="Gateway address" value={home.api_base_url} />
            )}
            {model && <CopyField label="Model" value={model} />}
            <div className="space-y-2">
              <label htmlFor="app-key" className="text-sm font-medium">
                API key
              </label>
              <Select
                value={selectedCredentialId}
                disabled={creating || !!reveal || testing}
                onValueChange={(value) => {
                  setSelectedCredentialId(value);
                  setTestResult(null);
                  setTestError(null);
                  setPastedKey("");
                  setSaved(false);
                }}
              >
                <SelectTrigger id="app-key" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="new">
                    New key for {chosenAppName}
                  </SelectItem>
                  {selectedCredentialId !== "new" &&
                    !keys.some((key) => key.id === selectedCredentialId) && (
                      <SelectItem value={selectedCredentialId}>
                        New key · loading access
                      </SelectItem>
                    )}
                  {keys.map((key) => (
                    <SelectItem key={key.id} value={key.id}>
                      {key.appName} · {key.display_name || "Key"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {reveal ? (
                <SecretReveal
                  key={reveal.id}
                  secret={reveal.secret}
                  onSaved={() => {
                    setReveal(null);
                    setSaved(true);
                  }}
                />
              ) : selectedCredentialId !== "new" ? (
                <div className="space-y-2">
                  <label
                    htmlFor="pasted-app-key"
                    className="text-sm font-medium"
                  >
                    Paste your {selectedRecipe.keyLabel}
                  </label>
                  <Input
                    id="pasted-app-key"
                    type="password"
                    value={pastedKey}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => {
                      setPastedKey(event.target.value);
                      setTestResult(null);
                      setTestError(null);
                    }}
                    placeholder="Paste the selected app key"
                    disabled={testing}
                  />
                  <p className="text-muted-foreground text-xs">
                    {saved
                      ? "Key saved. Paste it here to check the connection. "
                      : ""}
                    Kept only while this page is open.
                  </p>
                </div>
              ) : (
                <p className="text-muted-foreground text-sm">
                  The key appears once. Copy it into {chosenAppName}.
                </p>
              )}
              {!canCreate && (
                <p className="text-muted-foreground text-sm">
                  Your access plan does not let you create keys. Ask the gateway
                  owner for access.
                </p>
              )}
              {keyError && (
                <p role="alert" className="text-destructive text-sm">
                  {keyError}
                </p>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              {!reveal && (
                <Button
                  variant={
                    selectedCredentialId === "new" ? "default" : "outline"
                  }
                  disabled={creating || !canCreate || testing}
                  onClick={() => void createKey()}
                >
                  {creating ? (
                    <Loader2 className="animate-spin" aria-hidden="true" />
                  ) : (
                    <KeyRound aria-hidden="true" />
                  )}
                  {creating
                    ? "Creating key…"
                    : pendingApp
                      ? "Retry key creation"
                      : `Create key for ${chosenAppName}`}
                </Button>
              )}
              <Button
                variant={
                  selectedCredentialId !== "new" && !reveal && pastedKey.trim()
                    ? "default"
                    : "outline"
                }
                disabled={
                  testing ||
                  selectedCredentialId === "new" ||
                  !(reveal?.secret || pastedKey.trim())
                }
                onClick={() => void testConnection()}
              >
                {testing ? (
                  <Loader2 aria-hidden="true" className="animate-spin" />
                ) : (
                  <ShieldCheck aria-hidden="true" />
                )}
                {testing ? "Checking…" : "Check connection"}
              </Button>
              <Button variant="ghost" onClick={() => setNotesOpen(true)}>
                Setup notes
              </Button>
            </div>
            {loadingKeyAccess ? (
              <div
                role="status"
                aria-label="Loading key access"
                aria-busy="true"
              >
                <Skeleton className="h-16 w-full" />
              </div>
            ) : !model ? (
              <div className="bg-muted rounded-control p-4 text-sm">
                <p className="font-medium">
                  This key does not include the default model
                </p>
                <p className="text-muted-foreground mt-1">
                  Choose another key, or ask{" "}
                  {home.gateway?.owner_name?.trim() || "the owner"} to update
                  this app’s access.
                </p>
              </div>
            ) : null}
            {testResult && (
              <div
                role="status"
                className={cn(
                  "rounded-control space-y-1 p-3 text-sm",
                  testResult.ok ? "bg-success-tint" : "bg-danger-tint",
                )}
              >
                <p
                  className={cn(
                    "flex items-center gap-2 font-medium",
                    testResult.ok ? "text-success" : "text-destructive",
                  )}
                >
                  {testResult.ok ? (
                    <ShieldCheck aria-hidden="true" className="size-4" />
                  ) : (
                    <CircleX aria-hidden="true" className="size-4" />
                  )}
                  {testResult.ok
                    ? "Gateway responded with this key"
                    : "Connection check failed"}
                  {testResult.latency_ms != null
                    ? ` · ${formatMilliseconds(testResult.latency_ms)}`
                    : ""}
                </p>
                <p className="text-foreground">{testResult.reason}</p>
                {testResult.request_id && (
                  <a
                    className="text-link inline-flex min-h-11 items-center underline underline-offset-4"
                    href={`/r/${encodeURIComponent(testResult.request_id)}`}
                  >
                    View request receipt
                  </a>
                )}
              </div>
            )}
            {testError && (
              <p role="alert" className="text-destructive text-sm">
                {testError}
              </p>
            )}
          </div>
        )}
      </div>
      <Sheet open={notesOpen} onOpenChange={setNotesOpen}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>Setup notes</SheetTitle>
            <SheetDescription>
              Version notes and network caveats for {chosenAppName}.
            </SheetDescription>
          </SheetHeader>
          <div className="space-y-4 px-4 pb-4">
            <p className="text-sm">{selectedRecipe.description}</p>
            <p className="text-muted-foreground text-sm">
              The gateway check verifies the route and key. It does not verify
              the app.
            </p>
            <p className="text-muted-foreground text-sm">
              {client === "Open WebUI"
                ? "Tested with Open WebUI v0.11.4 using host networking and non-streaming."
                : "Check your app version and network path before testing."}
            </p>
            {model ? (
              <CodeBlock
                language={selectedRecipe.language}
                code={selectedRecipe.code}
              />
            ) : null}
            <a
              className="text-link inline-flex min-h-11 items-center gap-1 text-sm underline underline-offset-4"
              href={selectedRecipe.docs}
              target="_blank"
              rel="noreferrer"
            >
              {chosenAppName} docs
              <ArrowUpRight aria-hidden="true" className="size-3.5" />
            </a>
          </div>
        </SheetContent>
      </Sheet>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Recent creations                                                    */
/* ------------------------------------------------------------------ */

function CreationMedia({
  creation,
  title,
  visible,
}: {
  creation: HomeCreation;
  title: string;
  visible: boolean;
}) {
  const Icon =
    creation.kind === "video"
      ? Video
      : creation.kind === "audio"
        ? AudioLines
        : ImageIcon;
  const { data: poster, isPending: posterQueryPending } = useQuery({
    queryKey: ["library", "poster", creation.poster_asset_id, false],
    enabled: creation.kind === "video" && !!creation.poster_asset_id,
    queryFn: () => homeApi.mediaDownload(creation.poster_asset_id!),
    retry: false,
  });
  const posterUrl = poster?.download_url;
  const [imageFailed, setImageFailed] = useState(false);
  const [posterFailed, setPosterFailed] = useState(false);
  const posterPending =
    creation.kind === "video" &&
    !!creation.poster_asset_id &&
    posterQueryPending &&
    !posterFailed;
  const [videoFailed, setVideoFailed] = useState(false);
  const [audioError, setAudioError] = useState<"media" | "playback" | null>(
    null,
  );
  const [audioSrc, setAudioSrc] = useState(creation.download_url ?? undefined);
  const [isPlaying, setIsPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    setImageFailed(false);
    setPosterFailed(false);
    setVideoFailed(false);
    setAudioError(null);
    setIsPlaying(false);
    setAudioSrc(creation.download_url ?? undefined);
  }, [creation.asset_id, creation.download_url, posterUrl]);

  if (creation.kind === "image" && creation.download_url && !imageFailed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={creation.download_url}
        alt={title}
        loading="lazy"
        onError={() => setImageFailed(true)}
        className="size-full object-cover"
      />
    );
  }

  if (posterPending) {
    return (
      <div
        role="status"
        aria-label="Loading video poster"
        className="bg-muted text-muted-foreground flex size-full items-center justify-center"
      >
        <Loader2 aria-hidden="true" className="size-5 animate-spin" />
      </div>
    );
  }

  if (creation.kind === "video" && posterUrl && !posterFailed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={posterUrl}
        alt={title}
        loading="lazy"
        onError={() => setPosterFailed(true)}
        className="size-full object-cover"
      />
    );
  }

  if (
    creation.kind === "video" &&
    creation.download_url &&
    !videoFailed &&
    !posterPending
  ) {
    return (
      <video
        src={visible ? creation.download_url : undefined}
        preload="none"
        autoPlay={visible}
        muted
        playsInline
        onLoadedData={(event) => event.currentTarget.pause()}
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          if (Number.isFinite(video.duration) && video.duration > 0) {
            video.currentTime = Math.min(0.1, video.duration / 2);
          }
        }}
        onError={() => setVideoFailed(true)}
        className="size-full object-cover"
        aria-label={`${title} video preview`}
      />
    );
  }

  if (creation.kind === "audio" && creation.download_url) {
    return (
      <AudioFace
        peaksKey={creation.asset_id}
        src={audioSrc}
        visible={visible}
        status={
          audioError ? (
            <span role="status" className="text-destructive">
              {audioError === "media"
                ? "Audio preview unavailable. Play to retry."
                : "Playback didn't start. Try again."}
            </span>
          ) : null
        }
        control={
          <>
            <audio
              ref={audioRef}
              preload="none"
              src={audioSrc}
              onPlay={() => {
                setAudioError(null);
                setIsPlaying(true);
              }}
              onPause={() => setIsPlaying(false)}
              onEnded={() => setIsPlaying(false)}
              onError={() => {
                setIsPlaying(false);
                setAudioError("media");
              }}
              className="sr-only"
            />
            <button
              type="button"
              className={mediaBadgeButton(true)}
              aria-label={`${isPlaying ? "Pause" : "Play"} ${title}`}
              onClick={async () => {
                const audio = audioRef.current;
                if (!audio) return;
                if (isPlaying) audio.pause();
                else {
                  try {
                    if (audioError === "media") {
                      const renewed = await homeApi.mediaDownload(
                        creation.asset_id,
                      );
                      if (renewed.download_url) {
                        setAudioSrc(renewed.download_url);
                        audio.src = renewed.download_url;
                      }
                      audio.load();
                    }
                    await audio.play();
                    setAudioError(null);
                  } catch {
                    setIsPlaying(false);
                    setAudioError(
                      audioError === "media" || audio.error
                        ? "media"
                        : "playback",
                    );
                  }
                }
              }}
            >
              {isPlaying ? (
                <Pause aria-hidden="true" />
              ) : (
                <Play aria-hidden="true" />
              )}
            </button>
          </>
        }
      />
    );
  }

  return (
    <div
      role="img"
      aria-label={`${creation.kind[0].toUpperCase()}${creation.kind.slice(1)} preview unavailable`}
      className="bg-muted text-muted-foreground flex size-full flex-col items-center justify-center gap-2 p-3 text-center"
    >
      <Icon aria-hidden="true" className="size-6" />
      <p className="text-xs">Preview unavailable</p>
    </div>
  );
}

function CreationPreview({ creation }: { creation: HomeCreation }) {
  const { ref: visibilityRef, visible } = useHomePreviewVisible();
  const label =
    creation.kind === "video"
      ? "Video"
      : creation.kind === "audio"
        ? "Audio"
        : "Image";
  const prompt = firstWords(creation.prompt);
  const title = prompt ?? label;
  const linkLabel = prompt
    ? `Open ${label} in Library: ${prompt}`
    : `Open ${label} in Library`;
  const tile =
    "bg-muted group relative block aspect-square min-w-0 overflow-hidden rounded-media";
  const body = (
    <div ref={visibilityRef} className="size-full">
      <CreationMedia creation={creation} title={title} visible={visible} />
    </div>
  );
  if (creation.kind === "audio") {
    return (
      <li className={tile}>
        {body}
        <Link
          href={`/library/?asset=${encodeURIComponent(creation.asset_id)}`}
          aria-label={linkLabel}
          className="rounded-media absolute inset-0"
        />
      </li>
    );
  }
  return (
    <li className="min-w-0">
      <Link
        href={`/library/?asset=${encodeURIComponent(creation.asset_id)}`}
        aria-label={linkLabel}
        className={tile}
      >
        {body}
        {creation.kind === "video" ? (
          <MediaBadge icon={Play} className="absolute right-2 bottom-2" />
        ) : null}
      </Link>
    </li>
  );
}

function inFlightKind(
  receipt: HomeReceipt,
): "image" | "video" | "audio" | null {
  const intent = String(receipt.intent ?? "").toLowerCase();
  const path = String(receipt.path ?? "").toLowerCase();
  if (intent.startsWith("image") || path.includes("/images/")) return "image";
  if (intent.startsWith("video") || path.includes("/videos")) return "video";
  if (
    intent.startsWith("audio") ||
    intent.startsWith("speech") ||
    path.includes("/audio/")
  )
    return "audio";
  return null;
}

/**
 * Work running on a capability that is failing says so (red team round 2:
 * a video ran on a failing engine with no warning).
 */
function runningRisk(
  receipt: HomeReceipt,
  views: CapabilityView[],
): string | null {
  const key = requestCapability(receipt.intent, receipt.path);
  const view = views.find((item) => item.key === key);
  return view && !["ready", "busy"].includes(view.state)
    ? `${view.label} ${view.word.toLowerCase()}: it may not finish`
    : null;
}

function Recent({
  receipts,
  views,
}: {
  receipts: HomeReceipt[];
  views: CapabilityView[];
}) {
  const making = receipts.filter(
    (receipt) => isInFlight(receipt.status) && inFlightKind(receipt),
  );
  const {
    data: creations,
    isPending: creationsPending,
    isError: creationsError,
    refetch: refetchCreations,
    isFetching: creationsFetching,
  } = useQuery({
    queryKey: ["library", "home-recent-creations"],
    queryFn: homeApi.creations,
  });
  // A creation whose file the gateway no longer stores belongs in Library, not Recent.
  const recentCreations = (creations?.assets ?? [])
    .filter(hasHomeCreationContent)
    .filter((creation) => creation.available !== false)
    .slice(0, 4);
  if (
    !creationsPending &&
    !creationsError &&
    recentCreations.length === 0 &&
    !making.length
  ) {
    return null;
  }
  return (
    <section aria-labelledby="recent-heading">
      <SectionHeading
        id="recent-heading"
        title="Recent"
        action={
          <Link
            className="text-link inline-flex min-h-11 items-center gap-0.5 text-sm font-medium underline-offset-4 hover:underline"
            href="/library/"
          >
            Library
            <ChevronRight aria-hidden="true" className="size-4" />
          </Link>
        }
      />
      {creationsPending ? (
        <div
          role="status"
          aria-label="Loading recent creations"
          aria-busy="true"
          className="grid grid-cols-4 gap-1.5 sm:gap-2"
        >
          {[0, 1, 2, 3].map((item) => (
            <Skeleton
              key={item}
              className="rounded-media aspect-square w-full"
            />
          ))}
        </div>
      ) : creationsError ? (
        <ErrorState
          message="Recent creations could not be loaded. Try again."
          onRetry={() => void refetchCreations()}
          retrying={creationsFetching}
        />
      ) : (
        <>
          {creations?.partial ? (
            <p role="status" className="text-muted-foreground mb-2 text-sm">
              Recent results may be incomplete.
            </p>
          ) : null}
          {/* Four across at every width: on a phone, a strip of the
              household's latest work right under the tasks (Apple Home's
              cameras sit on the first screen). */}
          <ul className="grid grid-cols-4 gap-1.5 sm:gap-2">
            {making.slice(0, 2).map((receipt) => {
              const kind = inFlightKind(receipt);
              const Icon =
                kind === "video"
                  ? Video
                  : kind === "audio"
                    ? AudioLines
                    : ImageIcon;
              const at = receipt.accepted_at_ms
                ? receipt.accepted_at_ms / 1000
                : receipt._stored_at;
              const risk = runningRisk(receipt, views);
              return (
                <li
                  key={receipt.request_id}
                  className="bg-muted text-muted-foreground rounded-media flex aspect-square flex-col justify-end gap-1 p-3 max-sm:items-center max-sm:justify-center max-sm:p-2"
                >
                  <Icon aria-hidden="true" className="size-5" />
                  <span className="text-foreground text-sm font-medium max-sm:sr-only">
                    {kind === "video"
                      ? "Making your video"
                      : kind === "audio"
                        ? "Making your audio"
                        : "Making your picture"}
                  </span>
                  {at ? (
                    <span className="text-xs max-sm:sr-only">
                      Started <RelativeTime ts={at} fallback="" />
                    </span>
                  ) : null}
                  {risk ? (
                    <span className="text-warning text-xs font-medium max-sm:sr-only">
                      {risk}
                    </span>
                  ) : null}
                </li>
              );
            })}
            {recentCreations
              .slice(0, 4 - Math.min(making.length, 2))
              .map((creation) => (
                <CreationPreview key={creation.asset_id} creation={creation} />
              ))}
          </ul>
        </>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export default function HomePage() {
  const viewer = useViewer();
  const {
    data: home,
    isPending: homePending,
    isError: homeError,
    refetch: refetchHome,
    isFetching: homeFetching,
  } = useQuery({ queryKey: homeKey, queryFn: homeApi.home });
  const isOwner = Boolean(
    home?.authenticated &&
    ["owner", "superadmin", "admin"].includes(home.user.role) &&
    viewer.admin,
  );
  const model = useCapabilityViews(isOwner);
  const views = model.views;
  const verdict = model.verdict;
  const refusal = activeCapRefusal(model.requests);
  // Someone with no app that has ever made a request starts by connecting one.
  const newcomer = Boolean(
    home?.authenticated &&
    !home.apps.some((app) =>
      app.credentials.some((key) => typeof key.last_used_at === "number"),
    ),
  );
  const models: Record<string, string | undefined> = {};
  if (home && home.authenticated)
    for (const row of home.capabilities)
      models[row.icon_key ?? row.capability] ??=
        row.use_name ?? row.model ?? undefined;
  return (
    <div className="min-w-0">
      {homePending ? (
        <>
          <PageHeader title="Home">
            <Skeleton className="h-5 w-56" />
          </PageHeader>
          <HomeLoading />
        </>
      ) : homeError ? (
        <>
          <PageHeader title="Home" />
          <ErrorState
            heading="Couldn’t load Home"
            // "Status may show why" pointed at a page with no link to it
            // (red team round 2); the retry is the one next step.
            message="The gateway did not return your Home. Try again."
            onRetry={() => void refetchHome()}
            retrying={homeFetching}
          />
        </>
      ) : !home.authenticated ? (
        <>
          <PageHeader title="Home" />
          <ErrorState
            severity="info"
            heading="Sign in to use your AI"
            message="Make pictures, chat and more with the apps you already use."
            action={{
              label: "Sign in",
              href: `/ui/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/ui/home/" : window.location.pathname + window.location.search)}`,
            }}
          />
        </>
      ) : (
        <>
          <PageHeader
            title="Home"
            actions={
              // Connecting an app is secondary, but one tap from the top
              // (Apple Home puts "+" in the top bar).
              <Button variant="outline" size="sm" asChild className="lg:hidden">
                <a href="#connect">
                  <Plus aria-hidden="true" />
                  Connect an app
                </a>
              </Button>
            }
          >
            <StatusLine
              compact
              tone={verdict.tone}
              text={verdict.text}
              checkedAt={verdict.checkedAt}
              unconfirmed={verdict.unconfirmed}
            >
              {verdict.problems.length ? (
                // Members too: on a phone Status is under More, so the
                // warning itself leads to its explanation (red team round 2).
                <Link
                  href="/status/"
                  className="text-link inline-flex min-h-11 items-center gap-0.5 font-medium underline-offset-4 hover:underline sm:min-h-0"
                >
                  {isOwner ? "Open Status" : "See what is happening"}
                  <ChevronRight aria-hidden="true" className="size-4" />
                </Link>
              ) : verdict.tone === "off" && !isOwner ? (
                // A member cannot turn anything on; say who can.
                <span className="text-muted-foreground">
                  {home.gateway?.owner_name?.trim()
                    ? `Ask ${home.gateway.owner_name.trim()} to turn something on.`
                    : "Ask the gateway owner to turn something on."}
                </span>
              ) : verdict.tone === "off" && isOwner ? (
                // Nothing turned on: the owner's next step is an engine.
                <Link
                  href="/engines/"
                  className="text-link inline-flex min-h-11 items-center gap-0.5 font-medium underline-offset-4 hover:underline sm:min-h-0"
                >
                  Add an engine
                  <ChevronRight aria-hidden="true" className="size-4" />
                </Link>
              ) : null}
            </StatusLine>
          </PageHeader>
          <div className="grid items-start gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,21rem)]">
            <div className="min-w-0 space-y-10 max-sm:space-y-7 lg:col-start-1 lg:row-start-1">
              {refusal ? (
                <CapNotice
                  warning={
                    (refusal.warnings ?? []).find((text) =>
                      CAP_WARNING.test(text),
                    ) ?? ""
                  }
                  at={refusal.at}
                  owner={isOwner}
                />
              ) : null}
              <Recent receipts={home.receipts ?? []} views={views} />
              <Tasks
                views={views}
                models={models}
                receipts={home.receipts ?? []}
              />
            </div>
            {/* Top aligned with the first list, under its section heading. */}
            <div className="min-w-0 lg:col-start-2 lg:row-start-1 lg:mt-11">
              <Connect home={home} startOpen={newcomer} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

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
import { keyPromptNote } from "@/lib/prompt-memory";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Skeleton, SkeletonRows } from "@/components/ui/skeleton";
import { MakingTile } from "@/components/product/making-tile";
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
import { ListCard, ListRow } from "@/components/product/list-card";
import { StateBadge, StatusLine } from "@/components/product/status-ui";
import { AppMark } from "@/components/product/app-mark";
import { CapNotice } from "@/components/product/cap-notice";
import {
  AudioFace,
  MediaBadge,
  mediaGridGap,
  playDiscButton,
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
import { taskNames } from "@/lib/terms";
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
import { chatAlias, transcriptionAlias } from "./chat-alias";
import { keyAppById, keyApps, resolveKeyGuide } from "@/lib/app-guides";
import { capabilityTiles } from "./presentation";

const homeKey = ["account", "home"];
// Recent starts beside Home, not after it: both need only the signed-in session.
const recentCreationsQuery = {
  queryKey: ["library", "home-recent-creations"],
  queryFn: homeApi.creations,
  // Recent mounts after Home settles; it must reuse this read, not repeat it.
  staleTime: 30_000,
};
const setupKey = ["gateway", "setup-progress"];
// Apps lead the chooser; code (Python, JavaScript, curl) sits under "Another app".
const primaryAppChoices = keyApps
  .filter((app) => app.kind !== "Code")
  .map((app) => app.id);
const otherClientChoices = keyApps
  .filter((app) => app.kind === "Code")
  .map((app) => app.id);
type AppChoice = string;
const OTHER = "other";


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
 * layout): under the real header and a verdict placeholder, task rows at
 * 40 and the Connect card beside them on desktop.
 */
function HomeLoading() {
  return (
    <div role="status" aria-label="Loading Home" aria-busy="true">
      <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,21rem)]">
        <div className="min-w-0">
          <Skeleton className="mb-3 h-6 w-36" />
          <div className="bg-card rounded-panel overflow-hidden border">
            <SkeletonRows count={TASKS.length} />
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
  { key: "image", title: taskNames.image, tab: "image" },
  { key: "speech", title: taskNames.speech, tab: "speech" },
  { key: "chat", title: taskNames.chat, tab: "chat" },
  { key: "music", title: taskNames.music, tab: "music" },
  { key: "sfx", title: taskNames.sound_effects, tab: "sfx" },
  { key: "video", title: taskNames.video, tab: "video" },
  { key: "transcribe", title: taskNames.transcribe, tab: "transcribe" },
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

/** A state that is not Ready, as one StateBadge; Ready stays quiet. */
function TaskBadge({ view }: { view: CapabilityView }) {
  return view.state === "ready" ? null : (
    <StateBadge state={view.state} word={view.word} />
  );
}

/**
 * Why a task reads as it does: a Ready task that must start its engine
 * first, or the reason a task cannot be used. Usable faults say it with
 * their badge alone.
 */
function taskReason(view: CapabilityView): string | null {
  return view.state === "ready" || !USABLE.has(view.state) ? view.detail : null;
}

/**
 * The person's two most-used tasks, with real weight (D1.4: seven equal
 * rows never showed the next step; Apple Home puts favourites before the
 * rest). Picked from their own successful requests; someone new gets
 * pictures and chat, the household's most common asks. Only usable tasks
 * are favourites. They lead the same ListCard as the other tasks and keep
 * their "Last used" fact.
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
  const Icon = capabilityMeta(task.key).icon;
  const reason = taskReason(view);
  const note =
    view.state !== "ready" ? (
      <TaskBadge view={view} />
    ) : reason ? (
      reason
    ) : usedAt ? (
      <>
        Last used <RelativeTime ts={usedAt} fallback="" />
      </>
    ) : null;
  return (
    <ListRow
      leading={<Icon aria-hidden="true" className="text-foreground size-4" />}
      title={task.title}
      href={href}
      badge={view.state === "ready" ? undefined : <TaskBadge view={view} />}
      facts={view.state === "ready" && note ? [note] : undefined}
      factClassName="max-w-60 max-sm:max-w-32"
    />
  );
}

/**
 * Every other task: a one-line row at 40 (48 on a phone) with its icon in
 * ink, a StateBadge only when it is not Ready, and the reason when it cannot
 * be used. A usable task opens Try; an unusable one is a plain row.
 */
function TaskRow({
  task,
  view,
  href,
}: {
  task: Task;
  view: CapabilityView;
  href: string;
}) {
  const Icon = capabilityMeta(task.key).icon;
  const usable = USABLE.has(view.state);
  const reason = taskReason(view);
  return (
    <ListRow
      leading={<Icon aria-hidden="true" className="text-foreground size-4" />}
      title={task.title}
      href={usable ? href : undefined}
      badge={view.state === "ready" ? undefined : <TaskBadge view={view} />}
      facts={
        reason
          ? [
              <span key="reason" title={reason}>
                {reason}
              </span>,
            ]
          : undefined
      }
      factClassName="max-w-60 max-sm:max-w-32"
    />
  );
}

function Tasks({
  views,
  models,
  receipts,
  owner,
  statusExplains,
}: {
  views: CapabilityView[];
  models: Record<string, string | undefined>;
  receipts: HomeReceipt[];
  owner: boolean;
  /** The status line already gives the next step for "nothing is on". */
  statusExplains: boolean;
}) {
  const byKey = new Map(views.map((view) => [view.key, view]));
  const tasks = TASKS.filter((task) => byKey.has(task.key));
  if (!tasks.length) return (
    <section aria-labelledby="tasks-heading">
      <SectionHeading id="tasks-heading" title="Make something" />
      <p className="text-muted-foreground text-meta">
        Nothing is available yet.
        {statusExplains ? null : owner ? (
          <>
            {" "}
            <Link prefetch={false} href="/engines/" className="text-link font-medium underline-offset-4 hover:underline">
              Add an engine
            </Link>
          </>
        ) : (
          " Ask the gateway owner for access."
        )}
      </p>
    </section>
  );
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
  // Two heading levels only (craft PR 4): the section heading names the
  // group; favourites and other tasks share one row vocabulary and card.
  return (
    <section aria-labelledby="tasks-heading">
      <SectionHeading id="tasks-heading" title="Make something" />
      <ListCard labelledBy="tasks-heading">
        {favourites.map((task) => (
          <Favourite
            key={task.key}
            task={task}
            view={byKey.get(task.key)!}
            href={taskHref(task, models)}
            usedAt={uses.get(task.key)?.last || null}
          />
        ))}
        {rest.map((task) => (
          <TaskRow
            key={task.key}
            task={task}
            view={byKey.get(task.key)!}
            href={taskHref(task, models)}
          />
        ))}
      </ListCard>
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

/**
 * The person's apps, most recently used first: rows of 40 (48 on a phone),
 * the name in `text-body` 500 and when it was last used in `text-meta`.
 */
function YourApps({ home }: { home: HomeSnapshot }) {
  const apps = home.apps
    .filter((app) => !app.disabled_at)
    .toSorted((a, b) => (lastUsed(b) ?? 0) - (lastUsed(a) ?? 0));
  if (!apps.length) return null;
  return (
    <div>
      <ul aria-label="Your apps" className="divide-y">
        {apps.slice(0, 4).map((app) => {
          const used = lastUsed(app);
          const name = formatAppName(app.name, home.user.email);
          return (
            <li key={app.id}>
              <Link prefetch={false}
                href={`/apps/?app=${encodeURIComponent(app.id)}`}
                className="group flex min-h-10 items-center gap-3 max-sm:min-h-12"
              >
                <span
                  title={name}
                  className="text-body min-w-0 flex-1 truncate font-medium group-hover:underline"
                >
                  {name}
                </span>
                <span className="text-muted-foreground text-meta shrink-0">
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
        <Link prefetch={false}
          href="/apps/"
          className="text-link text-meta inline-flex min-h-10 items-center font-medium underline-offset-4 hover:underline max-sm:min-h-11"
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
  const [client, setClient] = useState<string>(keyApps[0].id);
  const [selectedCredentialId, setSelectedCredentialId] = useState("new");
  const [notesOpen, setNotesOpen] = useState(false);
  const [reveal, setReveal] = useState<{
    secret: string;
    id: string;
    note?: string | null;
  } | null>(null);
  const [creating, setCreating] = useState(false);
  const [pendingApp, setPendingApp] = useState<AccountApp | null>(null);
  const createLock = useRef(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pastedKey, setPastedKey] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [testHttpStatus, setTestHttpStatus] = useState<number | null>(null);
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
  const recommendedChat = capabilityTiles(home.capabilities, home.aliases).find(
    (row) => (row.icon_key ?? row.capability) === "chat",
  );
  const model = chatAlias(
    scopedAliases,
    recommendedChat?.use_name ?? recommendedChat?.model,
  );
  const hasChatAccess = scopedAliases.some((alias) =>
    ["chat.generate", "chat", "chat.completions"].includes(alias.operation),
  );
  const loadingKeyAccess =
    creating &&
    selectedCredentialId !== "new" &&
    !Object.hasOwn(home.credential_aliases ?? {}, selectedCredentialId);
  const canCreate =
    home.apps.some((app) => app.can_mint_keys) || home.apps.length === 0;
  const choiceLocked = creating || !!reveal || testing;
  const guideApp = keyAppById(client) ?? keyApps[0];
  const chosenAppName = guideApp.name;
  const selectedGuide = resolveKeyGuide(guideApp.key, {
    openai_url: home.api_base_url,
    anthropic_url: home.public_base_url,
    mcp_url: `${home.public_base_url}/mcp`,
    chat_model: model,
    transcription_model: transcriptionAlias(scopedAliases),
  });
  const guideAddress = selectedGuide.fields.find((field) => field.isAddress);
  // One copy field per distinct model the app needs (OpenWhispr needs two).
  const guideModels = selectedGuide.fields
    .filter((field) => field.isModel && !field.missing)
    .filter(
      (field, index, all) =>
        all.findIndex((other) => other.value === field.value) === index,
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
      setReveal({
        secret: key.api_key,
        id: key.id,
        note: keyPromptNote(key.prompt_memory),
      });
      setSaved(false);
      setPendingApp(null);
      await queryClient.invalidateQueries({ queryKey: homeKey });
      await queryClient.invalidateQueries({ queryKey: ["account", "session"] });
    } catch {
      setKeyError(
        "We couldn't tell where key creation stopped. Try again.",
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
    setTestHttpStatus(null);
    setTestResult(null);
    try {
      setTestResult(await homeApi.test(secret, selectedCredentialId));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: homeKey }),
        queryClient.invalidateQueries({ queryKey: setupKey }),
      ]);
    } catch (error) {
      setTestError("We couldn't tell where the connection check stopped. Try again.");
      setTestHttpStatus(error instanceof ApiError ? error.status : null);
    } finally {
      setTesting(false);
    }
  }
  return (
    // A content card (padding 24, blocks 16 apart): the title, the person's
    // apps at 40 and "Add an app" at 32.
    <Card
      id="connect"
      role="region"
      aria-labelledby="connect-heading"
      className="scroll-mt-20"
    >
      <div>
        <h2 id="connect-heading" className="text-heading">
          Connect an app
        </h2>
        <p className="text-muted-foreground text-meta mt-1">
          For chat apps, phone apps and coding tools. Each app gets its own key.
        </p>
      </div>
      <YourApps home={home} />
      {chooserOpen || appChoice ? (
        <div>
          <p id="add-app-label" className="text-meta mb-2 font-medium">
            Add an app
          </p>
          <div
            role="group"
            aria-labelledby="add-app-label"
            className="grid grid-cols-2 gap-1.5"
          >
            {[...primaryAppChoices, OTHER].map((name: AppChoice) => (
              <button
                key={name}
                type="button"
                className={cn(
                  "rounded-control text-meta flex min-h-10 items-center gap-2.5 border px-2 text-left transition-colors disabled:opacity-50 max-sm:min-h-11",
                  appChoice === name
                    ? "border-ring bg-accent text-accent-foreground"
                    : "bg-card hover:border-input border-transparent",
                )}
                aria-pressed={appChoice === name}
                disabled={choiceLocked}
                onClick={() => {
                  setAppChoice(name);
                  setClient(name === OTHER ? otherClientChoices[0] : name);
                  setTestResult(null);
                  setTestError(null);
                }}
              >
                <AppMark app={name === OTHER ? "Other" : (keyAppById(name)?.name ?? name)} />
                <span className="truncate">
                  {name === OTHER ? "Another app" : keyAppById(name)?.name}
                </span>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          onClick={() => setChooserOpen(true)}
        >
          <Plus aria-hidden="true" />
          Add an app
        </Button>
      )}
      {appChoice && (
        <div className="space-y-4 border-t pt-4">
          {appChoice === OTHER && (
            <div className="space-y-2">
              <label htmlFor="other-client" className="text-meta font-medium">
                Language or tool
              </label>
              <Select
                value={client}
                disabled={choiceLocked}
                onValueChange={(value) => {
                  setClient(value);
                  setTestResult(null);
                  setTestError(null);
                }}
              >
                <SelectTrigger id="other-client" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {otherClientChoices.map((id) => (
                    <SelectItem key={id} value={id}>
                      {keyAppById(id)?.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {guideAddress && guideAddress.value !== home.api_base_url ? (
            <CopyField label={guideAddress.label} value={guideAddress.value} />
          ) : (
            <CopyField label="Gateway address" value={home.api_base_url} />
          )}
          {guideModels.map((field) => (
            <CopyField
              key={field.value}
              label={field.group ? `${field.group.replace(/ \(optional\)$/, "")} model` : "Model"}
              value={field.value}
            />
          ))}
          <div className="space-y-2">
            <label htmlFor="app-key" className="text-meta font-medium">
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
                <SelectItem value="new">New key for {chosenAppName}</SelectItem>
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
                note={reveal.note}
                onSaved={() => {
                  setReveal(null);
                  setSaved(true);
                }}
              />
            ) : selectedCredentialId !== "new" ? (
              <div className="space-y-2">
                <label
                  htmlFor="pasted-app-key"
                  className="text-meta font-medium"
                >
                  Paste your {selectedGuide.keyLabel}
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
                <p className="text-muted-foreground text-meta">
                  {saved
                    ? "Key saved. Paste it here to check the connection. "
                    : ""}
                  Kept only while this page is open.
                </p>
              </div>
            ) : (
              <p className="text-muted-foreground text-meta">
                The key appears once. Copy it into {chosenAppName}.
              </p>
            )}
            {!canCreate && (
              <p className="text-muted-foreground text-meta">
                Your access plan does not let you create keys. Ask the gateway
                owner for access.
              </p>
            )}
            {keyError && (
              <p role="alert" className="text-destructive text-meta">
                {keyError}
              </p>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {!reveal && (
              <Button
                variant={selectedCredentialId === "new" ? "default" : "outline"}
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
            <div role="status" aria-label="Loading key access" aria-busy="true">
              <Skeleton className="h-16 w-full" />
            </div>
          ) : !model ? (
            <div className="bg-muted rounded-control text-meta p-4">
              <p className="font-medium">
                {hasChatAccess
                  ? "Chat is unavailable right now. Try again later."
                  : "This key has no chat models. Choose another key or ask the owner to update its access."}
              </p>
            </div>
          ) : null}
          {testResult && (
            <div
              role="status"
              className={cn(
                "rounded-control text-meta space-y-1 p-3",
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
                  className="text-link inline-flex items-center underline underline-offset-4 max-sm:min-h-11"
                  href={`/usage/?receipt=${encodeURIComponent(testResult.request_id)}`}
                >
                  View request receipt
                </a>
              )}
            </div>
          )}
          {testError && (
            <div role="alert" className="text-destructive text-meta">
              <p>{testError}</p>
              {testHttpStatus !== null && (
                <details className="mt-2">
                  <summary>Technical details</summary>
                  <p>HTTP {testHttpStatus}</p>
                </details>
              )}
            </div>
          )}
        </div>
      )}
      <Sheet open={notesOpen} onOpenChange={setNotesOpen}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>Setup notes</SheetTitle>
            <SheetDescription>
              Version notes and network caveats for {chosenAppName}.
            </SheetDescription>
          </SheetHeader>
          <div className="space-y-4 px-4 pb-4">
            <ol className="text-body list-decimal space-y-1 pl-5">
              {selectedGuide.steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            <p className="text-muted-foreground text-meta">
              The gateway check verifies the route and key. It does not verify
              the app.
            </p>
            {selectedGuide.note ? (
              <p className="text-muted-foreground text-meta">{selectedGuide.note}</p>
            ) : null}
            {model && selectedGuide.snippet ? (
              <CodeBlock
                language={selectedGuide.snippet.language}
                code={selectedGuide.snippet.code}
              />
            ) : null}
            <a
              className="text-link text-meta inline-flex items-center gap-1 underline underline-offset-4 max-sm:min-h-11"
              href={selectedGuide.docs}
              target="_blank"
              rel="noreferrer"
            >
              {chosenAppName} docs
              <ArrowUpRight aria-hidden="true" className="size-3.5" />
            </a>
          </div>
        </SheetContent>
      </Sheet>
    </Card>
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
  const [audioError, setAudioError] = useState<"media" | "playback" | "unavailable" | null>(
    null,
  );
  const [audioSrc, setAudioSrc] = useState(creation.download_url ?? undefined);
  const [isPlaying, setIsPlaying] = useState(false);
  const [audioLoading, setAudioLoading] = useState(false);
  const audioLoadLock = useRef(false);
  const audioRef = useRef<HTMLAudioElement>(null);

  const resetKey = `${creation.asset_id}:${creation.download_url ?? ""}:${posterUrl ?? ""}`;
  const [previousResetKey, setPreviousResetKey] = useState(resetKey);
  if (previousResetKey !== resetKey) {
    setPreviousResetKey(resetKey);
    setImageFailed(false);
    setPosterFailed(false);
    setVideoFailed(false);
    setAudioError(null);
    setIsPlaying(false);
    setAudioSrc(creation.download_url ?? undefined);
  }

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
        sizeBytes={creation.size_bytes}
        visible={visible}
        status={
          audioLoading ? (
            <span role="status">Loading audio…</span>
          ) : audioError ? (
            <span role="status" className="text-destructive">
              {audioError === "unavailable"
                ? "Audio isn't available. Open it in Library."
                : audioError === "media"
                  ? "Audio preview unavailable. Play to retry."
                  : "Playback didn't start. Try again."}
            </span>
          ) : null
        }
        center={
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
              // On a phone the tile opens the clip in Library instead.
              className={cn(playDiscButton, "max-sm:hidden")}
              disabled={audioLoading || audioError === "unavailable"}
              aria-label={audioLoading ? "Loading audio…" : `${isPlaying ? "Pause" : "Play"} ${title}`}
              onClick={async () => {
                const audio = audioRef.current;
                if (!audio || audioLoadLock.current || audioError === "unavailable") return;
                if (isPlaying) audio.pause();
                else {
                  audioLoadLock.current = true;
                  setAudioLoading(true);
                  try {
                    if (audioError === "media") {
                      const renewed = await homeApi.mediaDownload(
                        creation.asset_id,
                      );
                      if (!renewed.download_url?.trim()) {
                        setAudioError("unavailable");
                        return;
                      }
                      setAudioSrc(renewed.download_url);
                      audio.src = renewed.download_url;
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
                  } finally {
                    audioLoadLock.current = false;
                    setAudioLoading(false);
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
      <p className="text-badge">Preview unavailable</p>
      <p className="text-badge underline">Open in Library</p>
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
        <MediaBadge
          icon={AudioLines}
          className="absolute bottom-2 left-2 z-10"
        />
        <Link prefetch={false}
          href={`/library/?asset=${encodeURIComponent(creation.asset_id)}`}
          aria-label={linkLabel}
          className="rounded-media absolute inset-0"
        />
      </li>
    );
  }
  return (
    <li className="min-w-0">
      <Link prefetch={false}
        href={`/library/?asset=${encodeURIComponent(creation.asset_id)}`}
        aria-label={linkLabel}
        className={tile}
      >
        {body}
        {creation.kind === "video" ? (
          <MediaBadge icon={Video} className="absolute bottom-2 left-2" />
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
  } = useQuery(recentCreationsQuery);
  // A creation whose file the gateway no longer stores belongs in Library, not Recent.
  const recentCreations = (creations?.assets ?? [])
    .filter(hasHomeCreationContent)
    .filter((creation) => creation.available !== false)
    .slice(0, 4);
  return (
    <section aria-labelledby="recent-heading">
      <SectionHeading
        id="recent-heading"
        title="Recent"
        action={
          <Link prefetch={false}
            className="text-link text-meta inline-flex items-center gap-0.5 font-medium underline-offset-4 hover:underline max-sm:min-h-11"
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
          className={cn("grid grid-cols-4", mediaGridGap)}
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
          message="We couldn't tell why your recent creations didn't load. Try again."
          onRetry={() => void refetchCreations()}
          retrying={creationsFetching}
        />
      ) : (
        <>
          {!recentCreations.length && !making.length && !creations?.partial ? (
            <p className="text-muted-foreground text-meta">Your creations will appear here.</p>
          ) : null}
          {creations?.partial ? (
            <p role="status" className="text-muted-foreground text-meta mb-2">
              Recent results may be incomplete.
            </p>
          ) : null}
          {/* Four across at every width: on a phone, a strip of the
              household's latest work right under the tasks (Apple Home's
              cameras sit on the first screen). */}
          <ul className={cn("grid grid-cols-4", mediaGridGap)}>
            {making.slice(0, 2).map((receipt) => {
              const kind = inFlightKind(receipt);
              if (!kind) return null;
              const at = receipt.accepted_at_ms
                ? receipt.accepted_at_ms / 1000
                : receipt._stored_at;
              return (
                <li key={receipt.request_id}>
                  <MakingTile
                    kind={kind}
                    startedAt={at}
                    risk={runningRisk(receipt, views)}
                  />
                </li>
              );
            })}
            {recentCreations
              .slice(0, 4 - Math.min(making.length, 2))
              .map((creation) => (
                <CreationPreview
                  key={`${creation.asset_id}:${creation.download_url ?? ""}:${creation.poster_asset_id ?? ""}`}
                  creation={creation}
                />
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
  useQuery(recentCreationsQuery);
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
    for (const row of capabilityTiles(home.capabilities, home.aliases))
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
              href: `/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/" : window.location.pathname + window.location.search)}`,
            }}
          />
        </>
      ) : (
        <>
          <PageHeader
            title="Home"
            tight
            actions={
              // Connecting an app is secondary, but one tap from the top
              // (Apple Home puts "+" in the top bar).
              // A quiet link, so it does not compete with the title (D1.6).
              <Button
                variant="link"
                size="sm"
                asChild
                className="max-sm:min-h-11 lg:hidden"
              >
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
                <Link prefetch={false}
                  href="/status/"
                  className="text-link inline-flex items-center gap-0.5 font-medium underline-offset-4 hover:underline max-sm:min-h-11"
                >
                  {isOwner ? "Open Status" : "See what is happening"}
                  <ChevronRight aria-hidden="true" className="size-4" />
                </Link>
              ) : home.has_access_plan === false ? (
                // No plan: nothing can run until the owner chooses one (design §3.4).
                isOwner ? (
                  <Link
                    href="/people?tab=plans"
                    className="text-link inline-flex items-center gap-0.5 font-medium underline-offset-4 hover:underline max-sm:min-h-11"
                  >
                    You don&apos;t have an access plan yet. Choose one
                    <ChevronRight aria-hidden="true" className="size-4" />
                  </Link>
                ) : (
                  <span className="text-muted-foreground">
                    {`You can't use models yet. Ask ${home.gateway?.owner_name?.trim() || "the gateway owner"} to choose an access plan for you.`}
                  </span>
                )
              ) : verdict.tone === "off" && !isOwner ? (
                // A member cannot turn anything on; say who can.
                <span className="text-muted-foreground">
                  {home.gateway?.owner_name?.trim()
                    ? `Ask ${home.gateway.owner_name.trim()} to turn something on.`
                    : "Ask the gateway owner to turn something on."}
                </span>
              ) : verdict.tone === "off" && isOwner ? (
                // Nothing turned on: the owner's next step is an engine.
                <Link prefetch={false}
                  href="/engines/"
                  className="text-link inline-flex items-center gap-0.5 font-medium underline-offset-4 hover:underline max-sm:min-h-11"
                >
                  Add an engine
                  <ChevronRight aria-hidden="true" className="size-4" />
                </Link>
              ) : null}
            </StatusLine>
          </PageHeader>
          {/* Sections 32 apart (--space-section), the Connect card 32 to the
              side on desktop and 32 below on phones and tablets. */}
          <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,21rem)]">
            <div className="flex min-w-0 flex-col gap-(--space-section) lg:col-start-1 lg:row-start-1">
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
                owner={isOwner}
                statusExplains={verdict.tone === "off"}
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

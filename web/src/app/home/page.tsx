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
import { availabilityReason } from "@/lib/status-copy";
import { useViewer } from "@/components/shell/session";
import { accountApi, type AccountApp } from "@/lib/account-api";
import { api, ApiError, queryKeys } from "@/lib/api";
import { engineShortName } from "@/lib/health-presentation";
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
  EmptyState,
  ErrorState,
  SecretReveal,
} from "@/components/product";
import {
  availabilityState,
  capabilityMeta,
  evidenceStamp,
  EvidenceTime,
  SectionHeading,
  StateLabel,
  Verdict,
  type EvidenceStamp,
} from "@/components/product/readiness";
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
  type HomeCapability,
  type HomeCreation,
  type HomeSnapshot,
  hasHomeCreationContent,
} from "./home-api";
import { chatAlias, recipeFor, type Client } from "./recipes";
import {
  capabilityTiles,
  homeVerdict,
  oldestCheck,
  spendingNotice,
} from "./presentation";
import { Notice } from "@/components/product/notice";

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

/** Tiles open Try on the capability, with its recommended model selected. */
function capabilityHref(row: HomeCapability): string {
  const tab = ["music", "sfx"].includes(row.capability)
    ? "audio"
    : row.capability === "voice"
      ? "speech"
      : row.capability;
  const params = new URLSearchParams({ capability: tab });
  const model = row.use_name ?? row.model;
  if (model) params.set("model", model);
  return `/try/?${params.toString()}`;
}

function firstWords(value?: string | null): string | null {
  const words = value?.trim().split(/\s+/).filter(Boolean) ?? [];
  if (!words.length) return null;
  return words.slice(0, 12).join(" ");
}

function HomeLoading() {
  return (
    <div
      role="status"
      aria-label="Loading Home"
      aria-busy="true"
      className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]"
    >
      <div className="space-y-8">
        <Skeleton className="h-9 w-3/4" />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
            <Skeleton className="h-28 w-full rounded-panel" key={i} />
          ))}
        </div>
      </div>
      <Skeleton className="h-96 w-full rounded-panel" />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Capabilities                                                        */
/* ------------------------------------------------------------------ */

type EngineHint = { name: string; serviceId: string } | null;

function useOwnerEngineHints(enabled: boolean) {
  const { data: status } = useQuery({
    queryKey: ["foundation", "health"],
    queryFn: api.status,
    enabled,
    refetchInterval: 60_000,
  });
  const { data: registry } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
    enabled,
  });
  return (label: string): EngineHint => {
    if (!enabled) return null;
    const service = status?.current_health?.services.find(
      (item) =>
        item.enabled &&
        item.tone !== "ok" &&
        (item.capability_labels ?? [item.capability_title]).some(
          (value) => value?.toLowerCase() === label.toLowerCase(),
        ),
    );
    if (!service) return null;
    return {
      name: engineShortName(registry?.services[service.service_id], service),
      serviceId: service.service_id,
    };
  };
}

function CapabilityTile({
  row,
  verdictCheck,
}: {
  row: HomeCapability;
  verdictCheck: EvidenceStamp | null;
}) {
  const meta = capabilityMeta(row.icon_key ?? row.capability);
  const label = row.capability_title ?? meta.label;
  const state = availabilityState(row.status, row.reason_code);
  const stamp = evidenceStamp(row.availability?.evidence);
  // A tile repeats a time only when it differs from the verdict's check.
  const ownStamp =
    stamp &&
    !(
      verdictCheck &&
      stamp.kind === "checked" &&
      Math.abs(stamp.at - verdictCheck.at) < 300
    )
      ? stamp
      : null;
  const Icon = meta.icon;
  const reason = row.available
    ? undefined
    : availabilityReason(row.status, row.reason_code);
  const body = (
    <span className="flex min-w-0 items-center gap-3 md:flex-col md:items-start md:gap-3">
      <span
        role="img"
        aria-label={label}
        className={cn(
          "flex size-9 shrink-0 items-center justify-center rounded-control",
          row.available
            ? cn(meta.tint, meta.tone)
            : "bg-muted text-muted-foreground",
        )}
      >
        <Icon aria-hidden="true" className="size-4.5" />
      </span>
      <span className="block min-w-0">
        <span className="block truncate font-semibold">{label}</span>
        <StateLabel state={state} className="mt-0.5" />
        {ownStamp ? (
          <EvidenceTime stamp={ownStamp} className="mt-0.5 block text-xs" />
        ) : null}
      </span>
    </span>
  );
  if (!row.available)
    return (
      <li
        className="bg-muted/50 flex min-w-0 flex-col rounded-panel border border-dashed p-3 md:p-3.5"
        title={reason}
      >
        {body}
        {reason ? <span className="sr-only">{reason}</span> : null}
      </li>
    );
  return (
    <li className="min-w-0">
      <Link
        href={capabilityHref(row)}
        aria-label={label}
        className="bg-card hover:border-input flex h-full min-w-0 flex-col rounded-panel border p-3 transition-colors md:p-3.5"
      >
        {body}
      </Link>
    </li>
  );
}

function Capabilities({
  tiles,
  verdictCheck,
  owner,
}: {
  tiles: HomeCapability[];
  verdictCheck: EvidenceStamp | null;
  owner: boolean;
}) {
  return (
    <section aria-labelledby="capabilities-heading">
      <SectionHeading id="capabilities-heading" title="What you can use" />
      {!tiles.length ? (
        <div className="bg-card flex flex-wrap items-center justify-between gap-3 rounded-panel border border-dashed p-5">
          <p className="text-muted-foreground text-sm">
            {owner
              ? "Connect an engine or a provider to turn on chat, images and more."
              : "Ask the gateway owner to connect a model and give you access."}
          </p>
          {owner ? (
            <Button asChild size="sm">
              <Link href="/engines/">Add an engine</Link>
            </Button>
          ) : null}
        </div>
      ) : (
        <ul className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {tiles.map((row) => (
            <CapabilityTile
              key={row.capability}
              row={row}
              verdictCheck={verdictCheck}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Connect an app                                                      */
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
                <span className="min-w-0 flex-1 truncate text-sm font-medium group-hover:underline">
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

function Connect({ home }: { home: HomeSnapshot }) {
  const queryClient = useQueryClient();
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
          : "The server could not be reached. Try again.",
      );
    } finally {
      setTesting(false);
    }
  }
  return (
    <section
      id="connect"
      aria-labelledby="connect-heading"
      className="bg-card rounded-panel border"
    >
      <div className="space-y-5 p-5">
        <h2 id="connect-heading" className="text-lg font-semibold">
          Connect an app
        </h2>
        <CopyField label="Gateway address" value={home.api_base_url} />
        <YourApps home={home} />
        <div>
          <h3 className="text-muted-foreground mb-2 text-sm font-medium">
            Add an app
          </h3>
          <div className="grid grid-cols-2 gap-2">
            {([...primaryAppChoices, "Other"] as AppChoice[]).map((name) => (
              <button
                key={name}
                type="button"
                className={cn(
                  "flex min-h-11 items-center gap-2 rounded-control border px-3 text-left text-sm font-medium transition-colors disabled:opacity-50",
                  appChoice === name
                    ? "border-ring bg-accent text-accent-foreground"
                    : "bg-card hover:border-input",
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
                {name === "Other" ? (
                  <Plus aria-hidden="true" className="size-4 shrink-0" />
                ) : null}
                <span className="truncate">{name}</span>
              </button>
            ))}
          </div>
        </div>
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
            ) : null}
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
                  Your access plan does not let you create keys. Ask the
                  gateway owner for access.
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
                  Choose another key, or ask the owner to update this app’s
                  access.
                </p>
              </div>
            ) : null}
            {testResult && (
              <div
                role="status"
                className={cn(
                  "space-y-1 rounded-control p-3 text-sm",
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
      <div className="bg-capability-voice/12 flex size-full flex-col justify-between p-3">
        <p className="line-clamp-3 text-sm font-medium break-words">{title}</p>
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
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="icon"
            className="relative z-10 size-11 rounded-full"
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
                    audioError === "media" || audio.error ? "media" : "playback",
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
          </Button>
          {audioError ? (
            <p role="status" className="text-destructive text-xs">
              {audioError === "media"
                ? "Audio preview unavailable. Play to retry."
                : "Playback didn't start. Try again."}
            </p>
          ) : null}
        </div>
      </div>
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
          className="absolute inset-0 rounded-media"
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
          <span className="bg-chrome/80 text-chrome-foreground absolute bottom-2 left-2 flex size-7 items-center justify-center rounded-full">
            <Play aria-hidden="true" className="size-3.5" />
          </span>
        ) : null}
      </Link>
    </li>
  );
}

function Recent() {
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
  if (!creationsPending && !creationsError && recentCreations.length === 0) {
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
          className="grid grid-cols-2 gap-2 md:grid-cols-4"
        >
          {[0, 1, 2, 3].map((item) => (
            <Skeleton key={item} className="aspect-square w-full rounded-media" />
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
          <ul className="grid grid-cols-2 gap-2 md:grid-cols-4">
            {recentCreations.map((creation) => (
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
    home?.authenticated && ["owner", "superadmin", "admin"].includes(home.user.role),
  );
  const tiles =
    home && home.authenticated
      ? capabilityTiles(home.capabilities, home.aliases)
      : [];
  const verdict = homeVerdict(tiles);
  const notice =
    home && home.authenticated ? spendingNotice(home.receipts ?? []) : null;
  // Someone with no app that has ever made a request starts by connecting one.
  const newcomer = Boolean(
    home?.authenticated &&
      !home.apps.some((app) =>
        app.credentials.some((key) => typeof key.last_used_at === "number"),
      ),
  );
  const check = oldestCheck(tiles);
  const hintFor = useOwnerEngineHints(isOwner && viewer.admin);
  const hints = [
    ...new Map(
      tiles
        .filter((row) => !row.available)
        .map((row) =>
          hintFor(row.capability_title ?? capabilityMeta(row.capability).label),
        )
        .filter((hint): hint is NonNullable<EngineHint> => Boolean(hint))
        .map((hint) => [hint.serviceId, hint]),
    ).values(),
  ];
  return (
    <div className="min-w-0">
      <h1 className="sr-only">Home</h1>
      {homePending ? (
        <HomeLoading />
      ) : homeError ? (
        <ErrorState
          heading="Couldn’t load Home"
          message="Your connection settings could not be loaded. Try again."
          onRetry={() => void refetchHome()}
          retrying={homeFetching}
        />
      ) : !home.authenticated ? (
        <EmptyState
          title="Sign in to use your AI"
          description="Get your connection settings and connect the apps you already use."
          action={
            <Button asChild>
              <a
                href={`/ui/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/ui/home/" : window.location.pathname + window.location.search)}`}
              >
                Sign in
              </a>
            </Button>
          }
        />
      ) : (
        <div className="grid items-start gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
          <div className="min-w-0 space-y-6 lg:col-start-1 lg:row-start-1">
            <Verdict
              tone={verdict.tone}
              detail={
                check || hints.length ? (
                  <>
                    {check ? (
                      <EvidenceTime stamp={check} className="text-base" />
                    ) : null}
                    {hints.length > 2 ? (
                      <Link
                        href="/status/"
                        className="text-link inline-flex min-h-11 items-center gap-0.5 font-medium underline-offset-4 hover:underline sm:min-h-0"
                      >
                        {hints.length} engines need you
                        <ChevronRight aria-hidden="true" className="size-4" />
                      </Link>
                    ) : null}
                    {(hints.length > 2 ? [] : hints).map((hint) => (
                      <Link
                        key={hint.serviceId}
                        href={`/engines/?service=${encodeURIComponent(hint.serviceId)}`}
                        className="text-link inline-flex min-h-11 items-center gap-0.5 font-medium underline-offset-4 hover:underline sm:min-h-0"
                      >
                        Inspect {hint.name}
                        <ChevronRight aria-hidden="true" className="size-4" />
                      </Link>
                    ))}
                  </>
                ) : null
              }
            >
              {verdict.text}
            </Verdict>
            {notice ? (
              <Notice
                title="Your paid requests are being refused."
                action={
                  <a
                    href={`/r/${encodeURIComponent(notice.requestId)}`}
                    className="text-link inline-flex min-h-11 items-center font-medium underline-offset-4 hover:underline sm:min-h-0"
                  >
                    Open receipt
                  </a>
                }
              >
                {notice.message} Last refused{" "}
                <RelativeTime ts={notice.at} fallback="" />.{" "}
                {isOwner
                  ? "Raise the cap in the access plan to resume them."
                  : "Ask the gateway owner to raise the cap."}
              </Notice>
            ) : null}
          </div>
          <div
            className={cn(
              "min-w-0 lg:col-start-1 lg:row-start-2",
              newcomer ? "order-2 lg:order-none" : "order-1 lg:order-none",
            )}
          >
            <Capabilities
              tiles={tiles}
              verdictCheck={check}
              owner={isOwner && viewer.admin}
            />
          </div>
          <div
            className={cn(
              "min-w-0 lg:col-start-2 lg:row-span-3 lg:row-start-1",
              newcomer ? "order-1 lg:order-none" : "order-2 lg:order-none",
            )}
          >
            <Connect home={home} />
          </div>
          <div className="order-3 min-w-0 lg:order-none lg:col-start-1 lg:row-start-3">
            <Recent />
          </div>
        </div>
      )}
    </div>
  );
}

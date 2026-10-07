"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
  AudioLines,
  Circle,
  ImageIcon,
  KeyRound,
  Loader2,
  PlugZap,
  ShieldCheck,
  Video,
  Music2,
  Waves,
  ListFilter,
  Pause,
  Play,
} from "lucide-react";
import { formatAppName } from "@/lib/format";
import { availabilityReason, statusSummaryCopy } from "@/lib/status-copy";
import { useOwnerAlertTarget } from "@/components/shell/use-owner-alert";
import { accountApi, type AccountApp } from "@/lib/account-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  CapabilityIcon,
  CodeBlock,
  CopyField,
  EmptyState,
  ErrorState,
  SecretReveal,
} from "@/components/product";
import type { Capability } from "@/components/product/capability-icon";
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
import { capabilityLabels } from "@/lib/terms";
import { capabilityTiles } from "./presentation";

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
type AppChoice = Client | "Other";
const capabilityCopy: Record<
  string,
  { label: string; description: string; icon: Capability }
> = {
  chat: {
    label: "Chat",
    description: "Ask questions and work with text.",
    icon: "chat",
  },
  image: {
    label: "Images",
    description: "Make an image from your idea.",
    icon: "image",
  },
  video: {
    label: "Video",
    description: "Turn an idea into a short video.",
    icon: "video",
  },
  voice: {
    label: capabilityLabels.speech,
    description: "Read your text aloud.",
    icon: "voice",
  },
  speech: {
    label: "Speech",
    description: "Read your text aloud.",
    icon: "voice",
  },
  music: {
    label: "Music",
    description: "Create music from a prompt.",
    icon: "voice",
  },
  sfx: {
    label: "Sound effects",
    description: "Create sounds for your projects.",
    icon: "voice",
  },
  transcribe: {
    label: capabilityLabels.transcribe,
    description: "Turn a recording into text.",
    icon: "transcribe",
  },
  decide: {
    label: capabilityLabels.decisions,
    description: "Choose between options.",
    icon: "decide",
  },
  embed: {
    label: capabilityLabels.embeddings,
    description: "Turn text into numbers for search.",
    icon: "embed",
  },
  rerank: {
    label: capabilityLabels.reranking,
    description: "Sort search results by relevance.",
    icon: "embed",
  },
  audio: {
    label: "Audio",
    description: "Create sound from a prompt.",
    icon: "voice",
  },
};
const capabilityTint: Record<Capability, string> = {
  chat: "bg-capability-chat/5 border-capability-chat/20",
  image: "bg-capability-image/5 border-capability-image/20",
  video: "bg-capability-video/5 border-capability-video/20",
  voice: "bg-capability-voice/5 border-capability-voice/20",
  transcribe: "bg-capability-transcribe/5 border-capability-transcribe/20",
  decide: "bg-capability-decide/5 border-capability-decide/20",
  embed: "bg-capability-embed/5 border-capability-embed/20",
};

function formatMilliseconds(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

function capabilityHref(capability: string): string {
  const filter = ["music", "sfx"].includes(capability) ? "audio" : capability;
  return `/models/?capability=${encodeURIComponent(filter)}`;
}

function firstWords(value?: string | null): string | null {
  const words = value?.trim().split(/\s+/).filter(Boolean) ?? [];
  if (!words.length) return null;
  return words.slice(0, 12).join(" ");
}

function HomeCapabilityIcon({ row }: { row: HomeCapability }) {
  const key = row.icon_key ?? row.capability;
  const special = { music: Music2, sfx: Waves, rerank: ListFilter };
  const Icon = special[key as keyof typeof special];
  if (Icon)
    return (
      <span
        role="img"
        aria-label={row.capability_title ?? key}
        className={`inline-flex size-8 shrink-0 items-center justify-center rounded-md [&>svg]:size-4 ${row.available ? "text-capability-voice bg-capability-voice/10" : "bg-muted text-muted-foreground"}`}
      >
        <Icon aria-hidden="true" />
      </span>
    );
  return (
    <CapabilityIcon
      capability={
        key === "speech" ? "voice" : (capabilityCopy[key]?.icon ?? "chat")
      }
      className={row.available ? undefined : "bg-muted text-muted-foreground"}
    />
  );
}

function HomeLoading() {
  return (
    <div
      role="status"
      aria-label="Loading Home"
      aria-busy="true"
      className="space-y-8"
    >
      <Skeleton className="h-8 w-56" />
      <div className="space-y-4">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-52 w-full" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Skeleton className="h-32 w-full" key={i} />
        ))}
      </div>
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
    } catch {
      setTestError(
        "The gateway check could not finish. Check your key and try again.",
      );
    } finally {
      setTesting(false);
    }
  }
  return (
    <section
      id="connect"
      aria-labelledby="connect-heading"
      className="border-primary/20 bg-card overflow-hidden rounded-xl border"
    >
      <div className="p-4 sm:p-6">
        <div className="mb-4 flex items-start gap-3">
          <span className="bg-primary/10 text-primary flex size-11 shrink-0 items-center justify-center rounded-lg">
            <PlugZap aria-hidden="true" className="size-5" />
          </span>
          <div>
            <h2 id="connect-heading" className="text-xl font-semibold">
              Connect an app
            </h2>
            <p className="text-muted-foreground mt-1 text-sm">
              One address. A key for each app.
            </p>
          </div>
        </div>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
            {([...primaryAppChoices, "Other"] as AppChoice[]).map((name) => (
              <button
                key={name}
                type="button"
                className={`border-border hover:border-primary/50 focus-visible:ring-ring min-h-11 rounded-lg border px-3 py-2 text-left text-sm font-medium focus-visible:ring-2 ${appChoice === name ? "border-primary bg-primary/5 text-primary" : "bg-background"}`}
                aria-pressed={appChoice === name}
                disabled={choiceLocked}
                onClick={() => {
                  setAppChoice(name);
                  setClient(name === "Other" ? "Python" : name);
                  setTestResult(null);
                  setTestError(null);
                }}
              >
                {name}
              </button>
            ))}
          </div>
        </div>
        {appChoice && (
          <div className="mt-5 space-y-4">
            <div className="grid min-w-0 gap-3 lg:grid-cols-3">
              <CopyField
                label="Base URL"
                value={selectedRecipe.fields[0]?.value ?? home.api_base_url}
              />
              {model && <CopyField label="Model" value={model} />}
            </div>
            <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
              <div className="min-w-0 space-y-3">
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
                      New app · default access
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
                    Create a key, then copy it to your app. It appears only
                    once.
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
                    selectedCredentialId !== "new" &&
                    !reveal &&
                    pastedKey.trim()
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
                    <PlugZap aria-hidden="true" />
                  )}
                  {testing ? "Checking…" : "Check connection"}
                </Button>
                <Button variant="ghost" onClick={() => setNotesOpen(true)}>
                  Setup notes
                </Button>
              </div>
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
              <div className="bg-muted rounded-lg p-5 text-sm">
                <p className="font-medium">
                  This key does not include the default model
                </p>
                <p className="text-muted-foreground mt-2">
                  Choose another key, or ask the owner to update this app’s
                  access.
                </p>
              </div>
            ) : null}
            {testResult && (
              <div role="status" className="space-y-2">
                <p
                  className={`flex items-center gap-2 text-sm ${testResult.ok ? "text-success" : "text-destructive"}`}
                >
                  {testResult.ok ? (
                    <ShieldCheck aria-hidden="true" className="size-4" />
                  ) : (
                    <Circle aria-hidden="true" className="size-4" />
                  )}
                  {testResult.ok
                    ? "Gateway responded with this key"
                    : "Connection check failed"}
                  {testResult.latency_ms != null
                    ? ` · ${formatMilliseconds(testResult.latency_ms)}`
                    : ""}
                </p>
                <p className="text-muted-foreground text-sm">
                  {testResult.reason}
                </p>
                {testResult.request_id && (
                  <a
                    className="text-primary inline-flex min-h-11 items-center text-sm underline underline-offset-4"
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
              className="text-primary inline-flex min-h-11 items-center gap-1 text-sm underline underline-offset-4"
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

function Capabilities({
  capabilities,
  aliases,
}: {
  capabilities: HomeCapability[];
  aliases: HomeSnapshot["aliases"];
}) {
  const tiles = capabilityTiles(capabilities, aliases);
  return (
    <section aria-labelledby="capabilities-heading">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="capabilities-heading" className="text-lg font-semibold">
          What you can use
        </h2>
      </div>
      {!tiles.length ? (
        <EmptyState
          title="No tasks are available yet"
          description="Ask the owner to connect a model and update your access."
          action={
            <Button asChild variant="outline">
              <Link href="/models/">View models and access</Link>
            </Button>
          }
        />
      ) : (
        <div className="mt-4 flex flex-wrap gap-2">
          {tiles.map((capability) => {
            const copy = capabilityCopy[capability.capability];
            if (!copy) return null;
            const label = capability.capability_title ?? copy.label;
            const reason = availabilityReason(
              capability.status,
              capability.reason_code,
            );
            return (
              <Link
                key={capability.capability}
                href={capabilityHref(capability.capability)}
                title={capability.available ? undefined : reason}
                aria-label={
                  reason && !capability.available
                    ? `${label}: ${reason}`
                    : label
                }
                className={`hover:border-primary/50 focus-visible:ring-ring inline-flex min-h-11 max-w-full min-w-0 items-center gap-2 rounded-full border px-3 py-1.5 transition-colors focus-visible:ring-2 ${capability.available ? capabilityTint[copy.icon] : "border-border bg-muted/30 text-muted-foreground"}`}
              >
                <HomeCapabilityIcon row={capability} />
                <span className="truncate text-sm font-medium">{label}</span>
              </Link>
            );
          })}
        </div>
      )}
    </section>
  );
}

function CreationMedia({
  creation,
  title,
}: {
  creation: HomeCreation;
  title: string;
}) {
  const Icon =
    creation.kind === "video"
      ? Video
      : creation.kind === "audio"
        ? AudioLines
        : ImageIcon;
  const { data: poster } = useQuery({
    queryKey: ["library", "home-recent-poster", creation.poster_asset_id],
    enabled: creation.kind === "video" && !!creation.poster_asset_id,
    queryFn: () => homeApi.mediaDownload(creation.poster_asset_id!),
    retry: false,
  });
  const posterUrl = poster?.download_url;
  const [posterFailed, setPosterFailed] = useState(false);
  const [videoFailed, setVideoFailed] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);

  if (creation.kind === "image" && creation.download_url) {
    return (
      // Recent uses the Library API's signed thumbnail-capable download URL.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={creation.download_url}
        alt=""
        loading="lazy"
        className="size-full object-cover transition-transform group-hover:scale-[1.02] motion-reduce:transform-none"
      />
    );
  }

  if (creation.kind === "video" && posterUrl && !posterFailed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={posterUrl}
        alt=""
        loading="lazy"
        onError={() => setPosterFailed(true)}
        className="size-full object-cover transition-transform group-hover:scale-[1.02] motion-reduce:transform-none"
      />
    );
  }

  if (creation.kind === "video" && creation.download_url && !videoFailed) {
    return (
      <video
        src={creation.download_url}
        preload="metadata"
        muted
        playsInline
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
      <div className="flex size-full flex-col items-center justify-center gap-4 p-4">
        <AudioLines aria-hidden="true" className="size-12" />
        <audio
          ref={audioRef}
          preload="metadata"
          src={creation.download_url}
          onPlay={() => setIsPlaying(true)}
          onPause={() => setIsPlaying(false)}
          onEnded={() => setIsPlaying(false)}
          className="sr-only"
        />
        <Button
          type="button"
          variant="secondary"
          size="icon"
          className="size-11"
          aria-label={`${isPlaying ? "Pause" : "Play"} ${title}`}
          onClick={async () => {
            const audio = audioRef.current;
            if (!audio) return;
            if (isPlaying) audio.pause();
            else {
              try {
                await audio.play();
              } catch {
                setIsPlaying(false);
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
      </div>
    );
  }

  return (
    <div
      role="img"
      aria-label={`${creation.kind[0].toUpperCase()}${creation.kind.slice(1)} preview unavailable`}
      className="bg-muted text-muted-foreground flex size-full flex-col items-center justify-center gap-2 p-4 text-center"
    >
      <Icon aria-hidden="true" className="size-8" />
    </div>
  );
}

function CreationPreview({ creation }: { creation: HomeCreation }) {
  const label =
    creation.kind === "video"
      ? "Video"
      : creation.kind === "audio"
        ? "Audio"
        : "Image";
  const prompt = firstWords(creation.prompt);
  const title = prompt ?? label;
  const cardClass =
    "border-border bg-card hover:border-primary/40 focus-visible:ring-ring group flex min-w-0 flex-col overflow-hidden rounded-xl border focus-visible:ring-2";
  const body = (
    <>
      <div
        className={`flex aspect-[4/3] w-full items-center justify-center overflow-hidden ${creation.kind === "video" || (creation.kind === "audio" && !creation.download_url) ? "bg-muted text-muted-foreground" : creation.kind === "audio" ? "bg-capability-voice/10 text-capability-voice" : "bg-capability-image/10 text-capability-image"}`}
      >
        <CreationMedia creation={creation} title={title} />
      </div>
      <div className="min-w-0 p-3">
        {prompt && <p className="text-sm font-medium">{prompt}</p>}
      </div>
    </>
  );
  if (creation.kind === "audio") {
    return <article className={cardClass}>{body}</article>;
  }
  return (
    <Link href="/library/" className={cardClass}>
      {body}
    </Link>
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
  const recentCreations = (creations?.assets ?? [])
    .filter(hasHomeCreationContent)
    .slice(0, 4);
  if (!creationsPending && !creationsError && recentCreations.length === 0) {
    return null;
  }
  return (
    <section aria-labelledby="recent-heading" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="recent-heading" className="text-lg font-semibold">
          Recent
        </h2>
        <Link
          className="text-primary inline-flex min-h-11 items-center text-sm underline underline-offset-4"
          href="/library/"
        >
          View library
        </Link>
      </div>
      {creationsPending ? (
        <div
          role="status"
          aria-label="Loading recent creations"
          aria-busy="true"
          className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
        >
          {[0, 1, 2, 3].map((item) => (
            <Skeleton key={item} className="h-48 w-full rounded-xl" />
          ))}
        </div>
      ) : creationsError ? (
        <ErrorState
          message="Recent creations could not be loaded. Try again."
          onRetry={() => void refetchCreations()}
          retrying={creationsFetching}
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {recentCreations.map((creation) => (
            <CreationPreview key={creation.asset_id} creation={creation} />
          ))}
        </div>
      )}
    </section>
  );
}

export default function HomePage() {
  const {
    data: home,
    isPending: homePending,
    isError: homeError,
    refetch: refetchHome,
    isFetching: homeFetching,
  } = useQuery({ queryKey: homeKey, queryFn: homeApi.home });
  const isOwner = home?.authenticated && home.user.role === "owner";
  const ownerAlert = useOwnerAlertTarget(Boolean(isOwner));
  const statusCopy =
    home && "status" in home && home.status.state === "unavailable"
      ? statusSummaryCopy(home.status)
      : undefined;
  return (
    <div className="min-w-0 space-y-4 sm:space-y-6">
      {/* The visible page starts with "Connect an app"; the h1 keeps the page outline for assistive technology. */}
      <h1 className="sr-only">Home</h1>
      {homePending ? (
        <HomeLoading />
      ) : homeError ? (
        <ErrorState
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
        <>
          {isOwner && statusCopy && !ownerAlert && (
            <div
              role="status"
              className="text-muted-foreground flex flex-wrap items-center gap-2 text-sm"
            >
              <span>{statusCopy}</span>
              <Link
                className="text-primary underline underline-offset-4"
                href="/status/"
              >
                Fix in Status
              </Link>
            </div>
          )}
          <Connect home={home} />
          <Capabilities
            capabilities={home.capabilities}
            aliases={home.aliases}
          />
          <Recent />
        </>
      )}
    </div>
  );
}

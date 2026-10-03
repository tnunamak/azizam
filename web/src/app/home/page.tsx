"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
  AudioLines,
  Check,
  Circle,
  ImageIcon,
  KeyRound,
  Loader2,
  PlugZap,
  ShieldCheck,
  Video,
} from "lucide-react";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  CapabilityIcon,
  CodeBlock,
  CopyField,
  EmptyState,
  ErrorState,
  PageHeader,
  RelativeTime,
  SecretReveal,
  StatusPill,
} from "@/components/product";
import type { Capability } from "@/components/product/capability-icon";
import {
  homeApi,
  type ConnectionResult,
  type HomeCapability,
  type HomeAlias,
  type HomeCreation,
  type HomeSnapshot,
  type SetupProgress,
} from "./home-api";
import { chatAlias, clients, recipeFor, type Client } from "./recipes";

const homeKey = ["account", "home"];
const setupKey = ["gateway", "setup-progress"];
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
    label: "Voice",
    description: "Read your text aloud.",
    icon: "voice",
  },
  transcribe: {
    label: "Transcribe",
    description: "Turn a recording into text.",
    icon: "transcribe",
  },
  decide: {
    label: "Decide",
    description: "Make a structured decision.",
    icon: "decide",
  },
  embed: {
    label: "Embed",
    description: "Represent text for search.",
    icon: "embed",
  },
  rerank: {
    label: "Rerank",
    description: "Sort search results by relevance.",
    icon: "embed",
  },
  audio: {
    label: "Audio",
    description: "Create sound from a prompt.",
    icon: "voice",
  },
};
const stepLabels: Record<string, string> = {
  claim_owner: "Claim owner",
  connect_service: "Connect an engine or provider",
  test_request: "Run a test request",
  connect_app: "Connect an app",
  invite_someone: "Invite someone",
};

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
  const [client, setClient] = useState<Client>(clients[0]);
  const [selectedCredentialId, setSelectedCredentialId] = useState("new");
  const [reveal, setReveal] = useState<{ secret: string; id: string } | null>(
    null,
  );
  const [creating, setCreating] = useState(false);
  const [pendingApp, setPendingApp] = useState<AccountApp | null>(null);
  const createLock = useRef(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testKey, setTestKey] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const keys = home.apps
    .flatMap((app) =>
      app.credentials.map((key) => ({ ...key, appName: app.name })),
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

  async function createKey() {
    if (createLock.current) return;
    createLock.current = true;
    setCreating(true);
    setKeyError(null);
    setTestResult(null);
    setSelectedCredentialId("new");
    try {
      const app = pendingApp ?? (await accountApi.createApp(client));
      setPendingApp(app);
      const key = await accountApi.mintKey(app.id, client);
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
    const secret = reveal?.secret || testKey.trim();
    if (!secret || testing || selectedCredentialId === "new") return;
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    setTestKey("");
    try {
      setTestResult(await homeApi.test(secret, selectedCredentialId));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: homeKey }),
        queryClient.invalidateQueries({ queryKey: setupKey }),
      ]);
    } catch {
      setTestError(
        "The connection test could not finish. Check your key and try again.",
      );
    } finally {
      setTesting(false);
    }
  }
  return (
    <section
      aria-labelledby="connect-heading"
      className="border-border rounded-xl border"
    >
      <div className="border-border border-b p-5 sm:p-6">
        <div className="mb-6 flex items-center gap-3">
          <PlugZap aria-hidden="true" className="text-primary size-5" />
          <div>
            <h2 id="connect-heading" className="text-lg font-semibold">
              Connect an app
            </h2>
            <p className="text-muted-foreground mt-1 text-sm">
              One address. A key for each app you use.
            </p>
          </div>
        </div>
        <div className="grid min-w-0 gap-6 lg:grid-cols-2">
          <CopyField label="Base URL" value={home.api_base_url} />
          <div className="min-w-0 space-y-3">
            {reveal ? (
              <SecretReveal
                key={reveal.id}
                secret={reveal.secret}
                onSaved={() => {
                  setReveal(null);
                  setSaved(true);
                }}
              />
            ) : (
              <>
                <p className="text-sm font-medium">Your key</p>
                <p className="text-muted-foreground text-sm">
                  {saved
                    ? "Key saved. It won’t be shown again."
                    : keys.length
                      ? `You have ${keys.length} active ${keys.length === 1 ? "key" : "keys"}. Use your saved key, or create one for this app.`
                      : "Create a key, then copy it to your app. It appears only once."}
                </p>
                <div className="flex flex-wrap items-center gap-3">
                  <Button
                    variant="outline"
                    disabled={creating || !canCreate}
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
                        : `Create key for ${client}`}
                  </Button>
                  <Link
                    className="text-primary text-sm underline underline-offset-4"
                    href="/apps/"
                  >
                    Manage keys
                  </Link>
                </div>
                {!canCreate && (
                  <p className="text-muted-foreground text-sm">
                    Your access plan does not let you create keys. Ask the
                    gateway owner for access.
                  </p>
                )}
              </>
            )}
            {keyError && (
              <p role="alert" className="text-destructive text-sm">
                {keyError}
              </p>
            )}
          </div>
        </div>
        <div className="mt-6 max-w-lg space-y-2">
          <label htmlFor="recipe-key" className="text-sm font-medium">
            Key for these settings
          </label>
          <Select
            value={selectedCredentialId}
            disabled={creating || !!reveal || testing}
            onValueChange={(value) => {
              setSelectedCredentialId(value);
              setTestResult(null);
              setTestKey("");
            }}
          >
            <SelectTrigger id="recipe-key" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="new">New app · your default access</SelectItem>
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
          <p className="text-muted-foreground text-xs">
            Recipes use this key’s access plan. A new app uses your default
            access, which can differ from your other apps.
          </p>
        </div>
      </div>
      <Tabs
        value={client.replaceAll(" ", "-")}
        onValueChange={(value) => {
          const selected = clients.find(
            (name) => name.replaceAll(" ", "-") === value,
          );
          if (selected) setClient(selected);
        }}
        className="min-w-0 gap-0"
      >
        <div className="border-border overflow-x-auto border-b px-4 pt-2 sm:px-6">
          <TabsList
            variant="line"
            aria-label="Client recipes"
            className="gap-3"
          >
            {clients.map((name) => (
              <TabsTrigger
                value={name.replaceAll(" ", "-")}
                key={name}
                className="min-h-11 px-1"
              >
                {name}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        {clients.map((name) => {
          const recipe = recipeFor(
            name,
            home.api_base_url,
            home.public_base_url,
            model ?? "",
          );
          return (
            <TabsContent
              value={name.replaceAll(" ", "-")}
              key={name}
              className="min-w-0 p-5 sm:p-6"
            >
              <div className="mb-4 flex flex-wrap items-baseline justify-between gap-3">
                <h3 className="text-base font-semibold">Connect {name}</h3>
                <a
                  className="text-primary inline-flex min-h-11 items-center gap-1 text-sm underline underline-offset-4"
                  href={recipe.docs}
                  target="_blank"
                  rel="noreferrer"
                >
                  {name} docs
                  <ArrowUpRight aria-hidden="true" className="size-3.5" />
                </a>
              </div>
              <p className="text-muted-foreground mb-5 max-w-3xl text-sm leading-relaxed">
                {recipe.description}
              </p>
              {loadingKeyAccess ? (
                <div
                  role="status"
                  aria-label="Loading key access"
                  aria-busy="true"
                >
                  <Skeleton className="h-24 w-full" />
                </div>
              ) : model ? (
                <div className="[&_pre]:overflow-x-visible [&_pre]:break-words [&_pre]:whitespace-pre-wrap">
                  <CodeBlock language={recipe.language} code={recipe.code} />
                </div>
              ) : (
                <div className="bg-muted rounded-lg p-5 text-sm">
                  <p className="font-medium">
                    No chat model is available to this key yet
                  </p>
                  <p className="text-muted-foreground mt-2">
                    Choose a key with chat access, or ask the gateway owner to
                    add a model to this key’s access plan. Your base URL stays
                    the same.
                  </p>
                </div>
              )}
            </TabsContent>
          );
        })}
      </Tabs>
      <div className="border-border space-y-4 border-t p-5 sm:p-6">
        {!reveal && (
          <div className="max-w-lg space-y-2">
            <label
              htmlFor="connection-test-key"
              className="text-sm font-medium"
            >
              Key to test
            </label>
            <Input
              id="connection-test-key"
              type="password"
              value={testKey}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setTestKey(event.target.value)}
              placeholder="Paste your saved gateway key"
              disabled={testing}
            />
            <p className="text-muted-foreground text-xs">
              {selectedCredentialId === "new"
                ? "Create a key above, or select an existing key to test it."
                : "Paste the selected key. Used for this test, then cleared; it is not saved in your browser."}
            </p>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-4">
          <Button
            disabled={
              testing ||
              selectedCredentialId === "new" ||
              !(reveal?.secret || testKey.trim())
            }
            onClick={() => void testConnection()}
          >
            {testing ? (
              <Loader2 aria-hidden="true" className="animate-spin" />
            ) : (
              <PlugZap aria-hidden="true" />
            )}
            {testing ? "Testing…" : "Test connection"}
          </Button>
          <p className="text-muted-foreground text-xs">
            One real request. Declared local chat, or a model catalog check.
          </p>
        </div>
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
              {testResult.ok ? "Connection works" : "Connection failed"}
              {testResult.latency_ms != null
                ? ` · ${Math.round(testResult.latency_ms)} ms`
                : " · Not measured yet"}
            </p>
            <p className="text-muted-foreground text-sm">{testResult.reason}</p>
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
    </section>
  );
}

function SetupChecklist({ progress }: { progress: SetupProgress }) {
  return (
    <section aria-labelledby="setup-heading" className="space-y-4">
      <div>
        <h2 id="setup-heading" className="text-lg font-semibold">
          Finish setting up
        </h2>
        <p className="text-muted-foreground mt-1 text-sm">
          Your gateway is ready when these steps have actually worked.
        </p>
      </div>
      <ol className="divide-border divide-y">
        {progress.steps.map((step) => (
          <li key={step.id} className="flex items-start gap-3 py-4">
            <span
              className={`mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full ${step.done ? "bg-success-tint text-success" : "bg-muted text-muted-foreground"}`}
            >
              {step.done ? (
                <Check aria-hidden="true" className="size-4" />
              ) : (
                <Circle aria-hidden="true" className="size-3" />
              )}
              <span className="sr-only">
                {step.done ? "Complete" : "Incomplete"}
              </span>
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">
                {stepLabels[step.id] ?? step.id}
                {step.optional && (
                  <span className="text-muted-foreground ml-2 text-xs font-normal">
                    Optional
                  </span>
                )}
              </p>
              <p className="text-muted-foreground mt-1 text-sm">
                {step.reason}
              </p>
            </div>
            {!step.done && step.href && (
              <Button variant="ghost" asChild>
                <a
                  href={
                    step.id === "test_request" || step.id === "connect_app"
                      ? "#connect-heading"
                      : step.href
                  }
                >
                  {step.id === "invite_someone" ? "Invite someone" : "Continue"}
                  <ArrowUpRight aria-hidden="true" />
                </a>
              </Button>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

function Capabilities({
  capabilities,
  aliases,
}: {
  capabilities: HomeCapability[];
  aliases: HomeAlias[];
}) {
  const stable = new Set(aliases.map((alias) => alias.id));
  const bestByCapability = new Map<string, HomeCapability>();
  const score = (row: HomeCapability) =>
    Number(row.available) * 2 + Number(stable.has(row.model ?? ""));
  for (const row of capabilities) {
    const previous = bestByCapability.get(row.capability);
    if (!previous || score(row) > score(previous))
      bestByCapability.set(row.capability, row);
  }
  const tiles = [...bestByCapability.values()];
  return (
    <section aria-labelledby="capabilities-heading">
      <h2 id="capabilities-heading" className="text-lg font-semibold">
        What you can use
      </h2>
      {!tiles.length ? (
        <EmptyState
          title="No capabilities are available yet"
          description="Your gateway owner can connect a model and give your access plan permission to use it."
        />
      ) : (
        <div className="mt-4 grid gap-x-8 sm:grid-cols-2 lg:grid-cols-3">
          {tiles.map((capability) => {
            const copy = capabilityCopy[capability.capability];
            if (!copy) return null;
            return (
              <article
                key={capability.intent}
                className={`border-border min-w-0 border-t py-5 ${capability.available ? "" : "text-muted-foreground"}`}
              >
                <div className="flex items-center gap-3">
                  <CapabilityIcon
                    capability={copy.icon}
                    className={
                      capability.available
                        ? undefined
                        : "bg-muted text-muted-foreground"
                    }
                  />
                  <h3 className="text-base font-semibold">{copy.label}</h3>
                </div>
                <p className="text-muted-foreground mt-3 text-sm">
                  {copy.description}
                </p>
                <div className="mt-3">
                  <StatusPill
                    status={capability.status}
                    reason={capability.reason}
                  />
                </div>
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                  <code className="text-muted-foreground text-xs break-all">
                    {capability.model ??
                      capability.workflow ??
                      "No model assigned"}
                  </code>
                  {capability.available ? (
                    <Button variant="ghost" asChild>
                      <Link
                        href={`/try/?capability=${encodeURIComponent(capability.capability)}`}
                      >
                        Try
                        <ArrowUpRight aria-hidden="true" />
                      </Link>
                    </Button>
                  ) : (
                    <span className="text-muted-foreground text-xs">
                      Unavailable right now
                    </span>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
function CreationPreview({ creation }: { creation: HomeCreation }) {
  const label =
    creation.kind === "video"
      ? "Video"
      : creation.kind === "audio"
        ? "Audio"
        : "Image";
  const Icon =
    creation.kind === "video"
      ? Video
      : creation.kind === "audio"
        ? AudioLines
        : ImageIcon;
  return (
    <Link
      href="/library/"
      className="border-border hover:bg-muted focus-visible:ring-ring flex min-w-0 gap-3 rounded-lg border p-3 focus-visible:ring-2"
    >
      <div className="bg-muted flex size-16 shrink-0 items-center justify-center overflow-hidden rounded-md">
        {creation.kind === "image" && creation.download_url ? (
          // Recent uses the Library API's signed thumbnail-capable download URL.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={creation.download_url}
            alt=""
            loading="lazy"
            className="size-full object-cover"
          />
        ) : (
          <Icon aria-hidden="true" className="text-muted-foreground size-5" />
        )}
      </div>
      <div className="min-w-0">
        <p className="text-sm font-medium">{label}</p>
        <p className="text-muted-foreground mt-1 line-clamp-2 text-xs">
          {creation.prompt || creation.model || "No prompt recorded"}
        </p>
        <p className="text-muted-foreground mt-1 text-xs">
          <RelativeTime
            ts={creation.created_at ?? null}
            fallback="Time not recorded"
          />
        </p>
      </div>
    </Link>
  );
}

function Recent({ home }: { home: HomeSnapshot }) {
  const {
    data: creations,
    isPending: creationsPending,
    isError: creationsError,
  } = useQuery({
    queryKey: ["library", "home-recent-creations"],
    queryFn: homeApi.creations,
  });
  const recentCreations = creations?.assets ?? [];
  return (
    <section aria-labelledby="recent-heading" className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="recent-heading" className="text-lg font-semibold">
            Recent
          </h2>
          <p className="text-muted-foreground mt-1 text-sm">
            Your latest creations and requests.
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <Link
            className="text-primary inline-flex min-h-11 items-center text-sm underline underline-offset-4"
            href="/library/"
          >
            View library
          </Link>
          <Link
            className="text-primary inline-flex min-h-11 items-center text-sm underline underline-offset-4"
            href="/usage/"
          >
            View usage
          </Link>
        </div>
      </div>
      <section aria-labelledby="recent-creations-heading" className="space-y-3">
        <h3 id="recent-creations-heading" className="text-base font-semibold">
          Recent creations
        </h3>
        {creationsPending ? (
          <div
            role="status"
            aria-label="Loading recent creations"
            aria-busy="true"
            className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5"
          >
            {[0, 1, 2, 3, 4].map((item) => (
              <Skeleton key={item} className="h-24 w-full" />
            ))}
          </div>
        ) : creationsError ? (
          <p role="alert" className="text-muted-foreground text-sm">
            Recent creations could not be loaded.
          </p>
        ) : recentCreations.length ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {recentCreations.map((creation) => (
              <CreationPreview key={creation.asset_id} creation={creation} />
            ))}
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">
            Creations from image, video and audio requests will appear here.
          </p>
        )}
      </section>
      <section aria-labelledby="recent-requests-heading" className="space-y-3">
        <h3 id="recent-requests-heading" className="text-base font-semibold">
          Recent requests
        </h3>
        {!home.receipts.length ? (
          <EmptyState
            title="Your first request starts here"
            description="Requests from your connected apps will appear here with their result and time."
            action={
              <Button variant="outline" asChild>
                <Link href="/try/">Try a request</Link>
              </Button>
            }
          />
        ) : (
          <ul className="divide-border divide-y">
            {home.receipts.slice(0, 5).map((receipt) => {
              const duration = receipt.latency_ms ?? receipt.elapsed_ms;
              return (
                <li key={receipt.request_id}>
                  <a
                    href={`/r/${encodeURIComponent(receipt.request_id)}`}
                    className="hover:bg-muted focus-visible:ring-ring flex min-h-16 flex-wrap items-center justify-between gap-3 rounded-lg px-1 py-4 focus-visible:ring-2"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium break-words">
                        {receipt.requested_model ?? receipt.intent ?? "Request"}
                      </p>
                      <p className="text-muted-foreground mt-1 text-xs">
                        <RelativeTime
                          ts={
                            receipt._stored_at ??
                            (receipt.accepted_at_ms != null
                              ? receipt.accepted_at_ms / 1000
                              : null)
                          }
                          fallback="Time not recorded"
                        />
                      </p>
                    </div>
                    <div className="text-muted-foreground flex flex-wrap gap-3 text-xs">
                      <span>
                        {receipt.status === "succeeded" ||
                        receipt.status === "completed"
                          ? "Succeeded"
                          : receipt.status === "failed"
                            ? "Failed"
                            : (receipt.status ?? "Result not recorded")}
                      </span>
                      <span className="tabular-nums">
                        {duration != null
                          ? `${Math.round(duration)} ms`
                          : "Not measured yet"}
                      </span>
                      <ArrowUpRight aria-hidden="true" className="size-4" />
                    </div>
                  </a>
                </li>
              );
            })}
          </ul>
        )}
      </section>
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
  const isOwner = home?.user?.role === "owner";
  const {
    data: setup,
    isPending: setupPending,
    isError: setupError,
    refetch: refetchSetup,
    isFetching: setupFetching,
  } = useQuery({
    queryKey: setupKey,
    queryFn: homeApi.setup,
    enabled: !!isOwner,
  });
  return (
    <div className="min-w-0 space-y-8">
      <PageHeader title="Home" answer="Here’s how to use your AI." />
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
          <div className="flex flex-wrap items-center justify-between gap-4">
            <p className="text-base font-medium">
              Hello, {home.user.name || "there"}.
            </p>
            <StatusPill
              status={home.status.state}
              reason={home.status.reason}
            />
          </div>
          <Connect home={home} />
          {isOwner && setupPending ? (
            <div
              role="status"
              aria-label="Loading setup progress"
              aria-busy="true"
              className="space-y-3"
            >
              <Skeleton className="h-6 w-48" />
              <Skeleton className="h-48 w-full" />
            </div>
          ) : isOwner && setupError ? (
            <ErrorState
              message="Setup progress could not be loaded. Try again."
              onRetry={() => void refetchSetup()}
              retrying={setupFetching}
            />
          ) : isOwner && setup && !setup.complete ? (
            <SetupChecklist progress={setup} />
          ) : (
            <>
              <Capabilities
                capabilities={home.capabilities}
                aliases={home.aliases}
              />
              <Recent home={home} />
            </>
          )}
        </>
      )}
    </div>
  );
}

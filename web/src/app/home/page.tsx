"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
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
import { formatAppName } from "@/lib/format";
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
import { productApi, type SetupEvent } from "@/lib/product-api";
import { PRODUCT_NAME } from "@/lib/product";
import {
  capabilitySummary,
  capabilityTiles,
  greetingName,
  requestLabel,
  setupSteps,
} from "./presentation";

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
const capabilityTint: Record<Capability, string> = {
  chat: "bg-capability-chat/5 border-capability-chat/20",
  image: "bg-capability-image/5 border-capability-image/20",
  video: "bg-capability-video/5 border-capability-video/20",
  voice: "bg-capability-voice/5 border-capability-voice/20",
  transcribe: "bg-capability-transcribe/5 border-capability-transcribe/20",
  decide: "bg-capability-decide/5 border-capability-decide/20",
  embed: "bg-capability-embed/5 border-capability-embed/20",
};
const stepLabels: Record<string, string> = {
  connect_service: "Connect an engine or provider",
  test_request: "Make a real request",
  copy_settings: "Copy settings into your client",
  connect_app: "Confirm your client works",
  view_receipt: "See the request receipt",
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

function Connect({
  home,
  progress,
}: {
  home: HomeSnapshot;
  progress?: SetupProgress;
}) {
  const queryClient = useQueryClient();
  const [savingEvidence, setSavingEvidence] = useState(false);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [client, setClient] = useState<Client>(clients[0]);
  const [instructionsOpen, setInstructionsOpen] = useState(true);
  useEffect(() => {
    setInstructionsOpen(window.innerWidth >= 640);
  }, []);
  const [selectedCredentialId, setSelectedCredentialId] = useState("new");
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

  async function confirmClient(event: SetupEvent) {
    const app = home.apps.find((item) =>
      item.credentials.some((key) => key.id === selectedCredentialId),
    );
    if (savingEvidence) return;
    if (!app) {
      setEvidenceError("Choose an app key before saving your confirmation.");
      return;
    }
    setSavingEvidence(true);
    setEvidenceError(null);
    try {
      await productApi.recordSetupEvidence({ event, app_id: app.id });
      await queryClient.invalidateQueries({ queryKey: setupKey });
    } catch {
      setEvidenceError("Your confirmation could not be saved. Try again.");
    } finally {
      setSavingEvidence(false);
    }
  }
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
      <div className="border-border border-b p-4 sm:p-6">
        <div className="mb-4 flex items-start gap-3">
          <span className="bg-primary/10 text-primary flex size-11 shrink-0 items-center justify-center rounded-lg">
            <PlugZap aria-hidden="true" className="size-5" />
          </span>
          <div>
            <h2 id="connect-heading" className="text-xl font-semibold">
              Connect an app
            </h2>
            <p className="text-muted-foreground mt-1 text-sm">
              One address. A key for each app you use.
            </p>
          </div>
        </div>
        <div className="grid min-w-0 gap-3 lg:grid-cols-2">
          <CopyField label="Base URL" value={home.api_base_url} />
          <div className="min-w-0 space-y-3">
            <label htmlFor="app-key" className="text-sm font-medium">
              App key
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
                  New app · your default access
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
                <label htmlFor="pasted-app-key" className="text-sm font-medium">
                  Paste your key
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
                  Kept only while this page is open; never saved in your
                  browser.
                </p>
              </div>
            ) : (
              <p className="text-muted-foreground text-sm">
                Create a key, then copy it to your app. It appears only once.
              </p>
            )}
            {!reveal && (
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  variant="outline"
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
                      : `Create key for ${client}`}
                </Button>
                <Link
                  className="text-primary text-sm underline underline-offset-4"
                  href="/apps/"
                >
                  Manage keys
                </Link>
              </div>
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
        </div>
      </div>
      <div className="border-border space-y-3 px-4 py-3 sm:px-6">
        <div className="flex flex-wrap items-center gap-4">
          <Button
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
            {testing ? "Checking…" : "Gateway check"}
          </Button>
          <p className="text-muted-foreground text-xs">
            Checks the gateway route with this key. It does not test your client
            app.
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
              {testResult.ok ? "Gateway check passed" : "Gateway check failed"}
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
      <details
        open={instructionsOpen}
        onToggle={(event) => setInstructionsOpen(event.currentTarget.open)}
        className="border-border border-t px-4 sm:px-6"
      >
        <summary className="min-h-11 cursor-pointer content-center text-sm font-medium">
          Client instructions
        </summary>
        <p className="text-muted-foreground py-2 text-xs">
          Every recipe and the gateway check use this app key’s access plan. A
          new app uses your default access.
        </p>
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
                className="min-w-0 py-4"
              >
                <div className="mb-4 flex flex-wrap items-baseline justify-between gap-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <h3 className="text-base font-semibold">Connect {name}</h3>
                    <span className="text-muted-foreground text-xs">
                      {name === "Open WebUI"
                        ? "Tested with Open WebUI v0.11.4 · host network, non-streaming"
                        : "Untested recipe"}
                    </span>
                    <details className="text-muted-foreground text-xs">
                      <summary className="min-h-11 cursor-pointer content-center">
                        {progress?.evidence?.external_client_confirmed.recorded
                          ? "External client: user confirmed"
                          : "Client setup: not verified"}
                      </summary>
                      <p className="max-w-md pb-2 leading-relaxed">
                        Copy these settings into {name} and connect there. The
                        gateway check above verifies the route and key; it
                        cannot verify your client app.
                      </p>
                    </details>
                  </div>
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
                      No chat alias is advertised for this key yet
                    </p>
                    <p className="text-muted-foreground mt-2">
                      Choose another key, or ask the gateway owner to check this
                      key’s access plan and chat aliases. Models without an
                      alias may still be available. Your base URL stays the
                      same.
                    </p>
                  </div>
                )}
              </TabsContent>
            );
          })}
        </Tabs>
      </details>
      {progress && (
        <div className="border-border space-y-2 border-t p-4 sm:px-6">
          <p className="text-muted-foreground text-xs">
            Confirm only what you did in your external client. These are your
            confirmations, not automated client tests.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              className="min-h-11"
              disabled={
                savingEvidence ||
                creating ||
                !home.apps.some((app) =>
                  app.credentials.some(
                    (key) => key.id === selectedCredentialId,
                  ),
                ) ||
                selectedCredentialId === "new" ||
                !!progress.evidence?.settings_copied.recorded
              }
              onClick={() => void confirmClient("settings_copied")}
            >
              I copied the settings
            </Button>
            <Button
              variant="outline"
              className="min-h-11"
              disabled={
                savingEvidence ||
                creating ||
                !home.apps.some((app) =>
                  app.credentials.some(
                    (key) => key.id === selectedCredentialId,
                  ),
                ) ||
                selectedCredentialId === "new" ||
                !!progress.evidence?.external_client_confirmed.recorded
              }
              onClick={() => void confirmClient("external_client_confirmed")}
            >
              My client sent a request
            </Button>
          </div>
          {savingEvidence && (
            <p role="status" className="text-sm">
              Saving confirmation…
            </p>
          )}
          {evidenceError && (
            <p role="alert" className="text-destructive text-sm">
              {evidenceError}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function SetupChecklist({
  progress,
  home,
}: {
  progress: SetupProgress;
  home: HomeSnapshot;
}) {
  const queryClient = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const steps = setupSteps(progress);
  const next = steps.find((step) => !step.optional && !step.done);
  const receipt = home.receipts.find(
    (item) => item.status === "succeeded" || item.status === "completed",
  );
  async function confirmViewed() {
    if (!receipt || saving) return;
    setSaving(true);
    setError(null);
    try {
      await productApi.recordSetupEvidence({
        event: "receipt_viewed",
        request_id: receipt.request_id,
      });
      await queryClient.invalidateQueries({ queryKey: setupKey });
    } catch {
      setError("Your receipt confirmation could not be saved. Try again.");
    } finally {
      setSaving(false);
    }
  }
  return (
    <section
      aria-labelledby="setup-heading"
      className="border-primary/20 bg-primary/5 rounded-xl border p-4"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h2 id="setup-heading" className="text-sm font-semibold">
            Finish setting up
          </h2>
          <p className="mt-1 text-sm font-medium">
            Next:{" "}
            {next
              ? next.id === "view_receipt" && !receipt
                ? "Make a request with your app key"
                : (stepLabels[next.id] ?? next.id)
              : "Invite someone (optional)"}
          </p>
        </div>
        {next?.href && (
          <Button className="min-h-11" variant="outline" asChild>
            <a
              href={
                next.id === "view_receipt"
                  ? receipt
                    ? `/r/${encodeURIComponent(receipt.request_id)}`
                    : "#connect"
                  : next.id === "test_request" ||
                      next.id === "copy_settings" ||
                      next.id === "connect_app"
                    ? "#connect"
                    : next.href
              }
            >
              <span className="sr-only sm:not-sr-only">Continue</span>
              <ArrowUpRight aria-hidden="true" />
            </a>
          </Button>
        )}
      </div>
      <details className="mt-2">
        <summary className="text-muted-foreground min-h-11 cursor-pointer content-center text-sm">
          Setup details
        </summary>
        <ol className="divide-border divide-y">
          {steps.map((step) => (
            <li key={step.id} className="flex items-start gap-3 py-3">
              <span className="text-muted-foreground mt-1">
                {step.done ? (
                  <Check aria-hidden="true" className="text-success size-4" />
                ) : (
                  <Circle aria-hidden="true" className="size-4" />
                )}
                <span className="sr-only">
                  {step.done ? "Complete" : "Incomplete"}
                </span>
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">
                  {stepLabels[step.id] ?? step.id}
                  {step.optional && (
                    <span className="text-muted-foreground ml-2 text-xs">
                      Optional
                    </span>
                  )}
                </p>
                <p className="text-muted-foreground mt-1 text-sm">
                  {step.reason}
                </p>
                {step.id === "view_receipt" && receipt && !step.done && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button variant="ghost" asChild>
                      <a
                        href={`/r/${encodeURIComponent(receipt.request_id)}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open receipt
                        <ArrowUpRight aria-hidden="true" />
                      </a>
                    </Button>
                    <Button
                      variant="outline"
                      disabled={saving}
                      onClick={() => void confirmViewed()}
                    >
                      I viewed this receipt
                    </Button>
                  </div>
                )}
                {step.optional && !step.done && step.href && (
                  <Button variant="ghost" asChild>
                    <a href={step.href}>
                      Invite someone
                      <ArrowUpRight aria-hidden="true" />
                    </a>
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ol>
        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}
      </details>
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
  const tiles = capabilityTiles(capabilities, aliases);
  return (
    <section aria-labelledby="capabilities-heading">
      <h2 id="capabilities-heading" className="text-lg font-semibold">
        What you can use
      </h2>
      {!tiles.length ? (
        <EmptyState
          title="No capabilities are available yet"
          description="Your gateway owner can connect a model and give your access plan permission to use it."
          action={
            <Button asChild variant="outline">
              <Link href="/models/">View models and access</Link>
            </Button>
          }
        />
      ) : (
        <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {tiles.map((capability) => {
            const copy = capabilityCopy[capability.capability];
            if (!copy) return null;
            return (
              <article
                key={capability.intent}
                className={`min-w-0 rounded-xl border p-5 ${capability.available ? capabilityTint[copy.icon] : "border-border bg-muted/30 text-muted-foreground"}`}
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
                {capability.availability && (
                  <p className="text-muted-foreground mt-2 text-xs">
                    {capability.availability.evidence.source === "request"
                      ? "Last request observation: "
                      : "Health observation: "}
                    <RelativeTime
                      ts={
                        capability.availability.evidence.observed_at_s ?? null
                      }
                      fallback="Not measured yet"
                    />
                  </p>
                )}
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                  <code className="text-muted-foreground text-xs break-all">
                    {capability.model ?? "No alias assigned"}
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
                  ) : null}
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
      className="border-border bg-card hover:border-primary/40 focus-visible:ring-ring group flex min-w-0 flex-col overflow-hidden rounded-xl border focus-visible:ring-2"
    >
      <div
        className={`flex aspect-[4/3] w-full items-center justify-center overflow-hidden ${creation.kind === "video" ? "bg-capability-video/10 text-capability-video" : creation.kind === "audio" ? "bg-capability-voice/10 text-capability-voice" : "bg-capability-image/10 text-capability-image"}`}
      >
        {creation.kind === "image" && creation.download_url ? (
          // Recent uses the Library API's signed thumbnail-capable download URL.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={creation.download_url}
            alt=""
            loading="lazy"
            className="size-full object-cover transition-transform group-hover:scale-[1.02] motion-reduce:transform-none"
          />
        ) : (
          <Icon aria-hidden="true" className="size-8" />
        )}
      </div>
      <div className="min-w-0 p-3">
        <p className="text-sm font-medium">{label}</p>
        <p className="text-muted-foreground mt-1 line-clamp-2 text-xs">
          {creation.prompt || `${label} created by your gateway`}
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
    refetch: refetchCreations,
    isFetching: creationsFetching,
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
              <Skeleton key={item} className="h-48 w-full rounded-xl" />
            ))}
          </div>
        ) : creationsError ? (
          <ErrorState
            message="Recent creations could not be loaded. Try again."
            onRetry={() => void refetchCreations()}
            retrying={creationsFetching}
          />
        ) : recentCreations.length ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {recentCreations.map((creation) => (
              <CreationPreview key={creation.asset_id} creation={creation} />
            ))}
          </div>
        ) : (
          <EmptyState
            title="Your creations start here"
            description="Images, videos and audio you make through the gateway will appear here."
            action={
              <Button variant="outline" asChild>
                <Link href="/try/">Create something</Link>
              </Button>
            }
          />
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
                        {requestLabel(receipt, home.aliases)}
                      </p>
                      {receipt.requested_model && (
                        <code className="text-muted-foreground mt-1 block text-xs break-all">
                          {receipt.requested_model}
                        </code>
                      )}
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
  const { data: instance } = useQuery({
    queryKey: ["foundation", "instance"],
    queryFn: async () => {
      const response = await fetch("/.well-known/ai-gateway");
      if (!response.ok) throw new Error("Instance details are unavailable");
      return response.json() as Promise<{ service?: { name?: string } }>;
    },
  });
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
    <div className="min-w-0 space-y-4 sm:space-y-6">
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
          <div className="border-primary/25 flex items-start justify-between gap-3 border-b pb-4">
            <div className="min-w-0">
              <h1 className="font-display text-2xl font-medium tracking-tight break-words sm:text-4xl">
                Hello, {greetingName(home.user)}.
              </h1>
              <p className="text-muted-foreground mt-1 hidden text-xs sm:block">
                Welcome to {instance?.service?.name || PRODUCT_NAME}.
              </p>
              <p role="status" className="text-muted-foreground mt-2 text-sm">
                {capabilitySummary(
                  capabilityTiles(home.capabilities, home.aliases),
                )}
              </p>
            </div>
            <svg
              viewBox="0 0 40 32"
              aria-hidden="true"
              className="text-primary w-10 shrink-0 sm:w-16"
            >
              <path
                d="M9 16c8-12 19-12 26 0-7 12-18 12-26 0Z"
                fill="currentColor"
              />
              <path d="m9 16-6-7v14Z" fill="currentColor" />
              <circle cx="28" cy="14" r="2" className="fill-background" />
            </svg>
          </div>
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
          ) : isOwner &&
            setup &&
            (!setup.complete ||
              setupSteps(setup).some(
                (step) => !step.optional && !step.done,
              )) ? (
            <SetupChecklist progress={setup} home={home} />
          ) : null}
          <Connect home={home} progress={isOwner ? setup : undefined} />
          {!(
            isOwner &&
            setup &&
            (!setup.complete ||
              setupSteps(setup).some((step) => !step.optional && !step.done))
          ) && (
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

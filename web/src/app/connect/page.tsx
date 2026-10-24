"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { ErrorState } from "@/components/product/error-state";
import { CopyField } from "@/components/product/copy-field";
import { SettingsSection } from "@/components/product/setting-row";
import { AppGuide } from "@/components/product/app-guide";
import { RelativeTime } from "@/components/product/relative-time";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  accountApi,
  type AccountApp,
  type GatewayDiscovery,
} from "@/lib/account-api";
import { productApi } from "@/lib/product-api";
import {
  appGuides,
  guideIdForAppName,
  suggestedModel,
  type GuideValues,
} from "@/lib/app-guides";
import { MCP_DOCS_URL } from "@/lib/docs";
import { keyStart } from "@/lib/key-hint";
import { isLocalOnlyAddress } from "@/lib/private-address";
import {
  listTrustedAddresses,
  trustedAddressesKey,
} from "@/lib/trusted-addresses";
import { formatAppName } from "@/lib/format";

/** Discovery sends root-relative or `{base_url}` addresses when the gateway
 * has no public address set; apps then reach it at the address in use here. */
function absolute(address: string, origin: string): string {
  if (address.includes("{base_url}"))
    return address.replace("{base_url}", origin);
  return address.startsWith("/") ? `${origin}${address}` : address;
}

/** The app a link names (`?app=<id>`). Read after mount, as navigation context only: an id is never a credential. */
function useRequestedAppId(): string | null {
  const [appId, setAppId] = useState<string | null>(null);
  useEffect(() => {
    const read = () =>
      setAppId(new URLSearchParams(window.location.search).get("app") || null);
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return appId;
}

/** Names as one phrase: "A", "A and B", "A, B and C". */
function listed(names: string[]): string {
  if (names.length < 2) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function ConnectSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true">
      <span className="sr-only" role="status">
        Loading connection details…
      </span>
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/**
 * How to use this gateway from other apps: the values each client needs, the
 * addresses behind them and the MCP server. Everything comes from
 * /.well-known/azizam, so the page cannot drift from the server. With
 * `?app=<id>` it sets up one app from Apps & keys and shows that app's access
 * and key first.
 */
export default function ConnectPage() {
  const viewer = useViewer();
  const {
    data: discovery,
    isPending,
    isError,
    refetch,
  } = useQuery({
    queryKey: ["gateway", "discovery"],
    queryFn: accountApi.discovery,
  });
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const requestedAppId = useRequestedAppId();
  const { data: session } = useQuery({
    queryKey: ["account", "session"],
    queryFn: accountApi.session,
    enabled: viewer.authenticated && requestedAppId !== null,
  });
  const setupApp: AccountApp | null =
    session?.apps.find((app) => app.id === requestedAppId) ?? null;
  const notFound =
    requestedAppId !== null && session !== undefined && !setupApp;
  const setupName = setupApp
    ? formatAppName(setupApp.name, session?.user?.email)
    : "";

  return (
    <div className="page-read">
      <PageHeader
        title={setupApp ? `Connect ${setupName}` : "Connect"}
        subtitle={
          setupApp
            ? "Paste this address and your key."
            : "Use this gateway from other apps and AI agents."
        }
      />
      <div className="page-stack">
        {requestedAppId !== null ? (
          <p className="text-meta">
            <Link prefetch={false} href="/apps/" className="underline">
              Back to Apps &amp; keys
            </Link>
            {notFound ? " · That app isn't in your list." : null}
          </p>
        ) : viewer.authenticated ? (
          <p className="text-meta">
            Each app has its own key. To set one up,{" "}
            <Link prefetch={false} href="/apps/" className="underline">
              choose the app in Apps &amp; keys
            </Link>
            .
          </p>
        ) : null}
        {!viewer.authenticated ? (
          <EmptyState
            title="Sign in to connect your apps"
            description="Each app gets a key with your account's access plan."
            action={
              <Button asChild>
                <Link prefetch={false} href="/sign-in/?return_to=%2Fconnect%2F">
                  Sign in
                </Link>
              </Button>
            }
          />
        ) : isPending ? (
          <ConnectSkeleton />
        ) : isError ? (
          <ErrorState
            message="The gateway couldn't load connection details."
            onRetry={() => void refetch()}
          />
        ) : (
          <ConnectDetails
            key={setupApp?.id ?? "any-app"}
            discovery={discovery}
            origin={origin}
            admin={viewer.admin}
            app={setupApp}
            appName={setupName}
          />
        )}
      </div>
    </div>
  );
}

function ConnectDetails({
  discovery,
  origin,
  admin,
  app,
  appName,
}: {
  discovery: GatewayDiscovery;
  origin: string;
  admin: boolean;
  /** The app this page sets up, when the link named one. */
  app: AccountApp | null;
  appName: string;
}) {
  const address = (id: string, fallback: string) =>
    absolute(discovery.recipes[id]?.canonical_base_url ?? fallback, origin);
  const endpoints = {
    openai: address("openai", "/openai/v1"),
    anthropic: address("anthropic", "/anthropic"),
    ollama: address("ollama", "/ollama"),
    mcp: absolute(discovery.mcp?.url ?? "/mcp", origin),
  };
  const signIn = discovery.mcp?.sign_in ?? false;
  // The client the person picks. Until they pick one, it is the guide the app's name points at.
  const [pickedGuide, setPickedGuide] = useState<string | null>(null);
  const guideId =
    pickedGuide ?? (app ? guideIdForAppName(app.name) : appGuides.apps[0].id);
  const { data: catalog, isPending: catalogPending } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
  });
  const catalogRows = [...(catalog?.aliases ?? []), ...(catalog?.models ?? [])];
  // Only an owner or admin may list an app's trusted addresses; for anyone else the line stays off.
  const { data: trusted } = useQuery({
    queryKey: trustedAddressesKey(app?.id ?? ""),
    queryFn: () => listTrustedAddresses(app!.id),
    enabled: admin && app !== null,
  });
  const keyless = (trusted ?? []).map((entry) => entry.address);
  const values: GuideValues = {
    openai_url: endpoints.openai,
    anthropic_url: endpoints.anthropic,
    mcp_url: endpoints.mcp,
    chat_model: suggestedModel(catalogRows, "chat"),
    transcription_model: suggestedModel(catalogRows, "transcribe"),
  };
  const keyHint = app ? (
    app.credentials.length ? (
      "Paste the key you saved for this app."
    ) : (
      "No key yet. Create one above, then paste it here."
    )
  ) : (
    <>
      Paste a key from{" "}
      <Link prefetch={false} href="/apps/" className="underline">
        Apps &amp; keys
      </Link>
      .
    </>
  );
  const setup = (
    <AppGuide
      appId={guideId}
      onAppChange={setPickedGuide}
      values={values}
      keyHint={keyHint}
      loading={catalogPending}
    />
  );
  const anyLocal = Object.values(endpoints).some(isLocalOnlyAddress);
  const endpointsSection = (
    <SettingsSection
      title="Endpoints"
      description="Reference: each address this gateway serves."
    >
      <div className="flex flex-col gap-4 p-4">
        <div className="flex flex-col gap-2">
          <CopyField
            label="OpenAI-compatible address"
            value={endpoints.openai}
          />
          <p className="text-muted-foreground text-meta">
            Most apps use this one.
          </p>
        </div>
        <CopyField
          label="Anthropic-compatible address"
          value={endpoints.anthropic}
        />
        <CopyField label="Ollama-compatible address" value={endpoints.ollama} />
        <CopyField label="MCP server address" value={endpoints.mcp} />
        {anyLocal ? (
          <p className="text-muted-foreground text-meta">
            It works only from this machine or this network.
          </p>
        ) : null}
      </div>
    </SettingsSection>
  );
  const mcpSection = (
    <SettingsSection
      title="AI apps and agents (MCP)"
      description="ChatGPT, Claude and other MCP apps can run this gateway's workflows as you."
    >
      <div className="flex flex-col gap-3 p-4">
        {signIn ? (
          <p className="text-muted-foreground text-meta">
            Sign-in apps need only the MCP server address. You can disconnect
            any of them in Apps &amp; keys at any time.
          </p>
        ) : (
          <p className="text-meta">
            Signing in from AI apps needs this gateway&apos;s public https
            address.{" "}
            {admin ? (
              <Link prefetch={false} href="/settings" className="underline">
                Set it in Settings
              </Link>
            ) : (
              "Ask the owner to set it"
            )}
            . Until then, apps that send a header can use a key.
          </p>
        )}
        <a
          href={MCP_DOCS_URL}
          target="_blank"
          rel="noreferrer noopener"
          className="text-meta w-fit underline underline-offset-4"
        >
          MCP setup docs →
        </a>
      </div>
    </SettingsSection>
  );
  const automation = admin ? (
    <p className="text-muted-foreground text-meta flex flex-wrap items-center gap-x-4 gap-y-2 border-t pt-4">
      Gateway automation
      <a href="/docs" className="underline underline-offset-4">
        API reference
      </a>
      <a href="/openapi.json" className="underline underline-offset-4">
        OpenAPI document
      </a>
      <Link
        prefetch={false}
        href="/settings"
        className="underline underline-offset-4"
      >
        Set the public address
      </Link>
    </p>
  ) : null;

  if (app) {
    return (
      <>
        <AppStrip app={app} appName={appName} addresses={keyless} />
        <div className="bg-card rounded-panel border p-4">{setup}</div>
        {endpointsSection}
        {mcpSection}
        {automation}
      </>
    );
  }
  return (
    <>
      {endpointsSection}
      <SettingsSection
        title="Set up an app"
        description="Pick an app, then paste these values into it."
      >
        <div className="p-4">{setup}</div>
      </SettingsSection>
      {mcpSection}
      {automation}
    </>
  );
}

/**
 * The app this page sets up: its access plan, its newest key (masked, with
 * when it was last used) and the one action its key needs. Keyless addresses
 * are listed only for an owner, who is the only one who can see them.
 */
function AppStrip({
  app,
  appName,
  addresses,
}: {
  app: AccountApp;
  appName: string;
  addresses: string[];
}) {
  const newest = [...app.credentials].sort(
    (a, b) =>
      (b.last_used_at ?? b.created_at) - (a.last_used_at ?? a.created_at),
  )[0];
  const plan = app.access_plan;
  const planName = plan
    ? plan.same_as_person
      ? `${plan.name} (yours)`
      : plan.name
    : "none yet";
  const count = app.credentials.length;
  const appHref = `/apps/?app=${encodeURIComponent(app.id)}`;
  return (
    <section
      aria-label={`${appName}: access and key`}
      className="bg-card rounded-panel flex flex-col gap-3 border p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-body font-medium break-words">{appName}</h2>
        <p className="text-muted-foreground text-meta">
          Access plan: {planName}
        </p>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <p className="text-meta flex flex-wrap items-center gap-x-3 gap-y-1">
          {newest ? (
            <>
              <span className="font-mono">{keyStart(newest.prefix)}</span>
              <span className="text-muted-foreground">
                {newest.last_used_at != null ? (
                  <>
                    Last used <RelativeTime ts={newest.last_used_at} />
                  </>
                ) : (
                  "Never used"
                )}
              </span>
              {count > 1 ? (
                <span className="text-muted-foreground">{count} keys</span>
              ) : null}
            </>
          ) : (
            <span className="text-muted-foreground">No keys yet</span>
          )}
        </p>
        {!app.can_mint_keys ? (
          count === 0 ? (
            <p className="text-muted-foreground text-meta">
              Ask the owner to update this app&apos;s access.
            </p>
          ) : null
        ) : count === 0 ? (
          <Button asChild variant="outline" size="sm">
            <Link prefetch={false} href={`${appHref}&action=create-key`}>
              Create key
            </Link>
          </Button>
        ) : (
          <Button asChild variant="outline" size="sm">
            <Link prefetch={false} href={`${appHref}&action=rotate`}>
              {count === 1 ? "Rotate key" : "Rotate keys"}
            </Link>
          </Button>
        )}
      </div>
      {addresses.length > 0 ? (
        <p className="text-muted-foreground text-meta">
          From {listed(addresses)} no key is needed
        </p>
      ) : null}
    </section>
  );
}

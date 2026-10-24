"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { ErrorState } from "@/components/product/error-state";
import { CopyField } from "@/components/product/copy-field";
import { CodeBlock } from "@/components/product/code-block";
import { SettingsSection } from "@/components/product/setting-row";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  accountApi,
  type AccountApp,
  type GatewayDiscovery,
} from "@/lib/account-api";
import { productApi } from "@/lib/product-api";
import {
  appGuides,
  guideIdForAppName,
  keyApps,
  mcpApps,
  resolveMcpGuide,
  suggestedModel,
  type AppGuide as AppGuideData,
  type McpGuide,
} from "@/lib/app-guides";
import { AppGuide } from "@/components/product/app-guide";
import { PRODUCT_NAME } from "@/lib/product";
import { formatAppName } from "@/lib/format";

const KEY_PLACEHOLDER = "<your key>";

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

function mcpSnippets(url: string) {
  const name = PRODUCT_NAME.toLowerCase();
  return [
    {
      language: "JSON",
      code: JSON.stringify(
        {
          mcpServers: {
            [name]: {
              type: "http",
              url,
              headers: { Authorization: `Bearer ${KEY_PLACEHOLDER}` },
            },
          },
        },
        null,
        2,
      ),
    },
    {
      language: "Command line",
      code: [
        `curl -X POST ${url} \\`,
        `  -H "Authorization: Bearer ${KEY_PLACEHOLDER}" \\`,
        `  -H "Content-Type: application/json" \\`,
        `  -H "Accept: application/json" \\`,
        `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`,
      ].join("\n"),
    },
  ];
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
 * How to use this gateway from other apps: the addresses, the setup steps for
 * each app, and the MCP server with the tools it lists. Everything comes from
 * /.well-known/azizam, so the page cannot drift from the server. With
 * `?app=<id>` it sets up one app from Apps & keys and links back to it.
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
  const notFound = requestedAppId !== null && session !== undefined && !setupApp;
  const setupName = setupApp
    ? formatAppName(setupApp.name, session?.user?.email)
    : "";

  return (
    <div className="page-read">
      <PageHeader
        title="Connect"
        subtitle={
          setupApp
            ? `Set up ${setupName} with its key.`
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
                <Link prefetch={false} href="/sign-in/?return_to=%2Fconnect%2F">Sign in</Link>
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
  const mcpUrl = absolute(discovery.mcp?.url ?? "/mcp", origin);
  const tools = discovery.mcp?.tools ?? [];
  const signIn = discovery.mcp?.sign_in ?? false;
  // The client the person picks. Until they pick one, it is the guide the app's name points at.
  const [pickedGuide, setPickedGuide] = useState<string | null>(null);
  const guideApp =
    pickedGuide ?? (app ? guideIdForAppName(app.name) : keyApps[0].id);
  const { data: catalog, isPending: catalogPending } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
  });
  const catalogRows = [...(catalog?.aliases ?? []), ...(catalog?.models ?? [])];
  return (
    <>
      <SettingsSection
        title="API endpoints"
        description="Paste an address and a key into any app that supports one."
      >
        <div className="flex flex-col gap-4 p-4">
          <CopyField
            label="OpenAI-compatible address"
            value={address("openai", "/openai/v1")}
          />
          <CopyField
            label="Anthropic-compatible address"
            value={address("anthropic", "/anthropic")}
          />
          <CopyField
            label="Ollama-compatible address"
            value={address("ollama", "/ollama")}
          />
          <CopyField label="MCP server address" value={mcpUrl} />
        </div>
      </SettingsSection>
      <SettingsSection
        title="Set up an app"
        description={
          app
            ? `For ${appName}. Where to click and what to paste.`
            : "Where to click and what to paste, for each app."
        }
      >
        <div className="p-4">
          <AppGuide
            appId={guideApp}
            onAppChange={setPickedGuide}
            values={{
              openai_url: address("openai", "/openai/v1"),
              anthropic_url: address("anthropic", "/anthropic"),
              mcp_url: mcpUrl,
              chat_model: suggestedModel(catalogRows, "chat"),
              transcription_model: suggestedModel(catalogRows, "transcribe"),
            }}
            keyHint={
              app
                ? "Paste the key you saved for this app."
                : "Paste a key from Apps & keys."
            }
            loading={catalogPending}
          />
        </div>
      </SettingsSection>
      <SettingsSection
        title="AI apps and agents (MCP)"
        description="ChatGPT, Claude and other MCP apps can run this gateway's workflows as you."
      >
        <div className="flex flex-col gap-4 p-4">
          {signIn ? (
            <>
              <ol aria-label="How to connect" className="list-decimal pl-5">
                <li>
                  In your AI app, add a custom MCP server (some apps call it a
                  connector) and paste the MCP server address above.
                </li>
                <li>
                  When the app asks, sign in to {PRODUCT_NAME} and choose Allow.
                </li>
              </ol>
              <p className="text-muted-foreground text-meta">
                The app then works within your access plan. You can disconnect
                it in Apps &amp; keys at any time.
              </p>
            </>
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
          <div>
            <h3 className="text-body font-medium">Tools</h3>
            {tools.length ? (
              <ul aria-label="MCP tools" className="mt-2 flex flex-col gap-2">
                {tools.map((tool) => (
                  <li key={tool.name} className="min-w-0">
                    <code className="font-mono text-sm">{tool.name}</code>
                    <p className="text-muted-foreground text-meta">
                      {tool.description}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground text-meta">
                This gateway didn&apos;t list any MCP tools.
              </p>
            )}
          </div>
        </div>
      </SettingsSection>
      <SettingsSection
        title="MCP notes for each app"
        description={`From each app's own documentation, checked ${appGuides.checked}.`}
      >
        <Accordion type="single" collapsible className="px-4">
          {mcpApps.map((client) => (
            <ClientNote
              key={client.id}
              app={client}
              guide={resolveMcpGuide(client.mcp, mcpUrl)}
            />
          ))}
        </Accordion>
      </SettingsSection>
      <SettingsSection
        title="Use a key instead"
        description="For apps and scripts that send a key in a header. ChatGPT and Claude.ai sign in instead."
      >
        <div className="flex flex-col gap-4 p-4">
          <p className="text-muted-foreground text-meta">
            Send your key in the Authorization header. Replace {KEY_PLACEHOLDER}{" "}
            with a key from{" "}
            <Link prefetch={false} href="/apps/" className="underline">
              Apps &amp; keys
            </Link>
            .
          </p>
          <CodeBlock snippets={mcpSnippets(mcpUrl)} />
        </div>
      </SettingsSection>
      {admin ? (
        <SettingsSection
          title="Gateway automation"
          description="For scripts that manage this gateway."
        >
          <div className="flex flex-col gap-2 p-4 text-sm">
            <a href="/docs" className="underline">
              API reference
            </a>
            <a href="/openapi.json" className="underline">
              OpenAPI document
            </a>
            <Link prefetch={false} href="/settings" className="underline">
              Set the gateway&apos;s public address
            </Link>
          </div>
        </SettingsSection>
      ) : null}
    </>
  );
}

/** One app's MCP setup notes (lib/app-guides.json): data, never a branch. */
function ClientNote({ app, guide: note }: { app: AppGuideData; guide: McpGuide }) {
  return (
    <AccordionItem value={app.id}>
      <AccordionTrigger>
        <span className="flex flex-wrap items-center gap-2">
          {app.name}
          <Badge variant="outline">
            {note.auth === "sign-in" ? "Sign in" : "Sign in or key"}
          </Badge>
          {note.status === "unverified" ? (
            <Badge variant="secondary">Partly unverified</Badge>
          ) : null}
        </span>
      </AccordionTrigger>
      <AccordionContent>
        <ol className="list-decimal pl-5">
          {note.steps.map((step) => (
            <li key={step} className="break-words">
              {step}
            </li>
          ))}
        </ol>
        {note.note ? (
          <p className="text-muted-foreground text-meta mt-2">{note.note}</p>
        ) : null}
        <a
          href={note.docs}
          className="text-meta mt-2 inline-block underline"
          target="_blank"
          rel="noreferrer noopener"
        >
          {app.name} documentation
        </a>
      </AccordionContent>
    </AccordionItem>
  );
}

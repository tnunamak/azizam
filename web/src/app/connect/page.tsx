"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { ErrorState } from "@/components/product/error-state";
import { CopyField } from "@/components/product/copy-field";
import { CodeBlock } from "@/components/product/code-block";
import { SettingsSection } from "@/components/product/setting-row";
import { RelativeTime } from "@/components/product/relative-time";
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
  type GatewayDiscovery,
  type McpClientNote,
} from "@/lib/account-api";
import { PRODUCT_NAME } from "@/lib/product";
import { toast } from "@/lib/toast";

const KEY_PLACEHOLDER = "<your app key>";

/** Discovery sends root-relative or `{base_url}` addresses when the gateway
 * has no public address set; apps then reach it at the address in use here. */
function absolute(address: string, origin: string): string {
  if (address.includes("{base_url}"))
    return address.replace("{base_url}", origin);
  return address.startsWith("/") ? `${origin}${address}` : address;
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
 * How to use this gateway from other apps: the OpenAI-compatible, Anthropic and
 * Ollama addresses, and the MCP server with the tools it lists. Everything comes
 * from /.well-known/azizam, so the page cannot drift from the server.
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

  return (
    <div className="page-read">
      <PageHeader
        title="Connect"
        subtitle="Use this gateway from other apps and AI agents."
        primaryAction={
          viewer.authenticated ? (
            <Button asChild>
              <Link href="/apps?action=create-key">Create a key</Link>
            </Button>
          ) : undefined
        }
      />
      <div className="page-stack">
        {!viewer.authenticated ? (
          <EmptyState
            title="Sign in to connect your apps"
            description="Each app gets a key with your account's access plan."
            action={
              <Button asChild>
                <Link href="/sign-in/?return_to=%2Fconnect%2F">Sign in</Link>
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
            discovery={discovery}
            origin={origin}
            admin={viewer.admin}
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
}: {
  discovery: GatewayDiscovery;
  origin: string;
  admin: boolean;
}) {
  const address = (id: string, fallback: string) =>
    absolute(discovery.recipes[id]?.canonical_base_url ?? fallback, origin);
  const mcpUrl = absolute(discovery.mcp?.url ?? "/mcp", origin);
  const tools = discovery.mcp?.tools ?? [];
  const signIn = discovery.mcp?.sign_in ?? false;
  const notes = discovery.mcp?.clients?.clients ?? [];
  const checked = discovery.mcp?.clients?.checked ?? "";
  return (
    <>
      <SettingsSection
        title="Apps"
        description="Paste an address and an app key into any app that supports one."
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
        </div>
      </SettingsSection>
      <SettingsSection
        title="AI apps and agents (MCP)"
        description="ChatGPT, Claude and other MCP apps can run this gateway's workflows as you."
      >
        <div className="flex flex-col gap-4 p-4">
          <CopyField label="MCP server address" value={mcpUrl} />
          {signIn ? (
            <>
              <ol aria-label="How to connect" className="list-decimal pl-5">
                <li>
                  In your AI app, add a custom MCP server (some apps call it a
                  connector) and paste this address.
                </li>
                <li>
                  When the app asks, sign in to {PRODUCT_NAME} and choose Allow.
                </li>
              </ol>
              <p className="text-muted-foreground text-meta">
                The app then works within your access plan. You can disconnect
                it here at any time.
              </p>
            </>
          ) : (
            <p className="text-meta">
              Signing in from AI apps needs this gateway&apos;s public https
              address.{" "}
              {admin ? (
                <Link href="/settings" className="underline">
                  Set it in Settings
                </Link>
              ) : (
                "Ask the owner to set it"
              )}
              . Until then, apps that send a header can use an app key.
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
      <McpConnections />
      {notes.length ? (
        <SettingsSection
          title="Notes for each app"
          description={`From each app's own documentation, checked ${checked}.`}
        >
          <Accordion type="single" collapsible className="px-4">
            {notes.map((note) => (
              <ClientNote key={note.id} note={note} url={mcpUrl} />
            ))}
          </Accordion>
        </SettingsSection>
      ) : null}
      <SettingsSection
        title="Use an app key instead"
        description="For apps and scripts that send a header. ChatGPT and Claude.ai sign in instead."
      >
        <div className="flex flex-col gap-4 p-4">
          <p className="text-muted-foreground text-meta">
            Send your app key as a bearer token. Replace {KEY_PLACEHOLDER} with
            a key you{" "}
            <Link href="/apps?action=create-key" className="underline">
              create
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
            <Link href="/settings" className="underline">
              Set the gateway&apos;s public address
            </Link>
          </div>
        </SettingsSection>
      ) : null}
    </>
  );
}

/** One client's setup notes. They are data: the server never treats one
 * client differently from another. */
function ClientNote({ note, url }: { note: McpClientNote; url: string }) {
  const fill = (text: string) => text.replaceAll("<server address>", url);
  return (
    <AccordionItem value={note.id}>
      <AccordionTrigger>
        <span className="flex flex-wrap items-center gap-2">
          {note.name}
          <Badge variant="outline">
            {note.auth === "sign-in" ? "Sign in" : "Sign in or app key"}
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
              {fill(step)}
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
          {note.name} documentation
        </a>
      </AccordionContent>
    </AccordionItem>
  );
}

/** Apps this person connected by signing in, each with Disconnect. */
function McpConnections() {
  const queryClient = useQueryClient();
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ["mcp", "connections"],
    queryFn: accountApi.mcpConnections,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => accountApi.revokeMcpConnection(id),
    onSuccess: () => {
      toast.success("Disconnected. That app can't use your account any more.");
      void queryClient.invalidateQueries({ queryKey: ["mcp", "connections"] });
    },
    onError: () => toast.error("Couldn't disconnect. Try again."),
  });
  return (
    <SettingsSection
      title="Connected AI apps"
      description="Apps you approved by signing in. Disconnecting stops them at once."
    >
      <div className="p-4">
        {isPending ? (
          <Skeleton className="h-10 w-full" />
        ) : isError ? (
          <ErrorState
            message="Couldn't load your connected apps."
            onRetry={() => void refetch()}
          />
        ) : data.length === 0 ? (
          <p className="text-muted-foreground text-meta">
            No apps yet. Connect one with the address above.
          </p>
        ) : (
          <ul aria-label="Connected AI apps" className="flex flex-col gap-3">
            {data.map((connection) => (
              <li
                key={connection.id}
                className="flex flex-wrap items-center justify-between gap-2"
              >
                <div className="min-w-0">
                  <p className="text-body font-medium break-words">
                    {connection.client_name}
                  </p>
                  <p className="text-muted-foreground text-meta">
                    Returns to {connection.redirect_host} · connected{" "}
                    <RelativeTime ts={connection.created_at} /> · last used{" "}
                    <RelativeTime
                      ts={connection.last_used_at}
                      fallback="never"
                    />
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(connection.id)}
                  aria-label={`Disconnect ${connection.client_name}`}
                >
                  Disconnect
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </SettingsSection>
  );
}

"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useViewer } from "@/components/shell/session";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { ErrorState } from "@/components/product/error-state";
import { CopyField } from "@/components/product/copy-field";
import { CodeBlock } from "@/components/product/code-block";
import { SettingsSection } from "@/components/product/setting-row";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { accountApi, type GatewayDiscovery } from "@/lib/account-api";
import { PRODUCT_NAME } from "@/lib/product";

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
        title="AI agents (MCP)"
        description="Agents that speak MCP can run this gateway's workflows."
      >
        <div className="flex flex-col gap-4 p-4">
          <CopyField label="MCP server address" value={mcpUrl} />
          <p className="text-muted-foreground text-meta">
            Transport: Streamable HTTP. Send your app key as a bearer token.
            Replace {KEY_PLACEHOLDER} with a key you{" "}
            <Link href="/apps?action=create-key" className="underline">
              create
            </Link>
            .
          </p>
          <CodeBlock snippets={mcpSnippets(mcpUrl)} />
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

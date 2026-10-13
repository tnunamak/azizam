import { networkFetch } from "./network-error";
import { ApiError, type Credential, type MintedCredential } from "./api";
import {
  rejectPendingCleanup,
  type DeletePromptsResponse,
  type MemberPromptMemoryResponse,
  type Retention,
} from "./prompt-memory";

export interface AccountApp {
  id: string;
  name: string;
  kind: string;
  disabled_at: number | null;
  created_at: number;
  /** The plan the app runs under; null when it has none (it denies). */
  access_plan: {
    id: string;
    name: string;
    summary: string | null;
    same_as_person: boolean;
  } | null;
  can_mint_keys: boolean;
  credentials: Credential[];
}
/** The Try page's key for this sign-in. A reused key comes without its
 * secret: the browser kept it from the mint. */
export type TrySessionKey = Credential & {
  app_name: string;
  expires_at: number;
} & (
    | { reused: true; api_key?: undefined }
    | { reused: false; api_key: string }
  );
export interface AccountSnapshot {
  authenticated: boolean;
  csrf_token: string;
  user?: { id: string; email: string | null; name: string; role: string };
  apps: AccountApp[];
}

async function accountRequest<T>(
  path: string,
  init: { method?: string; body?: unknown } = {},
  retried = false,
): Promise<T> {
  const headers = new Headers();
  if (init.body !== undefined) headers.set("content-type", "application/json");
  if (init.method && init.method !== "GET") {
    const session = await accountApi.session();
    headers.set("x-azizam-user-action", "1");
    headers.set("x-azizam-csrf", session.csrf_token);
  }
  const response = await networkFetch(path, {
    method: init.method,
    headers,
    credentials: "same-origin",
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (
      response.status === 403 &&
      payload?.error?.code === "user_csrf_required" &&
      !retried
    )
      return accountRequest<T>(path, init, true);
    throw new ApiError(
      payload?.error?.code ?? "unexpected_response",
      payload?.error?.message ??
        "The gateway could not complete this request. Try again.",
      response.status,
    );
  }
  return payload as T;
}
/** One MCP client's setup notes (gateway/mcp_client_notes.json). */
export type McpClientNote = {
  id: string;
  name: string;
  auth: "sign-in" | "sign-in-or-key";
  status: "documented" | "unverified";
  steps: string[];
  note?: string;
  docs: string;
};
/** The parts of /.well-known/azizam the UI reads. */
export type GatewayDiscovery = {
  recipes: Record<string, { canonical_base_url?: string } | undefined>;
  mcp?: {
    url: string;
    transport: string;
    /** True when MCP clients can sign in here (OAuth needs a public https address). */
    sign_in?: boolean;
    clients?: { checked: string; clients: McpClientNote[] };
    tools: { name: string; title: string; description: string }[];
  };
};
/** One MCP client a person connected by signing in. */
export type McpConnection = {
  id: string;
  client_name: string;
  client_id: string;
  redirect_host: string;
  created_at: number;
  last_used_at: number | null;
};
/** What the consent page shows for a pending MCP sign-in. */
export type McpConsentRequest = {
  client: {
    name: string;
    client_id: string;
    /** The domain that publishes the client's metadata, or null when the client registered itself. */
    verified_domain: string | null;
    registration: "metadata_document" | "self_registered";
    redirect_host: string;
  };
  person: { name: string; email: string | null };
  plan: { name: string; description?: string };
  can_connect: boolean;
  access: string[];
  lasts: string;
};
export const accountApi = {
  discovery: () => accountRequest<GatewayDiscovery>("/.well-known/azizam"),
  session: () => accountRequest<AccountSnapshot>("/me/api/session"),
  createApp: (name: string) =>
    accountRequest<{ app: AccountApp }>("/me/api/apps", {
      method: "POST",
      body: { name },
    }).then((r) => r.app),
  mintKey: (appId: string, label: string) =>
    accountRequest<{ credential: MintedCredential }>(
      `/me/api/apps/${encodeURIComponent(appId)}/keys`,
      { method: "POST", body: { label } },
    ).then((r) => r.credential),
  /** Reuse this sign-in's Try key, or mint one that ends with the sign-in. */
  tryKey: (reuseId: string | null) =>
    accountRequest<{ credential: TrySessionKey }>("/me/api/try-key", {
      method: "POST",
      body: reuseId ? { reuse_id: reuseId } : {},
    }).then((r) => r.credential),
  revokeKey: (id: string) =>
    accountRequest(`/me/api/credentials/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  deleteApp: (appId: string) =>
    accountRequest(`/me/api/apps/${encodeURIComponent(appId)}`, {
      method: "DELETE",
    }),
  /** The signed-in person's prompt memory and the household's rules. */
  promptMemory: () =>
    accountRequest<MemberPromptMemoryResponse>("/me/api/prompt-memory"),
  /** Turning memory off or shortening the period deletes prompts now. */
  savePromptMemory: (value: { remember: boolean; retention: Retention }) =>
    accountRequest<MemberPromptMemoryResponse>("/me/api/prompt-memory", {
      method: "PUT",
      body: value,
    }).then(rejectPendingCleanup),
  /** MCP clients this person connected by signing in. */
  mcpConnections: () =>
    accountRequest<{ connections: McpConnection[] }>(
      "/me/api/mcp/connections",
    ).then((r) => r.connections),
  /** Disconnect one: its tokens stop working at once. */
  revokeMcpConnection: (id: string) =>
    accountRequest(`/me/api/mcp/connections/${encodeURIComponent(id)}/revoke`, {
      method: "POST",
    }),
  mcpConsentRequest: (requestId: string) =>
    accountRequest<McpConsentRequest>(
      `/me/api/mcp/requests/${encodeURIComponent(requestId)}`,
    ),
  /** Answer a pending sign-in; the browser then goes to `redirect`. */
  decideMcpConsent: (requestId: string, decision: "allow" | "deny") =>
    accountRequest<{ redirect: string }>(
      `/me/api/mcp/requests/${encodeURIComponent(requestId)}`,
      { method: "POST", body: { decision } },
    ),
  deleteSavedPrompts: () =>
    accountRequest<DeletePromptsResponse>("/me/api/prompt-memory/prompts", {
      method: "DELETE",
    }),
};

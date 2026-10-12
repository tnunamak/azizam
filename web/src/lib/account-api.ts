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
  profile: { name: string; missing?: boolean } | null;
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
/** The parts of /.well-known/azizam the UI reads. */
export type GatewayDiscovery = {
  recipes: Record<string, { canonical_base_url?: string } | undefined>;
  mcp?: {
    url: string;
    transport: string;
    tools: { name: string; title: string; description: string }[];
  };
};
export const accountApi = {
  discovery: () =>
    accountRequest<GatewayDiscovery>("/.well-known/azizam"),
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
  deleteSavedPrompts: () =>
    accountRequest<DeletePromptsResponse>("/me/api/prompt-memory/prompts", {
      method: "DELETE",
    }),
};

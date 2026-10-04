import { ApiError, type Credential, type MintedCredential } from "./api";

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
    headers.set("x-ai-gateway-user-action", "1");
    headers.set("x-ai-gateway-csrf", session.csrf_token);
  }
  const response = await fetch(path, {
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
export const accountApi = {
  discovery: () =>
    accountRequest<{ recipes: { openai?: { canonical_base_url?: string } } }>(
      "/.well-known/ai-gateway",
    ),
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
  revokeKey: (id: string) =>
    accountRequest(`/me/api/credentials/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  deleteApp: (appId: string) =>
    accountRequest(`/me/api/apps/${encodeURIComponent(appId)}`, {
      method: "DELETE",
    }),
};

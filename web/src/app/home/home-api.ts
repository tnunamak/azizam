import { ApiError, adminFetch } from "@/lib/api";
import { accountApi, type AccountApp } from "@/lib/account-api";
import type { AvailabilityStatus } from "@/components/product/status-pill";

export interface HomeCapability {
  capability: string;
  intent: string;
  model?: string | null;
  workflow?: string | null;
  available: boolean;
  status: AvailabilityStatus;
  reason: string;
}
export interface HomeAlias {
  id: string;
  operation: string;
  available: boolean;
  status: AvailabilityStatus;
  reason: string;
}
export interface HomeReceipt {
  request_id: string;
  intent?: string;
  path?: string;
  requested_model?: string | null;
  status?: string | null;
  elapsed_ms?: number | null;
  latency_ms?: number | null;
  _stored_at?: number | null;
  accepted_at_ms?: number | null;
}
export interface HomeSnapshot {
  authenticated: boolean;
  user: { id: string; name: string; email: string | null; role: string };
  apps: AccountApp[];
  csrf_token: string;
  public_base_url: string;
  api_base_url: string;
  capabilities: HomeCapability[];
  aliases: HomeAlias[];
  credential_aliases: Record<string, HomeAlias[]>;
  new_app_aliases: HomeAlias[];
  status: { state: AvailabilityStatus; reason: string };
  receipts: HomeReceipt[];
  creations: null;
}
export interface SetupProgress {
  complete: boolean;
  steps: Array<{
    id: string;
    done: boolean;
    optional: boolean;
    reason: string;
    href: string;
  }>;
}
export interface ConnectionResult {
  ok: boolean;
  latency_ms: number | null;
  reason: string;
  path: string;
  model: string | null;
  request_id: string | null;
}

async function homeRequest<T>(
  path: string,
  body?: unknown,
  retried = false,
): Promise<T> {
  const headers = new Headers();
  if (body !== undefined) {
    const session = await accountApi.session();
    headers.set("content-type", "application/json");
    headers.set("x-ai-gateway-user-action", "1");
    headers.set("x-ai-gateway-csrf", session.csrf_token);
  }
  const response = await fetch(path, {
    credentials: "same-origin",
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (
      body !== undefined &&
      !retried &&
      response.status === 403 &&
      payload?.error?.code === "user_csrf_required"
    ) {
      return homeRequest<T>(path, body, true);
    }
    throw new ApiError(
      payload?.error?.code ?? "unexpected_response",
      payload?.error?.message ??
        "The gateway could not complete this request. Try again.",
      response.status,
    );
  }
  return payload as T;
}

export const homeApi = {
  home: () => homeRequest<HomeSnapshot>("/me/api/home"),
  setup: () => adminFetch<SetupProgress>("/admin/api/setup-progress"),
  test: (apiKey: string, credentialId?: string) =>
    homeRequest<ConnectionResult>("/me/api/connection-test", {
      api_key: apiKey,
      ...(credentialId ? { credential_id: credentialId } : {}),
    }),
};

import { ApiError } from "@/lib/api";
import { accountApi } from "@/lib/account-api";
import {
  productApi,
  type HomeCapability as SharedHomeCapability,
  type HomeSnapshot as SharedHomeSnapshot,
  type SetupProgress as SharedSetupProgress,
} from "@/lib/product-api";

export type { HomeAlias, HomeReceipt } from "@/lib/product-api";
export type SetupProgress = SharedSetupProgress & {
  dismissed?: boolean;
  evidence: SharedSetupProgress["evidence"] & {
    observed_external_client?: { recorded: boolean; source: string };
  };
};
// A display tile may omit a routing name; the wire contract stays generated.
export type HomeCapability = Pick<
  SharedHomeCapability,
  "capability" | "intent" | "available" | "status" | "reason_code"
> & {
  capability: string;
  intent: string;
  available: boolean;
  status: SharedHomeCapability["status"];
  model?: string | null;
  workflow?: string | null;
  availability?: SharedHomeCapability["availability"];
  title?: string;
  use_name?: string;
  capability_title?: string;
  icon_key?: string;
  recommended_model?: string;
  use_names?: string[];
  summary?: string;
};
export type HomeSnapshot = SharedHomeSnapshot & {
  user: NonNullable<SharedHomeSnapshot["user"]>;
  capabilities: HomeCapability[];
};
export interface HomeCreation {
  asset_id: string;
  kind: "image" | "video" | "audio";
  prompt?: string | null;
  model?: string | null;
  created_at?: number | string | null;
  download_url?: string | null;
  media_type?: string | null;
  poster_asset_id?: string | null;
}
export interface HomeCreationsResponse {
  assets: HomeCreation[];
  next_cursor?: string | null;
  has_more?: boolean;
}

export function hasHomeCreationContent(creation: HomeCreation): boolean {
  if (creation.kind === "image") return Boolean(creation.download_url);
  if (creation.kind === "video") return Boolean(creation.poster_asset_id);
  return Boolean(creation.download_url && creation.prompt?.trim());
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
    headers.set("x-azizam-user-action", "1");
    headers.set("x-azizam-csrf", session.csrf_token);
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
  home: async (): Promise<
    HomeSnapshot | { authenticated: false; user?: never }
  > => {
    try {
      return (await productApi.home()) as HomeSnapshot;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401)
        return { authenticated: false };
      throw error;
    }
  },
  creations: async (): Promise<HomeCreationsResponse> => {
    const assets: HomeCreation[] = [];
    let cursor: string | null | undefined;
    let hasMore = true;
    while (hasMore && assets.length < 4) {
      const query = new URLSearchParams({ limit: "50" });
      if (cursor) query.set("cursor", cursor);
      const page = await homeRequest<HomeCreationsResponse>(
        `/me/api/media?${query.toString()}`,
      );
      assets.push(...page.assets.filter(hasHomeCreationContent));
      cursor = page.next_cursor;
      hasMore = Boolean(page.has_more && cursor);
    }
    return { assets: assets.slice(0, 4) };
  },
  mediaDownload: (assetId: string) =>
    homeRequest<{ download_url?: string }>(
      `/me/api/media/${encodeURIComponent(assetId)}/download?scope=me`,
    ),
  setup: productApi.setup,
  dismissSetup: () =>
    homeRequest<{ dismissed: boolean }>("/admin/api/setup-dismiss", {}),
  test: (apiKey: string, credentialId?: string) =>
    homeRequest<ConnectionResult>("/me/api/connection-test", {
      api_key: apiKey,
      ...(credentialId ? { credential_id: credentialId } : {}),
    }),
};

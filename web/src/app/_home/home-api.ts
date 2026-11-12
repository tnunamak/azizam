import { ApiError } from "@/lib/api";
import { REAUTH_REQUIRED_CODE } from "@/lib/reauth";
import { accountApi } from "@/lib/account-api";
import { networkFetch } from "@/lib/network-error";
import {
  productApi,
  type HomeCapability as SharedHomeCapability,
  type HomeSnapshot as SharedHomeSnapshot,
  type SetupProgress as SharedSetupProgress,
} from "@/lib/product-api";

export type { HomeReceipt } from "@/lib/product-api";
export type SetupProgress = SharedSetupProgress & {
  dismissed?: boolean;
  evidence: SharedSetupProgress["evidence"] & {
    observed_external_client?: { recorded: boolean; source: string };
  };
};
export type HomeCapability = SharedHomeCapability;
export type HomeSnapshot = SharedHomeSnapshot & {
  user: NonNullable<SharedHomeSnapshot["user"]>;
};
export type HomeChat = NonNullable<SharedHomeSnapshot["recent_chats"]>[number];

/** The newest media Home shows: one page, which is what the six Recent tiles need. */
export const RECENT_MEDIA_LIMIT = 8;

/** The server's words for a kind, and whether Recent shows it (registry/asset_kinds.yml). */
export interface HomeKindInfo {
  label: string;
  untitled: string;
  recent: boolean;
}

export interface HomeCreation {
  asset_id: string;
  /** Any kind the gateway names; the tile draws the ones it has a renderer for and shows a file tile for the rest. */
  kind: string;
  /** Set by `homeApi.creations` from the page's kind words; a row without it reads as a neutral file. */
  kind_info?: HomeKindInfo;
  prompt?: string | null;
  model?: string | null;
  created_at?: number | string | null;
  download_url?: string | null;
  media_type?: string | null;
  size_bytes?: number | null;
  poster_asset_id?: string | null;
  available?: boolean;
}
export interface HomeCreationsResponse {
  assets: HomeCreation[];
  /** The words for each kind in the Library, as `GET /me/api/media` sends them. */
  kinds?: unknown[];
  next_cursor?: string | null;
  has_more?: boolean;
  partial?: boolean;
}

/** Recent keeps a row unless the server's words for its kind say not to show it. */
export function hasHomeCreationContent(creation: HomeCreation): boolean {
  return creation.kind_info?.recent !== false;
}

/** The words per kind from the page's `kinds` list; rows that are not well-formed are ignored. */
function kindInfoByKind(rows: readonly unknown[] | undefined): Map<string, HomeKindInfo> {
  const byKind = new Map<string, HomeKindInfo>();
  for (const row of rows ?? []) {
    if (!row || typeof row !== "object") continue;
    const { kind, label, untitled, recent } = row as Record<string, unknown>;
    if (typeof kind !== "string" || typeof label !== "string" || !label.trim()) continue;
    byKind.set(kind, {
      label: label.trim(),
      untitled: typeof untitled === "string" && untitled.trim() ? untitled.trim() : label.trim(),
      recent: recent !== false,
    });
  }
  return byKind;
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
  // Through networkFetch, so the session guard applies; `true` keeps the head-start fast path.
  const response = await networkFetch(path, {
    credentials: "same-origin",
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  }, true);
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
      if (
        error instanceof ApiError &&
        error.status === 401 &&
        error.code !== REAUTH_REQUIRED_CODE
      )
        return { authenticated: false };
      throw error;
    }
  },
  /** The newest media the person owns: a single page, so Recent costs one request. */
  creations: async (): Promise<HomeCreationsResponse> => {
    const page = await homeRequest<HomeCreationsResponse>(
      `/me/api/media?limit=${RECENT_MEDIA_LIMIT}`,
    );
    const words = kindInfoByKind(page.kinds);
    const assets = page.assets
      .map((row) => (words.has(row.kind) ? { ...row, kind_info: words.get(row.kind) } : row))
      .filter(hasHomeCreationContent);
    return { ...page, assets };
  },
  mediaDownload: (assetId: string) =>
    homeRequest<{ download_url?: string }>(
      `/me/api/media/${encodeURIComponent(assetId)}/download`,
    ),
  /** Asks the owner for an access plan. The request carries no content. */
  requestAccess: () => homeRequest<unknown>("/me/api/access-request", {}),
  setup: productApi.setup,
  dismissSetup: () =>
    homeRequest<{ dismissed: boolean }>("/admin/api/setup-dismiss", {}),
};

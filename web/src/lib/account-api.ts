import { networkFetch } from "./network-error";
import { ApiError, type AdministratorsOnRecord, type Credential, type MintedCredential } from "./api";
import { parseReauth, requestReauth } from "./reauth";
import {
  REQUEST_HISTORY_PAGE_SIZE,
  type ReceiptRetentionEntry,
  type RequestHistoryChoice,
  type RequestHistoryPage,
  type RequestHistoryRecord,
} from "./request-history";
import {
  learnContract,
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
  /** Trusted addresses on this app that the cut-over would remove, each with a remedy. Sent to the owner only. */
  addresses_to_check?: number;
}
/** A replacement: the new key (its secret, shown once, in `credential.api_key`) and the id of the key it replaced. */
export interface ReplacedKey {
  credential: MintedCredential;
  replaced_credential_id: string;
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
/** A person's avatar as the gateway stores it. `picture_url` is a gateway path, never the sign-in provider's address. */
export interface AvatarSetting {
  choice: string;
  person_id: string;
  /** The picture the avatar shows now: the sign-in picture, or the one the gateway made. */
  picture_url: string | null;
  /** The person's own previews of each picture they have (absent for anyone else). */
  sign_in_picture_url?: string | null;
  made_picture_url?: string | null;
  /** The automatic picture is being made for the person (their own view only). */
  generating?: boolean;
}

/** A make job as the make page reads it: its state, and its pictures once it has succeeded. */
export interface MakeJob {
  job_id: string;
  status: string;
  assets?: Array<{ asset_id: string; media_type?: string | null }>;
}
export interface AccountSnapshot {
  authenticated: boolean;
  csrf_token: string;
  user?: {
    id: string;
    /** The stable id the generated avatar is drawn from. */
    person_id?: string;
    avatar?: AvatarSetting;
    email: string | null;
    name: string;
    role: string;
    /** The person's own access plan; a new app starts with it. Null: none. */
    access_plan?: { id: string; name: string; summary: string | null } | null;
  };
  apps: AccountApp[];
}

async function accountRequest<T>(
  path: string,
  init: { method?: string; body?: unknown } = {},
  retried = false,
  confirmed = false,
  renewed = false,
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
      return accountRequest<T>(path, init, true, confirmed, renewed);
    // A write answered `login_required` wrote nothing (decision D41), so it is safe to send again once: the
    // session may have renewed while the request was in flight, and the second try carries the new cookie.
    if (
      response.status === 401 &&
      payload?.error?.code === "login_required" &&
      init.method &&
      init.method !== "GET" &&
      !renewed
    )
      return accountRequest<T>(path, init, retried, confirmed, true);
    // Connecting a client from an old sign-in asks the person to confirm it is them first,
    // then sends the request once more. A person's own keys are not asked (gateway/step_up.py).
    const reauth =
      response.status === 401 && !confirmed ? parseReauth(payload) : null;
    if (reauth && (await requestReauth(reauth)))
      return accountRequest<T>(path, init, retried, true, renewed);
    throw new ApiError(
      payload?.error?.code ?? "unexpected_response",
      payload?.error?.message ??
        "The gateway could not complete this request. Try again.",
      response.status,
      payload,
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
    /** True when MCP clients can sign in here (OAuth needs a public https address). */
    sign_in?: boolean;
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
/** Keeping a binding that administrators on record could also have made needs the owner's confirmation. */
export interface KeepOptions {
  /** The owner has read the administrators and confirms them. */
  confirmed?: boolean;
}

const keepBody = ({ confirmed = false }: KeepOptions) =>
  confirmed ? { confirm_admins_could_have_made_it: true } : undefined;

export type { AdministratorsOnRecord } from "./api";

/** True when the gateway names anyone the owner must confirm before a keep. */
export function hasAdministrators(administrators?: AdministratorsOnRecord | null): boolean {
  return !!administrators && (administrators.people.length > 0 || administrators.unnamed_people > 0 || administrators.admin_keys > 0);
}

/** The administrators on record in one line, as the owner reads them: each person by name, then the people with no name
 * and the administrator keys as counts, with empty parts left out. The gateway renders the same sentence. */
export function administratorsSentence(administrators: AdministratorsOnRecord): string {
  const items = [
    ...administrators.people,
    ...(administrators.unnamed_people > 0
      ? [`${administrators.unnamed_people} ${administrators.unnamed_people === 1 ? "person" : "people"} with no name`]
      : []),
    ...(administrators.admin_keys > 0
      ? [`${administrators.admin_keys} administrator ${administrators.admin_keys === 1 ? "key" : "keys"}`]
      : []),
  ];
  if (items.length <= 2) return items.join(" and ");
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}

function isAdministratorsOnRecord(value: unknown): value is AdministratorsOnRecord {
  const record = value as Partial<AdministratorsOnRecord> | null;
  return typeof record === "object" && record !== null && Array.isArray(record.people)
    && typeof record.unnamed_people === "number" && typeof record.admin_keys === "number";
}

/** The administrators the gateway asks the owner to confirm before a keep (409 `keep_needs_confirmation`), or null
 * for any other failure. A stale snapshot can send a keep that the gateway still wants confirmed. */
export function administratorsToConfirm(caught: unknown): AdministratorsOnRecord | null {
  if (!(caught instanceof ApiError) || caught.code !== "keep_needs_confirmation") return null;
  const administrators = (caught.payload as { error?: { administrators?: unknown } } | undefined)?.error?.administrators;
  return isAdministratorsOnRecord(administrators) ? administrators : { people: [], unnamed_people: 0, admin_keys: 0 };
}

export const accountApi = {
  discovery: () => accountRequest<GatewayDiscovery>("/.well-known/azizam"),
  session: () => accountRequest<AccountSnapshot>("/me/api/session"),
  createApp: (name: string) =>
    accountRequest<{ app: AccountApp }>("/me/api/apps", {
      method: "POST",
      body: { name },
    }).then((r) => r.app),
  /** Rename an app. The answer is the saved record: show its name, not the one sent. */
  renameApp: (appId: string, name: string) =>
    accountRequest<{ app: AccountApp }>(
      `/me/api/apps/${encodeURIComponent(appId)}`,
      { method: "PATCH", body: { name } },
    ).then((r) => r.app),
  /** Rename a key. The answer is the saved record: show its label, not the one sent. */
  renameKey: (credentialId: string, displayName: string) =>
    accountRequest<{ credential: Credential }>(
      `/me/api/credentials/${encodeURIComponent(credentialId)}`,
      { method: "PATCH", body: { display_name: displayName } },
    ).then((r) => r.credential),
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
  /** Keep a key made before its creator was recorded: this person becomes its creator. The answer is the saved record. */
  keepKey: (credentialId: string, options: KeepOptions = {}) =>
    accountRequest<{ credential: Credential }>(
      `/me/api/credentials/${encodeURIComponent(credentialId)}/adopt`,
      { method: "POST", body: keepBody(options) },
    ).then((r) => r.credential),
  /** Keep a trusted address made before its creator was recorded: this person becomes its creator. */
  keepTrustedAddress: (appId: string, entryId: string, options: KeepOptions = {}) =>
    accountRequest(
      `/me/api/apps/${encodeURIComponent(appId)}/trusted-addresses/${encodeURIComponent(entryId)}/adopt`,
      { method: "POST", body: keepBody(options) },
    ),
  /** Add an address that cannot be kept again, in one step: the same address replaces the old entry, made by this
   * person. An app holds each address once, so removing and adding would be two calls that can leave it removed. */
  readdTrustedAddress: (appId: string, entryId: string) =>
    accountRequest<{ trusted_address: { id: string }; replaced_entry_id: string }>(
      `/me/api/apps/${encodeURIComponent(appId)}/trusted-addresses/${encodeURIComponent(entryId)}/readd`,
      { method: "POST" },
    ),
  /** Replace a key that cannot be kept: a new key on the same app, and this one revoked. The secret comes back once. */
  replaceKey: (credentialId: string) =>
    accountRequest<ReplacedKey>(
      `/me/api/credentials/${encodeURIComponent(credentialId)}/replace`,
      { method: "POST" },
    ),
  /** Trust an address on an app the person answers for. An app holds each address once. */
  addTrustedAddress: (
    appId: string,
    entry: { address: string; note: string },
  ) =>
    accountRequest<{ trusted_address: { id: string } }>(
      `/me/api/apps/${encodeURIComponent(appId)}/trusted-addresses`,
      {
        method: "POST",
        body: entry,
      },
    ),
  /** Stop trusting one address on an app the person answers for. */
  removeTrustedAddress: (appId: string, entryId: string) =>
    accountRequest(
      `/me/api/apps/${encodeURIComponent(appId)}/trusted-addresses/${encodeURIComponent(entryId)}`,
      { method: "DELETE" },
    ),
  deleteApp: (appId: string) =>
    accountRequest(`/me/api/apps/${encodeURIComponent(appId)}`, {
      method: "DELETE",
    }),
  /** The signed-in person's prompt memory and the household's rules. */
  promptMemory: () =>
    accountRequest<MemberPromptMemoryResponse>("/me/api/prompt-memory").then(
      learnContract,
    ),
  /** Turning memory off or shortening the period deletes prompts now. */
  savePromptMemory: (value: { remember: boolean; retention: Retention }) =>
    accountRequest<MemberPromptMemoryResponse>("/me/api/prompt-memory", {
      method: "PUT",
      body: value,
    })
      .then(rejectPendingCleanup)
      .then(learnContract),
  /** The signed-in person's request history choice (REQUEST_RETENTION.md section 10). */
  requestHistory: () => accountRequest<RequestHistoryChoice>("/me/api/retention"),
  /** Saves the choice. Turning it off erases what was kept, so a screen refetches the list after it. */
  saveRequestHistory: (value: { keep: boolean; retention: string; parts: string[] }) =>
    accountRequest<RequestHistoryChoice>("/me/api/retention", {
      method: "PUT",
      body: value,
    }),
  /** Dismisses the one-time notice for a member who predates request history. The choice does not change. */
  dismissRequestHistoryNotice: () =>
    accountRequest<{ notice_due: boolean }>("/me/api/retention/notice-seen", {
      method: "POST",
    }),
  /** One page of the person's kept requests, newest first. Metadata only. */
  requestHistoryPage: (cursor: string | null) =>
    accountRequest<RequestHistoryPage>(
      `/gateway/v1/requests?limit=${REQUEST_HISTORY_PAGE_SIZE}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    ),
  /** One kept request with its content. */
  requestHistoryRecord: (recordId: string) =>
    accountRequest<RequestHistoryRecord>(
      `/gateway/v1/requests/${encodeURIComponent(recordId)}`,
    ),
  deleteRequestHistoryRecord: (recordId: string) =>
    accountRequest(`/gateway/v1/requests/${encodeURIComponent(recordId)}`, {
      method: "DELETE",
    }),
  /** Deletes every kept request. `pending` means the rows are hidden now and deleted in the background. */
  deleteAllRequestHistory: () =>
    accountRequest<{ deleted: number; pending?: boolean }>("/gateway/v1/requests", {
      method: "DELETE",
    }),
  /** The receipt for one request, as far as the person may read it. Its `retention` list says what was kept. */
  receiptRetention: (requestId: string) =>
    accountRequest<{ retention?: ReceiptRetentionEntry[] }>(
      `/me/api/receipts/${encodeURIComponent(requestId)}`,
    ),
  /**
   * The Library item this request made, or null when it made none. The Library is searched by the start of the
   * prompt (the search is a substring match), and the item must name this request.
   */
  libraryItemForRequest: (requestId: string, prompt: string) =>
    accountRequest<{ assets?: Array<{ asset_id?: string; request_id?: string | null }> }>(
      `/me/api/media?q=${encodeURIComponent(prompt.slice(0, 100))}`,
    ).then(
      (page) => page.assets?.find((asset) => asset.request_id === requestId)?.asset_id ?? null,
    ),
  /** The signed-in person's avatar choice. */
  avatar: () => accountRequest<AvatarSetting>("/me/api/profile/avatar"),
  /** `choice`: auto, v1 to v8, picture, or generated (only once a picture is kept). The gateway refuses anything else. */
  saveAvatar: (choice: string) =>
    accountRequest<AvatarSetting>("/me/api/profile/avatar", {
      method: "PUT",
      body: { choice },
    }),
  /** Whether this gateway has an image route to make the person's avatar with. */
  avatarGenerator: () => accountRequest<{ available: boolean }>("/me/api/profile/avatar/generator"),
  /** The automatic picture for a person with no sign-in picture and no chosen avatar. The gateway makes it once. */
  startDefaultAvatar: () =>
    accountRequest<AvatarSetting>("/me/api/profile/avatar/default", { method: "POST" }),
  /** Start one attempt at a picture: a make job, with a new seed each time. */
  startAvatarGeneration: () =>
    accountRequest<{ job: MakeJob }>("/me/api/profile/avatar/generate", { method: "POST" }),
  /** A make job the person started, with its pictures once it has succeeded. */
  makeJob: (jobId: string) =>
    accountRequest<{ job: MakeJob }>(`/me/api/make/jobs/${encodeURIComponent(jobId)}`),
  /** A signed link that shows one of the person's own pictures. */
  mediaDownloadUrl: (assetId: string) =>
    accountRequest<{ download_url?: string | null }>(
      `/me/api/media/${encodeURIComponent(assetId)}/download`,
    ).then((link) => link.download_url ?? null),
  /** Keep one of the person's own pictures as their avatar. */
  keepGeneratedAvatar: (assetId: string) =>
    accountRequest<AvatarSetting>("/me/api/profile/avatar/generated", {
      method: "POST",
      body: { asset_id: assetId },
    }),
  /** Remove the picture the gateway made. */
  removeGeneratedAvatar: () =>
    accountRequest<AvatarSetting>("/me/api/profile/avatar/generated", { method: "DELETE" }),
  /** Avatar choices the viewer may see: their own, and everyone's for an owner or admin. */
  avatars: () =>
    accountRequest<{
      people: Record<string, { choice: string; picture_url: string | null }>;
    }>("/me/api/profile/avatars").then((r) => r.people),
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

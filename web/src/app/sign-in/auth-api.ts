import { ApiError } from "@/lib/api";
import { networkFetch } from "@/lib/network-error";
import { uiPages } from "@/lib/product";
import type { RememberedList } from "@/lib/remembered-people";

export interface SignInOptions {
  built_in: boolean;
  oidc: boolean;
  oidc_label: string;
  oidc_url: string;
  instance_name: string;
  owner_name: string;
  /** The owner's own password works even while built-in sign-in is off. */
  owner_password?: boolean;
  signed_in_oidc?: { email: string; name: string; csrf_token: string } | null;
}
export async function authFetch<T>(
  path: string,
  body?: object,
  headers?: Record<string, string>,
): Promise<T> {
  return authRequest<T>(body ? "POST" : "GET", path, body, headers);
}

export async function authRequest<T>(
  method: "GET" | "POST" | "DELETE",
  path: string,
  body?: object,
  headers?: Record<string, string>,
): Promise<T> {
  // Session-bound like every other request: the answer can name the person who is signed in (signed_in_oidc).
  const response = await networkFetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    ...(body
      ? {
          headers: { "Content-Type": "application/json", ...headers },
          body: JSON.stringify(body),
        }
      : {}),
  });
  const result = await response.json();
  if (!response.ok) {
    if (
      result &&
      typeof result === "object" &&
      "error" in result &&
      result.error &&
      typeof result.error === "object" &&
      "code" in result.error &&
      "message" in result.error &&
      typeof result.error.code === "string" &&
      typeof result.error.message === "string"
    )
      throw new ApiError(
        result.error.code,
        result.error.message,
        response.status,
      );
    throw new Error(
      result.error?.message ||
        result.detail ||
        "The gateway could not complete this request. Try again.",
    );
  }
  return result;
}
// Where sign-in may send someone next (D2.8). The rule and its cases are
// shared with the gateway's sanitizer: tests/fixtures/return_paths.json.
// Allowed: "/" or "/{page}/" for a page in gateway/ui_pages.json, and
// "/status/raw" or "/status/raw/", each with an optional query and fragment.
// Everything else falls back to Home.
const STATUS_RAW_PATHS = ["/status/raw", "/status/raw/"];
export function safeContinuePath(next: string): string {
  const fallback = "/";
  if (
    !next.startsWith("/") ||
    next.startsWith("//") ||
    /[\\\x00-\x1f\x7f]/.test(next)
  )
    return fallback;
  const path = next.split(/[?#]/, 1)[0];
  const segments = path.split("/").slice(1);
  for (const [index, segment] of segments.entries()) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      return fallback;
    }
    const last = index === segments.length - 1;
    if ((segment === "" && !last) || [segment, decoded].some(isDotSegment))
      return fallback;
  }
  const name = path.slice(1, -1);
  const page =
    path === "/" ||
    (path.endsWith("/") && name !== "" && !name.includes("/") && Object.hasOwn(uiPages, name));
  return page || STATUS_RAW_PATHS.includes(path) ? next : fallback;
}
function isDotSegment(segment: string): boolean {
  return segment === "." || segment === "..";
}
export function continueTo(next: string) {
  window.location.assign(safeContinuePath(next));
}

/** The names this browser remembers. Needs no session: the list is for whoever is at the computer. */
export function rememberedList(): Promise<RememberedList> {
  return authFetch<RememberedList>("/auth/api/remembered");
}

/** Takes one name off this browser's list. It signs no one out and changes no account. */
export function forgetRemembered(tile: string): Promise<{ ok: boolean }> {
  return authRequest<{ ok: boolean }>("DELETE", `/auth/api/remembered/${encodeURIComponent(tile)}`);
}

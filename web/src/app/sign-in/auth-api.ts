import { ApiError } from "@/lib/api";

export interface SignInOptions {
  built_in: boolean;
  oidc: boolean;
  oidc_label: string;
  oidc_url: string;
  instance_name: string;
  owner_name: string;
  signed_in_oidc?: { email: string; name: string; csrf_token: string } | null;
}
export async function authFetch<T>(
  path: string,
  body?: object,
  headers?: Record<string, string>,
): Promise<T> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
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
export function safeContinuePath(next: string): string {
  if (
    !next.startsWith("/") ||
    next.startsWith("//") ||
    /[\\\x00-\x1f\x7f]/.test(next)
  )
    return "/ui/home/";
  const url = new URL(next, window.location.origin);
  if (url.origin !== window.location.origin) return "/ui/home/";
  return url.pathname.startsWith("/ui/") ||
    ["/setup", "/setup/", "/status/raw", "/status/raw/"].includes(url.pathname)
    ? next
    : "/ui/home/";
}
export function continueTo(next: string) {
  window.location.assign(safeContinuePath(next));
}

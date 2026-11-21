import { accountApi } from "./account-api";
import { networkFetch } from "./network-error";

export const ADAPTER_UPLOAD_PATH = "/me/api/adapters";

async function post(init: { method: "POST"; body: FormData }): Promise<Response> {
  const session = await accountApi.session();
  return networkFetch(ADAPTER_UPLOAD_PATH, {
    ...init,
    credentials: "same-origin",
    headers: { "x-azizam-user-action": "1", "x-azizam-csrf": session.csrf_token },
  });
}

/**
 * The `send` that `uploadAdapter` takes: one signed-in member POST. Like every member write it carries the
 * user-action header and the session's CSRF token. It sets no content type, so the browser adds the multipart
 * boundary. A stale token answers 403 `user_csrf_required` before anything is written (the same rule
 * `accountRequest` follows), so that one answer is sent again once with a fresh session read.
 */
export async function sendAdapterUpload(init: { method: "POST"; body: FormData }): Promise<Response> {
  const response = await post(init);
  if (response.status !== 403) return response;
  const refusal = await response.clone().json().catch(() => null);
  return refusal?.error?.code === "user_csrf_required" ? post(init) : response;
}

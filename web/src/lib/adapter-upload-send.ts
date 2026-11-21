import { accountApi } from "./account-api";
import { networkFetch } from "./network-error";

export const ADAPTER_UPLOAD_PATH = "/me/api/adapters";

/**
 * The `send` that `uploadAdapter` takes: one signed-in member POST. Like every member write it carries the
 * user-action header and the session's CSRF token. It sets no content type, so the browser adds the multipart
 * boundary.
 */
export async function sendAdapterUpload(init: { method: "POST"; body: FormData }): Promise<Response> {
  const session = await accountApi.session();
  return networkFetch(ADAPTER_UPLOAD_PATH, {
    ...init,
    credentials: "same-origin",
    headers: { "x-azizam-user-action": "1", "x-azizam-csrf": session.csrf_token },
  });
}

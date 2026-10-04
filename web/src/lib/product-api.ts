import { accountApi } from "./account-api";
import { ApiError, adminFetch } from "./api";
import type {
  MemberStatus,
  ModelsCatalog,
  SetupEvidenceRequest,
  SetupEvidenceResponse,
  SetupProgress,
} from "./product-api-types";

export type * from "./product-api-types";

async function memberGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      payload?.error?.code ?? "unexpected_response",
      payload?.error?.message ?? "The gateway could not complete this request.",
      response.status,
    );
  }
  return payload as T;
}

export const productApi = {
  status: () => memberGet<MemberStatus>("/me/api/status"),
  models: () => memberGet<ModelsCatalog>("/me/api/models/catalog"),
  setup: () => adminFetch<SetupProgress>("/admin/api/setup-progress"),
  recordSetupEvidence: async (
    event: SetupEvidenceRequest,
  ): Promise<SetupEvidenceResponse> => {
    const session = await accountApi.session();
    const response = await fetch("/admin/api/setup-evidence", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "content-type": "application/json",
        "x-ai-gateway-user-action": "1",
        "x-ai-gateway-csrf": session.csrf_token,
      },
      body: JSON.stringify(event),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        payload?.error?.code ?? "unexpected_response",
        payload?.error?.message ?? "The setup evidence could not be saved.",
        response.status,
      );
    }
    return payload as SetupEvidenceResponse;
  },
};

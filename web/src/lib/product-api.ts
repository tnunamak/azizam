import { accountApi, type AccountSnapshot } from "./account-api";
import { ApiError, adminFetch } from "./api";
import { scrubModelPaths } from "./model-name";
import type {
  MemberStatus,
  HomeProjection,
  ModelsCatalog,
  SetupEvidenceRequest,
  SetupEvidenceResponse,
  SetupProgress,
} from "./product-api-types";

export type * from "./product-api-types";
export type HomeSnapshot = AccountSnapshot & HomeProjection;

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
  // A model can be registered by file path; screens show its file name only.
  return scrubModelPaths(payload) as T;
}

export const productApi = {
  home: () => memberGet<HomeSnapshot>("/me/api/home"),
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
        "x-azizam-user-action": "1",
        "x-azizam-csrf": session.csrf_token,
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

import type { Credential, StatusPayload } from "@/lib/api";

export function isActiveKey(credential: Credential): boolean {
  return credential.revoked_at === null;
}

/** A data-plane credential is valid for an app only when both stored IDs agree. */
export function isActiveAppCredentialFor(
  credential: Credential,
  appId: string,
): boolean {
  return (
    isActiveKey(credential) &&
    credential.subject_kind === "app" &&
    credential.app_id === appId &&
    credential.subject_id === appId
  );
}

export function appCredentialFacts(
  credentials: Credential[] | undefined,
  appId: string,
) {
  if (credentials === undefined) return undefined;

  const appCredentials = credentials.filter(
    (credential) =>
      credential.subject_kind === "app" &&
      credential.app_id === appId &&
      credential.subject_id === appId,
  );
  return {
    activeKeyCount: appCredentials.filter(isActiveKey).length,
    lastUsedAt: appCredentials.reduce<number | null>((latest, credential) => {
      const usedAt = credential.last_used_at;
      return usedAt !== null && (latest === null || usedAt > latest)
        ? usedAt
        : latest;
    }, null),
  };
}

export function buildTelemetryCards(status: StatusPayload | undefined) {
  const authKeys = status?.auth_keys ?? [];
  const requestWindowAvailable =
    authKeys.length > 0 &&
    authKeys.every(
      (key) =>
        typeof key.requests_24h === "number" &&
        Number.isFinite(key.requests_24h),
    );
  const errorWindowAvailable =
    authKeys.length > 0 &&
    authKeys.every(
      (key) =>
        typeof key.errors_4xx_24h === "number" &&
        Number.isFinite(key.errors_4xx_24h) &&
        typeof key.errors_5xx_24h === "number" &&
        Number.isFinite(key.errors_5xx_24h),
    );

  return {
    requests24h: requestWindowAvailable
      ? authKeys.reduce((sum, key) => sum + key.requests_24h!, 0)
      : null,
    requestWindowAvailable,
    failedRequests24h: errorWindowAvailable
      ? authKeys.reduce(
          (sum, key) => sum + key.errors_4xx_24h! + key.errors_5xx_24h!,
          0,
        )
      : null,
    errorWindowAvailable,
  };
}

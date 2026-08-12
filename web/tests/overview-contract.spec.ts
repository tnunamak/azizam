import { describe, expect, it } from "vitest";

import {
  appCredentialFacts,
  buildTelemetryCards,
  isActiveAppCredentialFor,
} from "@/app/admin/overview-derivations";
import type { Credential, StatusPayload } from "@/lib/api";

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: "cred_1",
    subject_kind: "app",
    subject_id: "app_1",
    app_id: "app_1",
    profile_id: null,
    prefix: "opk_test",
    display_name: "Test key",
    scopes: [],
    created_at: 1,
    expires_at: null,
    revoked_at: null,
    last_used_at: null,
    ...overrides,
  };
}

describe("Admin Overview derivations", () => {
  it("completes an app key only from that app's active data-plane credential", () => {
    const unrelatedAdminKey = credential({
      id: "cred_admin",
      subject_kind: "service_account",
      subject_id: "svc_1",
      app_id: null,
    });
    const otherAppKey = credential({
      id: "cred_other",
      subject_id: "app_2",
      app_id: "app_2",
    });
    const exactAppKey = credential();

    expect(isActiveAppCredentialFor(unrelatedAdminKey, "app_1")).toBe(false);
    expect(isActiveAppCredentialFor(otherAppKey, "app_1")).toBe(false);
    expect(isActiveAppCredentialFor(exactAppKey, "app_1")).toBe(true);
    expect(
      isActiveAppCredentialFor(credential({ revoked_at: 2 }), "app_1"),
    ).toBe(false);
  });

  it("keeps app credential facts unavailable until credentials load", () => {
    expect(appCredentialFacts(undefined, "app_1")).toBeUndefined();
    expect(appCredentialFacts([], "app_1")).toEqual({
      activeKeyCount: 0,
      lastUsedAt: null,
    });
  });

  it("does not present partial key telemetry as a gateway-wide total", () => {
    const partial: StatusPayload = {
      auth_keys: [
        {
          app_id: "app_1",
          requests_24h: 8,
          errors_4xx_24h: 1,
          errors_5xx_24h: 0,
        },
        {
          app_id: "app_2",
          requests_24h: null,
          errors_4xx_24h: null,
          errors_5xx_24h: 0,
        },
      ],
    };
    const complete: StatusPayload = {
      auth_keys: [
        {
          app_id: "app_1",
          requests_24h: 8,
          errors_4xx_24h: 1,
          errors_5xx_24h: 0,
        },
        {
          app_id: "app_2",
          requests_24h: 12,
          errors_4xx_24h: 2,
          errors_5xx_24h: 3,
        },
      ],
    };

    expect(buildTelemetryCards(partial)).toMatchObject({
      requestWindowAvailable: false,
      errorWindowAvailable: false,
    });
    expect(buildTelemetryCards(complete)).toMatchObject({
      requestWindowAvailable: true,
      requests24h: 20,
      errorWindowAvailable: true,
      failedRequests24h: 6,
    });
  });
});

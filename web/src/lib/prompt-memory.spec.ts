import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { accountApi } from "./account-api";
import { ApiError, resetSessionCache } from "./api";
import { householdPromptMemoryApi } from "./prompt-memory";

// A save whose older-prompt cleanup is still pending answers 202 with an
// error-shaped body. The screens show that message and refetch, exactly as
// they did when the gateway answered 503, so neither API may resolve with it.

const PENDING = {
  error: {
    code: "prompt_retention_pending",
    message:
      "The setting was saved. Older prompt cleanup is pending and will be retried.",
  },
};
const SAVED_MEMBER = {
  prompt_memory: { remember: true, retention: "3_months" },
};
const SAVED_HOUSEHOLD = {
  prompt_memory: { default_remember: true, never_keep: false },
  revision: "r2",
};

function stubGateway(put: { status: number; body: unknown }) {
  const fetchMock = vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === "PUT")
      return Response.json(put.body, { status: put.status });
    return Response.json({ csrf_token: "csrf", actor: { csrf_token: "csrf" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => resetSessionCache());
afterEach(() => vi.unstubAllGlobals());

describe.each([
  [
    "member",
    () =>
      accountApi.savePromptMemory({ remember: true, retention: "3_months" }),
    SAVED_MEMBER,
  ],
  [
    "household",
    () =>
      householdPromptMemoryApi.put(
        {
          default_remember: true,
          never_keep: false,
          retention_cap: "3_months",
        },
        "r1",
      ),
    SAVED_HOUSEHOLD,
  ],
] as const)("%s prompt-memory save", (_scope, save, saved) => {
  test("a 202 cleanup-pending answer rejects with its message, as the 503 did", async () => {
    stubGateway({ status: 202, body: PENDING });
    const failure = await save().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({
      code: "prompt_retention_pending",
      status: 202,
      message: PENDING.error.message,
    });
  });

  test("a clean 200 save still resolves with the saved settings", async () => {
    stubGateway({ status: 200, body: saved });
    await expect(save()).resolves.toEqual(saved);
  });

  test("a 503 unsaved failure still rejects with its message", async () => {
    stubGateway({
      status: 503,
      body: {
        error: {
          code: "prompt_erasure_unavailable",
          message: "The setting wasn’t saved. Try again.",
        },
      },
    });
    await expect(save()).rejects.toMatchObject({
      status: 503,
      message: "The setting wasn’t saved. Try again.",
    });
  });
});

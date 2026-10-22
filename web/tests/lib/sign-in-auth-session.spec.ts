import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authFetch, type SignInOptions } from "@/app/sign-in/auth-api";
import { navigate } from "@/lib/navigate";
import * as sessionEnd from "@/lib/session-end";

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

// The options answer can name the person who is signed in, with their CSRF token.
const options: SignInOptions = {
  built_in: true,
  oidc: false,
  oidc_label: "",
  oidc_url: "",
  instance_name: "Azizam",
  owner_name: "Owner",
  signed_in_oidc: { email: "previous@example.com", name: "Previous Person", csrf_token: "csrf-previous" },
};

beforeEach(() => {
  vi.spyOn(navigate, "replace").mockImplementation(() => {});
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/logout") return json({ next: "/sign-in/" });
      if (String(input) === "/me/api/session") return json({ authenticated: true, csrf_token: "csrf" });
      return json(options);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  sessionEnd.resetSessionStateForTests();
});

// An answer to a request that is still in flight when the session ends may name the person who was signed in.
// The page must refuse it, as it refuses every session-bound answer.
it("a sign-in answer that arrives after the session ended is refused, not returned", async () => {
  let answer!: (response: Response) => void;
  const held = new Promise<Response>((resolve) => {
    answer = resolve;
  });
  vi.mocked(fetch).mockImplementationOnce(() => held);
  const pending = authFetch<SignInOptions>("/auth/api/options");
  await sessionEnd.endSession("sign-out");
  answer(json(options));
  await expect(pending).rejects.toMatchObject({ code: "session_ended" });
});

// Sign-in requests go through the same guard as Home's: once the session has ended, none is sent.
it("a sign-in request made after the session ended is refused and never sent", async () => {
  await sessionEnd.endSession("sign-out");
  vi.mocked(fetch).mockClear();
  await expect(authFetch<SignInOptions>("/auth/api/options")).rejects.toMatchObject({ code: "session_ended" });
  expect(fetch).not.toHaveBeenCalled();
});

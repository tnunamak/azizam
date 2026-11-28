import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { resetSessionCache } from "./api";
import {
  cancelSignInTest,
  discoverProvider,
  dismissNotice,
  linkOwner,
  previewSignIn,
  saveSignIn,
  setOwnerPassword,
  startSignInTest,
  type SaveRequest,
  type SignInProposal,
} from "./signin-api";

// The page's wrappers must put a JSON object on the wire: adminFetch encodes the body itself,
// so a body that is already a string reaches the server as one string and fails validation.
type Sent = { path: string; method: string; body: unknown; headers: Headers };
let sent: Sent[];

beforeEach(() => {
  sent = [];
  resetSessionCache();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/admin/api/session"))
        return new Response(JSON.stringify({ actor: { csrf_token: "token" } }));
      sent.push({
        path,
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: new Headers(init?.headers),
      });
      return new Response(JSON.stringify({}));
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  resetSessionCache();
});

const proposal: SignInProposal = {
  methods: { password: true, provider: true },
  provider: {
    name: "",
    address: "https://id.example.test/realms/home",
    client_id: "azizam",
    client_secret: "",
    scopes: "openid email profile",
    groups_claim: "groups",
    authorization_endpoint: "",
    token_endpoint: "",
    jwks_uri: "",
    clock_skew_seconds: 60,
  },
  rules: { fallback: "closed", rules: [] },
};

it.each([
  ["discover", () => discoverProvider("https://x.example"), "/admin/api/signin/discover", { address: "https://x.example" }],
  ["preview", () => previewSignIn(proposal, "test_1"), "/admin/api/signin/preview", { proposal, test_id: "test_1" }],
  ["start a test", () => startSignInTest(proposal.provider), "/admin/api/signin/test", { provider: proposal.provider }],
  ["link the owner", () => linkOwner("test_1"), "/admin/api/signin/link-owner", { test_id: "test_1" }],
  ["set a password", () => setOwnerPassword("a long enough password"), "/admin/api/signin/owner-password", { password: "a long enough password" }],
])("%s sends a JSON object, not a string", async (_name, call, path, body) => {
  await call();
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatchObject({ path, method: "POST", body });
  expect(typeof sent[0].body).toBe("object");
  expect(sent[0].headers.get("content-type")).toBe("application/json");
});

it("save sends the request as an object with the revision in If-Match", async () => {
  const request: SaveRequest = { proposal, test_id: null, keep_admin_keys: [], save_for_later: false };
  await saveSignIn("3-1-2", request);
  expect(sent[0]).toMatchObject({ path: "/admin/api/signin", method: "PUT", body: request });
  expect(sent[0].headers.get("if-match")).toBe("3-1-2");
});

it("cancel and dismiss use the right verbs without a body", async () => {
  await cancelSignInTest("test_1");
  await dismissNotice();
  expect(sent.map((s) => [s.method, s.path, s.body])).toEqual([
    ["DELETE", "/admin/api/signin/test/test_1", undefined],
    ["POST", "/admin/api/signin/notice/dismiss", undefined],
  ]);
});

// Shared fixtures and a renderer for the Sign-in specs. Each spec file mocks
// "@/lib/signin-api" itself (vi.mock must sit in the spec, and a factory cannot
// import this file); everything else lives here.
import { configure, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import * as api from "@/lib/signin-api";
import type {
  Discovery,
  Preview,
  SignInRule,
  SignInSnapshot,
  TestStatus,
} from "@/lib/signin-api";
import { SignInSection } from "./sign-in-section";
import { PREVIEW_DEBOUNCE_MS } from "./use-preview";

export const mocked = {
  get: vi.mocked(api.getSignIn),
  preview: vi.mocked(api.previewSignIn),
  discover: vi.mocked(api.discoverProvider),
  start: vi.mocked(api.startSignInTest),
  poll: vi.mocked(api.getSignInTest),
  cancel: vi.mocked(api.cancelSignInTest),
  link: vi.mocked(api.linkOwner),
  save: vi.mocked(api.saveSignIn),
  dismiss: vi.mocked(api.dismissNotice),
  password: vi.mocked(api.setOwnerPassword),
};

// The preview waits 300 ms and discovery 600 ms before they ask; the machine is shared.
configure({ asyncUtilTimeout: 6000 });

// Each journey types a form, runs a test and waits for the preview: about 1 s
// alone, past the 5 s default when the gate runs beside the web build.
vi.setConfig({ testTimeout: 20_000 });

/**
 * `vi.waitFor` with the same 6 s limit. Its own default is 1 s, and the preview
 * alone waits 300 ms after a test passes, so a loaded machine ran out of time
 * before the page had done anything wrong.
 */
export const until = <T,>(check: () => T | Promise<T>) =>
  vi.waitFor(check, { timeout: 6000, interval: 50 });

// jsdom lacks these; Radix Select and the value picker need them.
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};
Element.prototype.setPointerCapture ??= () => {};
Element.prototype.scrollIntoView ??= () => {};

export const PLANS = [
  { id: "plan_everything", name: "Everything" },
  { id: "plan_local", name: "Local and paid" },
  { id: "plan_assistant", name: "Assistant" },
];

export const trusted: SignInRule = {
  id: "rule-trusted",
  field: "group",
  values: ["/Trusted"],
  admit: true,
  admin: true,
  plan: null,
};

const seen: SignInSnapshot["seen"] = {
  group: [
    { value: "/Family", count: 1 },
    { value: "/Trusted", count: 2 },
  ],
  role: [],
  email: [
    { value: "simon.agent@vivid.fish", count: 1, note: "Simon Agent" },
    { value: "tim@opendatalabs.xyz", count: 1, note: "Tim ODL" },
  ],
  domain: [
    { value: "opendatalabs.xyz", count: 1 },
    { value: "vivid.fish", count: 1 },
  ],
};

const baseProvider = {
  name: "",
  address: "",
  client_id: "",
  scopes: "",
  groups_claim: "",
  authorization_endpoint: "",
  token_endpoint: "",
  jwks_uri: "",
  clock_skew_seconds: 60,
  client_secret_saved: false,
  host: "",
  effective_name: "",
  redirect_uri: "https://ai.example.test/admin/oidc/callback",
  tested_at: null,
  groups_missing: false,
  fresh_login_supported: false,
};

const OWNER = {
  name: "Tim Nunamaker",
  email: "tim.nunamaker@gmail.com",
  has_password: true,
};

/** State a: the provider is on, tested, with one rule. */
export function liveSnapshot(over: Partial<SignInSnapshot> = {}): SignInSnapshot {
  return {
    revision: "rev-1",
    viewer: { role: "owner", can_edit: true },
    methods: { password: true, provider: true },
    memory: "remember",
    provider: {
      ...baseProvider,
      name: "Keycloak",
      address: "https://id.vivid.fish/realms/home",
      client_id: "azizam",
      scopes: "openid email profile",
      groups_claim: "groups",
      client_secret_saved: true,
      host: "id.vivid.fish",
      effective_name: "Keycloak",
      tested_at: Math.floor(Date.now() / 1000) - 3 * 3600,
    },
    rules: { fallback: "closed", rules: [{ ...trusted }] },
    rule_matches: {
      "rule-trusted": { count: 2, names: ["Simon Agent", "Tim Nunamaker"] },
    },
    seen,
    plans: PLANS,
    owner: { ...OWNER },
    recovery_command: "./venv/bin/python proxy.py generate-admin-key owner-cli",
    notice: null,
    ...over,
  };
}

/** State e: nothing connected, no rules. */
export function firstRunSnapshot(over: Partial<SignInSnapshot> = {}): SignInSnapshot {
  return {
    ...liveSnapshot(),
    methods: { password: true, provider: false },
    provider: { ...baseProvider },
    rules: { fallback: "closed", rules: [] },
    rule_matches: {},
    seen: { group: [], role: [], email: [], domain: [] },
    ...over,
  };
}

export function emptyPreview(over: Partial<Preview> = {}): Preview {
  return {
    people: [],
    unaffected: [],
    issuer_change: null,
    settings: [],
    blockers: [],
    rule_matches: {},
    test_verdict: null,
    ...over,
  };
}

export function discovery(host = "id.example.com"): Discovery {
  return {
    ok: true,
    host,
    authorization_endpoint: `https://${host}/auth`,
    token_endpoint: `https://${host}/token`,
    jwks_uri: `https://${host}/certs`,
  };
}

export function passedStatus(
  over: Partial<Extract<TestStatus, { status: "passed" }>> = {},
): TestStatus {
  return {
    status: "passed",
    tested_at: Math.floor(Date.now() / 1000),
    fresh_login_supported: false,
    identity: {
      name: "Tim Nunamaker",
      email: "tim.nunamaker@gmail.com",
      email_verified: true,
      groups: ["/Trusted"],
      roles: [],
      claims: ["sub", "email", "name", "groups"],
      groups_claim: "groups",
    },
    warnings: [],
    can_link: false,
    details: 'Claims received: sub, email, name, groups\nGroups arrived under the name "groups".',
    ...over,
  };
}

/** The pop-up `window.open` hands back. */
export function fakePopup(over: Partial<{ closed: boolean }> = {}) {
  return {
    closed: false,
    close: vi.fn(),
    location: { href: "" },
    ...over,
  };
}

export interface Mount {
  user: ReturnType<typeof userEvent.setup>;
  client: QueryClient;
}

/**
 * Mounts the section against a snapshot. `preview` may be a Preview or a
 * function of the proposal so a spec can answer like the server would.
 */
export async function mountSignIn(
  snapshot: SignInSnapshot,
  options: {
    preview?: Preview | ((proposal: api.SignInProposal, testId: string | null) => Preview);
    discovery?: Discovery | null;
    userOptions?: Parameters<typeof userEvent.setup>[0];
  } = {},
): Promise<Mount> {
  mocked.get.mockResolvedValue(snapshot);
  const answer = options.preview ?? emptyPreview();
  mocked.preview.mockImplementation(async (proposal, testId) =>
    typeof answer === "function" ? answer(proposal, testId) : answer,
  );
  const found = options.discovery === undefined ? discovery() : options.discovery;
  mocked.discover.mockImplementation(async () =>
    found ?? { ...discovery(), ok: false },
  );
  mocked.cancel.mockResolvedValue({ cancelled: true });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const user = userEvent.setup(
    options.userOptions ??
      (vi.isFakeTimers() ? { advanceTimers: vi.advanceTimersByTime } : {}),
  );
  render(
    <QueryClientProvider client={client}>
      <SignInSection />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "Ways to sign in" });
  if (snapshot.viewer.can_edit) await settle();
  return { user, client };
}

/** Wait until the footer is there and the last form change has been answered by the preview. */
export async function settle() {
  await waitFor(() => {
    const caption = document.querySelector('[data-slot="sign-in-caption"]');
    if (!caption) throw new Error("footer not there yet");
    if (caption.textContent === "Checking what would change…")
      throw new Error("preview pending");
  });
}

export const lastProposal = () => {
  const calls = mocked.preview.mock.calls;
  return calls[calls.length - 1];
};

export const text = () => document.body.textContent ?? "";

/** Make the page believe it is on a phone (or not). Call `setPhone(false)` after. */
export function setPhone(phone: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: phone && query === "(max-width: 639px)",
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
      onchange: null,
    }),
  });
}
export function clearPhone() {
  Reflect.deleteProperty(window, "matchMedia");
}

type User = Mount["user"];

export function stubPopup(over: Parameters<typeof fakePopup>[0] = {}) {
  const popup = fakePopup(over);
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
  return popup;
}

/** Type the connection form the way an owner would. */
export async function fillConnection(
  user: User,
  v: { address?: string; clientId?: string; secret?: string; name?: string },
) {
  const put = async (label: string, value: string) => {
    const input = screen.getByLabelText(label);
    await user.click(input);
    await user.paste(value);
  };
  if (v.address) await put("Provider address", v.address);
  if (v.clientId) await put("Client ID", v.clientId);
  if (v.secret) await put("Client secret", v.secret);
  if (v.name) await put("Name", v.name);
  if (v.address) await screen.findByText(/Found a sign-in provider at/);
}

/** Click Test sign-in and let the first poll answer with `status`. */
export async function runTest(
  user: User,
  status: TestStatus = passedStatus(),
  testId = "t1",
) {
  mocked.start.mockResolvedValue({
    test_id: testId,
    start_url: "https://id.example.com/auth?state=x",
  });
  mocked.poll.mockResolvedValue(status);
  const popup = stubPopup();
  await user.click(screen.getByRole("button", { name: "Test sign-in" }));
  await vi.advanceTimersByTimeAsync(1600);
  // The passed test changes what the preview asks; step past its wait.
  await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 50);
  return popup;
}

export const ownerUnlinked = (email = "tim.nunamaker@gmail.com") =>
  ({ kind: "owner_unlinked", email, old_link_stale: false }) as const;

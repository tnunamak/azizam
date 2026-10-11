import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SignInPage from "./page";
import { continueTo } from "./auth-api";

vi.mock("./auth-api", async (original) => ({
  ...(await original<typeof import("./auth-api")>()),
  continueTo: vi.fn(),
}));
const defaults = {
  built_in: true,
  oidc: false,
  oidc_label: "",
  oidc_url: "/login?return_to=%2Fui%2Fusage%2F",
  instance_name: "Azizam",
};
let options: typeof defaults;
let ownerExists: boolean;
let client: QueryClient;
const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));
beforeEach(() => {
  options = { ...defaults };
  ownerExists = true;
  window.history.replaceState(null, "", "/ui/sign-in/");
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) =>
      String(input).startsWith("/auth/api/options")
        ? json(options)
        : json({ owner_exists: ownerExists }),
    ),
  );
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.unstubAllGlobals();
});
function mount() {
  return render(
    <QueryClientProvider client={client}>
      <SignInPage />
    </QueryClientProvider>,
  );
}
async function submit() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Email"), "member@example.test");
  await user.type(screen.getByLabelText("Password"), "A long test password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}
it("shows one heading and a quiet loading state before options arrive", () => {
  vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));
  mount();
  expect(screen.getByRole("status")).toHaveTextContent("Getting sign-in ready");
  expect(screen.getAllByRole("heading")).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
});
it.each([
  "Household accounts",
  "Google",
  "River Library",
  "single sign-on",
  "your identity provider",
  "",
])(
  "makes the configured provider primary with a plain fallback: %s",
  async (label) => {
    options.oidc = true;
    options.oidc_label = label;
    mount();
    const provider = await screen.findByRole("link", {
      name:
        label && label !== "your identity provider"
          ? `Continue with ${label}`
          : "Continue with single sign-on",
    });
    expect(provider).toHaveAttribute("data-variant", "default");
    expect(provider).toHaveAttribute("href", options.oidc_url);
    expect(screen.getByRole("button", { name: "Sign in" })).toHaveAttribute(
      "data-variant",
      "outline",
    );
    expect(
      provider.compareDocumentPosition(screen.getByLabelText("Email")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  },
);
it("offers only single sign-on when passwords are disabled", async () => {
  options.built_in = false;
  options.oidc = true;
  mount();
  await screen.findByRole("link", { name: "Continue with single sign-on" });
  expect(screen.queryByLabelText("Email")).toBeNull();
});
it("sends the original return target to the server and follows its validated next destination", async () => {
  window.history.replaceState(
    null,
    "",
    "/ui/sign-in/?return_to=%2Fui%2Fusage%2F%3Freceipt%3Done",
  );
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login"
      ? json({ next: "/ui/usage/?receipt=one" })
      : original(input, init),
  );
  mount();
  await submit();
  expect(fetch).toHaveBeenCalledWith(
    "/auth/api/login",
    expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      body: JSON.stringify({
        email: "member@example.test",
        password: "A long test password",
        return_to: "/ui/usage/?receipt=one",
      }),
    }),
  );
  await waitFor(() =>
    expect(continueTo).toHaveBeenCalledWith("/ui/usage/?receipt=one"),
  );
});
it.each([
  [
    "invalid_login",
    401,
    "Email or password is incorrect.",
    "Check both and try again.",
  ],
  [
    "account_disabled",
    403,
    "This account is disabled.",
    "Ask the person who invited you for help.",
  ],
  [
    "login_rate_limited",
    429,
    "Too many attempts. Try again in 15 minutes.",
    "Try again in 15 minutes.",
  ],
  [
    "sessions_unavailable",
    503,
    "Browser sessions are unavailable.",
    "Couldn’t sign you in.",
  ],
])(
  "explains %s and focuses the error without losing the fields",
  async (code, status, message, expected) => {
    const original = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((input, init) =>
      String(input) === "/auth/api/login"
        ? json({ error: { code, message } }, Number(status))
        : original(input, init),
    );
    mount();
    await submit();
    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent(expected);
    expect(error).toHaveFocus();
    expect(screen.getByLabelText("Email")).toHaveValue("member@example.test");
    expect(screen.getByLabelText("Password")).toHaveValue(
      "A long test password",
    );
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  },
);
it("names the pending action and prevents duplicate submissions", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login"
      ? new Promise(() => {})
      : original(input, init),
  );
  mount();
  await submit();
  expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();
  expect(screen.getByLabelText("Email")).toBeDisabled();
});
it("explains an expired password reset without offering unrelated sign-in methods", async () => {
  window.history.replaceState(null, "", "/ui/sign-in/#reset=expired-test");
  options.oidc = true;
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/password-reset"
      ? json({ error: { code: "link_unavailable", message: "Expired" } }, 404)
      : original(input, init),
  );
  mount();
  await userEvent.type(
    await screen.findByLabelText("New password"),
    "A long new password",
  );
  await userEvent.click(screen.getByRole("button", { name: "Save password" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Ask for a new link.",
  );
  expect(screen.queryByRole("link")).toBeNull();
  expect(fetch).toHaveBeenCalledWith(
    "/auth/api/password-reset",
    expect.objectContaining({
      body: JSON.stringify({
        token: "expired-test",
        password: "A long new password",
      }),
    }),
  );
});
it("offers setup when there is no owner, and no password form", async () => {
  ownerExists = false;
  mount();
  expect(
    await screen.findByRole("link", { name: "Set up your gateway" }),
  ).toHaveAttribute("href", "/ui/setup/");
  expect(screen.queryByLabelText("Email")).toBeNull();
});
it("explains when the host has disabled all sign-in methods", async () => {
  options.built_in = false;
  mount();
  expect(await screen.findByText("Sign-in is not configured")).toBeVisible();
  expect(screen.queryByLabelText("Email")).toBeNull();
});
it("offers recovery after an options failure", async () => {
  vi.mocked(fetch).mockRejectedValueOnce(new Error("Offline"));
  mount();
  await userEvent.click(
    await screen.findByRole("button", { name: "Try again" }),
  );
  expect(await screen.findByLabelText("Email")).toBeVisible();
});
it("explains provider failure without rendering untrusted error descriptions", async () => {
  window.history.replaceState(
    null,
    "",
    "/ui/sign-in/?error=provider&error_description=untrusted",
  );
  options.oidc = true;
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Single sign-on didn’t finish.",
  );
  expect(screen.queryByText("untrusted")).toBeNull();
});

it("renders provider text through React escaping", async () => {
  options.oidc = true;
  options.oidc_label = '<img src=x onerror="alert(1)">';
  mount();
  const provider = await screen.findByRole("link", {
    name: `Continue with ${options.oidc_label}`,
  });
  expect(provider.querySelector("img")).toBeNull();
  expect(provider).toHaveTextContent(options.oidc_label);
});

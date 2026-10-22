import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
  oidc_url: "/login?return_to=%2Fusage%2F",
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
  window.history.replaceState(null, "", "/sign-in/");
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
    // The name box is on by default, so the single sign-on start says so: remember=1 (Addendum 2).
    expect(provider).toHaveAttribute("href", `${options.oidc_url}&remember=1`);
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
it("keeps the owner's password one click away while passwords are off for everyone else", async () => {
  options.built_in = false;
  options.oidc = true;
  Object.assign(options, { owner_password: true });
  const user = userEvent.setup();
  mount();
  await screen.findByRole("link", { name: "Continue with single sign-on" });
  expect(screen.queryByLabelText("Email")).toBeNull();

  await user.click(screen.getByRole("button", { name: "Owner? Sign in with your password" }));

  expect(await screen.findByLabelText("Email")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Owner? Sign in with your password" })).toBeNull();
  expect(screen.getByText("or use your email")).toBeVisible();
});
it("signs the owner in with the password form while passwords are off", async () => {
  options.built_in = false;
  options.oidc = true;
  Object.assign(options, { owner_password: true });
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login"
      ? json({ next: "/" })
      : original(input, init),
  );
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Owner? Sign in with your password" }));
  await submit();
  expect(fetch).toHaveBeenCalledWith("/auth/api/login", expect.objectContaining({ method: "POST" }));
  await waitFor(() => expect(continueTo).toHaveBeenCalledWith("/"));
});
it("offers no owner password link when the server does not offer one", async () => {
  options.built_in = false;
  options.oidc = true;
  Object.assign(options, { owner_password: false });
  mount();
  await screen.findByRole("link", { name: "Continue with single sign-on" });
  expect(screen.queryByRole("button", { name: "Owner? Sign in with your password" })).toBeNull();
});
it("sends the original return target to the server and follows its validated next destination", async () => {
  window.history.replaceState(
    null,
    "",
    "/sign-in/?return_to=%2Fusage%2F%3Freceipt%3Done",
  );
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login"
      ? json({ next: "/usage/?receipt=one" })
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
        return_to: "/usage/?receipt=one",
        remember: true,
      }),
    }),
  );
  await waitFor(() =>
    expect(continueTo).toHaveBeenCalledWith("/usage/?receipt=one"),
  );
});
it("requires a password before sign-in and sends a one-character password", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login" ? json({ next: "/" }) : original(input, init),
  );
  mount();
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Email"), "member@example.test");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(fetch).not.toHaveBeenCalledWith("/auth/api/login", expect.anything());
  expect(screen.getByLabelText("Password")).toBeRequired();
  await user.type(screen.getByLabelText("Password"), "x");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/auth/api/login", expect.objectContaining({ body: JSON.stringify({ email: "member@example.test", password: "x", return_to: "/", remember: true }) })));
});
it("requires a password before reset and sends a one-character password", async () => {
  window.history.replaceState(null, "", "/sign-in/#reset=reset-test");
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/password-reset" ? json({ next: "/" }) : original(input, init),
  );
  mount();
  const user = userEvent.setup();
  const field = await screen.findByLabelText("New password");
  await user.click(screen.getByRole("button", { name: "Save password" }));
  expect(fetch).not.toHaveBeenCalledWith("/auth/api/password-reset", expect.anything());
  expect(field).toBeRequired();
  await user.type(field, "x");
  await user.click(screen.getByRole("button", { name: "Save password" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/auth/api/password-reset", expect.objectContaining({ body: JSON.stringify({ token: "reset-test", password: "x" }) })));
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
  window.history.replaceState(null, "", "/sign-in/#reset=expired-test");
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
  ).toHaveAttribute("href", "/setup/");
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
    "/sign-in/?error=provider&error_description=untrusted",
  );
  options.oidc = true;
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Single sign-on didn’t finish.",
  );
  expect(screen.queryByText("untrusted")).toBeNull();
});

it("says a provider that reports no sign-in time cannot confirm a risky action, and how to confirm instead", async () => {
  window.history.replaceState(
    null,
    "",
    "/sign-in/?error=no_sign_in_time&return_to=%2Fsettings%2F",
  );
  options.oidc = true;
  mount();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("doesn’t tell Azizam when you signed in");
  expect(alert).toHaveTextContent("Confirm with your Azizam password");
  expect(alert).not.toHaveTextContent("Single sign-on didn’t finish");
});

it("says a provider did not ask for a password just now, and links to the sign-out when the provider has one", async () => {
  window.history.replaceState(
    null,
    "",
    "/sign-in/?error=not_fresh&return_to=%2Fsettings%2F&provider_sign_out=1",
  );
  options.oidc = true;
  options.oidc_label = "Keycloak";
  mount();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(
    "Azizam couldn’t confirm that Keycloak just asked for your password.",
  );
  expect(alert).toHaveTextContent("Sign out of Keycloak, then try again.");
  expect(
    within(alert).getByRole("link", { name: "Sign out of Keycloak" }),
  ).toHaveAttribute("href", "/login/provider-sign-out");
});

it("gives the same message without a link when the provider publishes no sign-out address", async () => {
  window.history.replaceState(null, "", "/sign-in/?error=not_fresh");
  options.oidc = true;
  options.oidc_label = "Keycloak";
  mount();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Sign out of Keycloak, then try again.");
  expect(within(alert).queryByRole("link")).toBeNull();
});

it("names the provider generically when the label is the default one", async () => {
  window.history.replaceState(null, "", "/sign-in/?error=not_fresh");
  options.oidc = true;
  options.oidc_label = "your identity provider";
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Azizam couldn’t confirm that your sign-in provider just asked for your password. Sign out of your sign-in provider, then try again.",
  );
});

it("shows a refused first-owner sign-in on the setup page of a new gateway, where the setup link is", async () => {
  ownerExists = false;
  window.history.replaceState(
    null,
    "",
    "/sign-in/?error=not_fresh&return_to=%2Fsetup%2F&provider_sign_out=1",
  );
  options.oidc = true;
  options.oidc_label = "Keycloak";
  mount();
  expect(
    await screen.findByRole("heading", { name: "Set up Azizam" }),
  ).toBeVisible();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Azizam couldn’t confirm that Keycloak just asked for your password.",
  );
  expect(
    screen.getByRole("link", { name: "Set up your gateway" }),
  ).toHaveAttribute("href", "/setup/");
});

it("shows a refused invitation sign-in with the same message, and the retry goes back to the invitation", async () => {
  window.history.replaceState(
    null,
    "",
    "/sign-in/?error=not_fresh&return_to=%2Finvite%2F&provider_sign_out=1",
  );
  options.oidc = true;
  options.oidc_label = "Keycloak";
  options.oidc_url = "/login?return_to=%2Finvite%2F";
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Azizam couldn’t confirm that Keycloak just asked for your password.",
  );
  expect(
    screen.getByRole("link", { name: "Continue with Keycloak" }),
  ).toHaveAttribute("href", "/login?return_to=%2Finvite%2F&remember=1");
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

it("shows a labelled email and password form for a password sign-in, with no recovery disclosure", async () => {
  mount();
  expect(await screen.findByLabelText("Email")).toBeVisible();
  expect(screen.getByLabelText("Password")).toHaveAttribute(
    "autocomplete",
    "current-password",
  );
  expect(screen.getByRole("button", { name: "Sign in" })).toBeVisible();
  expect(screen.queryByText("Recovery and advanced setup")).toBeNull();
});
it("keeps one heading and no form when options fail to load", async () => {
  vi.mocked(fetch).mockRejectedValue(new Error("Offline"));
  mount();
  expect(
    await screen.findByRole("button", { name: "Try again" }),
  ).toBeVisible();
  expect(screen.getAllByRole("heading")).toHaveLength(1);
  expect(screen.queryByRole("form")).toBeNull();
  expect(screen.queryByLabelText("Email")).toBeNull();
  expect(screen.queryByText("Recovery and advanced setup")).toBeNull();
});

import {
  cleanup,
  render,
  screen,
  waitFor,
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
let options: typeof defaults & { fresh_sign_in?: boolean };
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
    // The sign-in primary is the Home's resting Make (`--make-rest`, an ink pill): the same fill as the Home's Make button.
    expect(provider).toHaveAttribute("data-variant", "make");
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
  "explains %s and moves focus to the password without losing the fields",
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
    expect(screen.getByLabelText("Password")).toHaveFocus();
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

it("a refused sign-in says the provider did not ask for a password, and offers its sign-out (S8)", async () => {
  options.fresh_sign_in = true;
  window.history.replaceState(
    null,
    "",
    "/sign-in/?error=not_fresh&return_to=%2Fsettings%2F&provider_sign_out=1",
  );
  options.oidc = true;
  options.oidc_label = "Keycloak";
  mount();
  expect(
    await screen.findByRole("heading", { level: 1, name: "Sign out of Keycloak first" }),
  ).toBeVisible();
  expect(
    screen.getByText(
      "Azizam couldn’t confirm that Keycloak just asked for a password, so it stopped. Someone may still be signed in to Keycloak in this browser.",
    ),
  ).toBeVisible();
  expect(
    screen.getByText(
      "Signing out of Keycloak also signs out of other apps that use it in this browser.",
    ),
  ).toBeVisible();
  expect(screen.getByRole("link", { name: "Sign out of Keycloak" })).toHaveAttribute(
    "href",
    "/login/provider-sign-out",
  );
  expect(screen.getByRole("button", { name: "Back" })).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("without a provider sign-out address, says who can sign out and offers Sign in again (S8b)", async () => {
  options.fresh_sign_in = true;
  window.history.replaceState(null, "", "/sign-in/?error=not_fresh&return_to=%2Fusage%2F");
  options.oidc = true;
  options.oidc_label = "Keycloak";
  mount();
  expect(
    await screen.findByRole("heading", { level: 1, name: "Sign out of Keycloak first" }),
  ).toBeVisible();
  expect(
    screen.getByText(
      "Azizam couldn’t confirm that Keycloak just asked for a password, so it stopped. Ask the person who used Keycloak last to sign out of it. Or open Keycloak in a new tab, choose Sign out, and open nothing else there.",
    ),
  ).toBeVisible();
  expect(screen.getByText("If neither works, ask the person who runs Azizam.")).toBeVisible();
  expect(screen.queryByRole("link", { name: "Sign out of Keycloak" })).toBeNull();
  expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute(
    "href",
    "/login?return_to=%2Fusage%2F&remember=1",
  );
});

it("names the provider generically when the label is the default one (S8b)", async () => {
  options.fresh_sign_in = true;
  window.history.replaceState(null, "", "/sign-in/?error=not_fresh");
  options.oidc = true;
  options.oidc_label = "your identity provider";
  mount();
  expect(
    await screen.findByRole("heading", { level: 1, name: "Sign out of your sign-in provider first" }),
  ).toBeVisible();
  expect(
    screen.getByText(
      "Azizam couldn’t confirm that your sign-in provider just asked for a password, so it stopped. Ask the person who used your sign-in provider last to sign out of it. Or open your sign-in provider in a new tab, choose Sign out, and open nothing else there.",
    ),
  ).toBeVisible();
});

it("a refused first-owner sign-in shows the same screen, and Back returns to the setup page", async () => {
  options.fresh_sign_in = true;
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
    await screen.findByRole("heading", { level: 1, name: "Sign out of Keycloak first" }),
  ).toBeVisible();
  await userEvent.setup().click(screen.getByRole("button", { name: "Back" }));
  expect(
    await screen.findByRole("heading", { level: 1, name: "Set up Azizam" }),
  ).toBeVisible();
  expect(
    screen.getByRole("link", { name: "Set up your gateway" }),
  ).toHaveAttribute("href", "/setup/");
});

it("a refused invitation sign-in shows the same screen, and Back goes back to the invitation", async () => {
  options.fresh_sign_in = true;
  window.history.replaceState(
    null,
    "",
    "/sign-in/?error=not_fresh&return_to=%2Finvite%2F&provider_sign_out=1",
  );
  options.oidc = true;
  options.oidc_label = "Keycloak";
  options.oidc_url = "/login?return_to=%2Finvite%2F";
  mount();
  expect(
    await screen.findByRole("heading", { level: 1, name: "Sign out of Keycloak first" }),
  ).toBeVisible();
  await userEvent.setup().click(screen.getByRole("button", { name: "Back" }));
  expect(
    await screen.findByRole("link", { name: "Continue with Keycloak" }),
  ).toHaveAttribute("href", "/login?return_to=%2Finvite%2F&remember=1");
  expect(screen.queryByRole("heading", { name: /Sign out of/ })).toBeNull();
});

it("Back leaves the sign-out screen, and a reload does not bring it back", async () => {
  options.fresh_sign_in = true;
  window.history.replaceState(null, "", "/sign-in/?error=not_fresh&provider_sign_out=1");
  options.oidc = true;
  options.oidc_label = "Keycloak";
  mount();
  await userEvent.setup().click(await screen.findByRole("button", { name: "Back" }));
  expect(
    await screen.findByRole("heading", { level: 1, name: "Sign in to Azizam" }),
  ).toBeVisible();
  expect(window.location.search).not.toContain("error=");
  expect(window.location.search).not.toContain("provider_sign_out");
});

it("a refused sign-in on a gateway that does not report fresh sign-in keeps the plain page, with no sign-out link", async () => {
  // Before the gateway reports fresh sign-in (A2), the refused-sign-in screen stays off and the page reads as usual.
  window.history.replaceState(
    null,
    "",
    "/sign-in/?error=not_fresh&return_to=%2Fsettings%2F&provider_sign_out=1",
  );
  options.oidc = true;
  options.oidc_label = "Keycloak";
  mount();
  expect(
    await screen.findByRole("heading", { level: 1, name: "Sign in to Azizam" }),
  ).toBeVisible();
  expect(screen.queryByText("Sign out of Keycloak first")).toBeNull();
  expect(screen.queryByRole("link", { name: "Sign out of Keycloak" })).toBeNull();
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

it("names the instance only: no shared-by line, and no person's name, even if the gateway sent one", async () => {
  // The sign-in page is public. The options answer carries no owner name, and the page would not print one anyway.
  Object.assign(options, { owner_name: "Sam Okafor" });
  mount();
  expect(await screen.findByRole("heading", { name: "Sign in to Azizam" })).toBeVisible();
  expect(screen.queryByText(/Shared by/)).toBeNull();
  expect(screen.queryByText(/Sam Okafor/)).toBeNull();
});

it("gives the reset line one copy for everyone, with no name in it", async () => {
  Object.assign(options, { owner_name: "Sam Okafor" });
  mount();
  const reset = await screen.findByText("Forgot your password? Ask the person who runs this gateway for a reset link.");
  expect(reset).toBeVisible();
  expect(reset).toHaveClass("text-pretty");
  expect(screen.queryByText(/Sam Okafor/)).toBeNull();
});

it("puts the name box under the single sign-on button, and its helper is its description", async () => {
  options.built_in = false;
  options.oidc = true;
  options.oidc_label = "Okafor ID";
  mount();
  const provider = await screen.findByRole("link", { name: "Continue with Okafor ID" });
  const box = screen.getByRole("checkbox", { name: "Remember me on this browser" });
  expect(provider.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(box).toHaveAccessibleDescription("Turn this off on a public computer.");
});

it("puts the name box after the form's Sign in when both ways in are on, so it covers both", async () => {
  options.built_in = true;
  options.oidc = true;
  options.oidc_label = "Okafor ID";
  mount();
  const provider = await screen.findByRole("link", { name: "Continue with Okafor ID" });
  const signIn = screen.getByRole("button", { name: "Sign in" });
  const box = screen.getByRole("checkbox", { name: "Remember me on this browser" });
  expect(provider.compareDocumentPosition(signIn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(signIn.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("moves focus to Email when the owner's password form opens in place", async () => {
  options.built_in = false;
  options.oidc = true;
  Object.assign(options, { owner_password: true });
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Owner? Sign in with your password" }));
  expect(await screen.findByLabelText("Email")).toHaveFocus();
});

it("marks both fields invalid when the password is refused, and names the refusal in each", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login"
      ? json({ error: { code: "invalid_login", message: "Wrong" } }, 401)
      : original(input, init),
  );
  mount();
  await submit();
  await screen.findByRole("alert");
  for (const field of [screen.getByLabelText("Email"), screen.getByLabelText("Password")]) {
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription("Email or password is incorrect. Check both and try again.");
  }
});

it("moves focus to the password field after a refusal, selects its text, and enables Sign in again", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login"
      ? json({ error: { code: "invalid_login", message: "Wrong" } }, 401)
      : original(input, init),
  );
  mount();
  await submit();
  await screen.findByRole("alert");
  const password = screen.getByLabelText("Password") as HTMLInputElement;
  await waitFor(() => expect(password).toHaveFocus());
  expect([password.selectionStart, password.selectionEnd]).toEqual([0, "A long test password".length]);
  expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
});

it("does not flag the fields for a refusal that is not about the password", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input) === "/auth/api/login"
      ? json({ error: { code: "account_disabled", message: "This account is disabled." } }, 403)
      : original(input, init),
  );
  mount();
  await submit();
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Email")).not.toHaveAttribute("aria-invalid");
  expect(screen.getByLabelText("Password")).not.toHaveAttribute("aria-invalid");
});

it("says the provider is opening, once, and holds the other controls while the page leaves", async () => {
  options.built_in = true;
  options.oidc = true;
  options.oidc_label = "Okafor ID";
  mount();
  const user = userEvent.setup();
  const provider = await screen.findByRole("link", { name: "Continue with Okafor ID" });
  // jsdom cannot leave the page: stop the link's navigation here, as the browser would start it.
  const stop = (event: MouseEvent) => event.preventDefault();
  window.addEventListener("click", stop);
  try {
    await user.click(provider);
  } finally {
    window.removeEventListener("click", stop);
  }
  expect(provider).toHaveTextContent("Opening Okafor ID…");
  expect(provider).toHaveAttribute("aria-disabled", "true");
  expect(screen.getByRole("status")).toHaveTextContent("Opening Okafor ID");
  expect(screen.getByLabelText("Email")).toBeDisabled();
  expect(screen.getByLabelText("Password")).toBeDisabled();
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
});

it("applies the name box to single sign-on as well as to the password", async () => {
  options.built_in = true;
  options.oidc = true;
  options.oidc_label = "Okafor ID";
  mount();
  const user = userEvent.setup();
  const provider = await screen.findByRole("link", { name: "Continue with Okafor ID" });
  expect(provider).toHaveAttribute("href", `${options.oidc_url}&remember=1`);
  await user.click(screen.getByRole("checkbox", { name: "Remember me on this browser" }));
  expect(provider).toHaveAttribute("href", `${options.oidc_url}&remember=0`);
  await user.type(screen.getByLabelText("Email"), "member@example.test");
  await user.type(screen.getByLabelText("Password"), "A long test password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => {
    const login = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/auth/api/login");
    expect(JSON.parse(String(login?.[1]?.body))).toMatchObject({ remember: false });
  });
});

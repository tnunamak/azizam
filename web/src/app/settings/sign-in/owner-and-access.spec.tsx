import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { ApiError } from "@/lib/api";

vi.mock("@/lib/toast", () => ({
  toast: { success: vi.fn(), info: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));
vi.mock("@/lib/signin-api", async (original) => {
  const real = await original<typeof import("@/lib/signin-api")>();
  return {
    ...real,
    getSignIn: vi.fn(),
    previewSignIn: vi.fn(),
    discoverProvider: vi.fn(),
    startSignInTest: vi.fn(),
    getSignInTest: vi.fn(),
    cancelSignInTest: vi.fn(),
    linkOwner: vi.fn(),
    saveSignIn: vi.fn(),
    dismissNotice: vi.fn(),
    setOwnerPassword: vi.fn(),
  };
});

import { SignInSection } from "./sign-in-section";
import {
  emptyPreview,
  liveSnapshot,
  mocked,
  mountSignIn,
} from "./test-support";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const caption = () =>
  document.querySelector('[data-slot="sign-in-caption"]')!.textContent;

describe("the two ways to sign in can never both be off", () => {
  it("locks the last one on and says why as a state, not an instruction (P1)", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: emptyPreview(),
    });
    const password = () => screen.getByRole("switch", { name: "Email and password" });
    const provider = () => screen.getByRole("switch", { name: "Keycloak" });
    expect(password()).not.toHaveAttribute("aria-disabled");
    expect(provider()).not.toHaveAttribute("aria-disabled");

    // Password off: the provider is the last way in and is locked.
    await user.click(password());
    expect(password()).not.toBeChecked();
    expect(provider()).toHaveAttribute("aria-disabled", "true");
    const reason = screen.getByText(
      "Stays on until email and password is on, so you can still sign in.",
    );
    expect(provider()).toHaveAttribute("aria-describedby", reason.id);
    await user.click(provider());
    expect(provider()).toBeChecked();
    // Back on: both are free again and the reason is gone.
    await user.click(password());
    expect(password()).toBeChecked();
    expect(provider()).not.toHaveAttribute("aria-disabled");
    expect(screen.queryByText(/^Stays on until/)).toBeNull();

    // Provider off: the password is the last way in and is locked.
    await user.click(provider());
    expect(provider()).not.toBeChecked();
    expect(password()).toHaveAttribute("aria-disabled", "true");
    const why = screen.getByText(
      "Stays on until Keycloak is on, so you can still sign in.",
    );
    expect(password()).toHaveAttribute("aria-describedby", why.id);
    await user.click(password());
    expect(password()).toBeChecked();
    // No instruction wording anywhere.
    expect(document.body.textContent).not.toMatch(/Turn on .* first, or only you/);
    // The footer never offers a save that leaves nobody able to sign in.
    await vi.waitFor(() =>
      expect(
        (mocked.preview.mock.calls.at(-1)?.[0] as { methods: object }).methods,
      ).toEqual({ password: true, provider: false }),
    );
  });

  it("lets a saved, tested provider be switched back on, and keeps an untested one locked", async () => {
    const off = { password: true, provider: false };
    const { user } = await mountSignIn(liveSnapshot({ methods: off }), {
      preview: emptyPreview(),
    });
    const provider = screen.getByRole("switch", { name: "Keycloak" });
    expect(screen.getByText("Off")).toBeInTheDocument();
    expect(provider).not.toHaveAttribute("aria-disabled");
    await user.click(provider);
    expect(provider).toBeChecked();
    cleanup();

    const untested = liveSnapshot({ methods: off });
    untested.provider.tested_at = null;
    await mountSignIn(untested, { preview: emptyPreview() });
    expect(screen.getByRole("switch", { name: "Keycloak" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByText("Off, not tested")).toBeInTheDocument();
  });
});

describe("an owner without a password (round 3, item 6)", () => {
  const noPassword = () =>
    liveSnapshot({
      owner: { name: "Tim Nunamaker", email: "t@example.com", has_password: false },
    });

  it("says so and offers to set one, instead of promising a password that is not there", async () => {
    await mountSignIn(noPassword(), { preview: emptyPreview() });
    expect(
      screen.getByText(
        "You have no password yet. Set one so you can still get in when Keycloak is down.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Your password always works/)).toBeNull();
    expect(screen.queryByText(/You, the owner, can always use your password/)).toBeNull();
    expect(screen.getByText("For people you invite by email.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set a password" })).toBeEnabled();
  });

  it("sets the password with a small inline form and shows the normal row afterwards", async () => {
    const { user } = await mountSignIn(noPassword(), { preview: emptyPreview() });
    await user.click(screen.getByRole("button", { name: "Set a password" }));
    const save = screen.getByRole("button", { name: "Save password" });
    expect(save).toBeDisabled();
    await user.click(screen.getByLabelText("New password"));
    await user.paste("a long enough passphrase");
    mocked.password.mockResolvedValue(liveSnapshot());
    await user.click(save);
    expect(mocked.password).toHaveBeenCalledWith("a long enough passphrase");
    expect(
      await screen.findByText("Your password always works, even when Keycloak is down"),
    ).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Password set.");
    expect(screen.queryByRole("button", { name: "Set a password" })).toBeNull();
    expect(
      screen.getByText("For people you invite by email. You, the owner, can always use your password."),
    ).toBeInTheDocument();
  });

  it("shows the server's reason when the password is refused", async () => {
    const { user } = await mountSignIn(noPassword(), { preview: emptyPreview() });
    await user.click(screen.getByRole("button", { name: "Set a password" }));
    await user.click(screen.getByLabelText("New password"));
    await user.paste("short");
    mocked.password.mockRejectedValue(new ApiError("password_weak", "That password is too short.", 400));
    await user.click(screen.getByRole("button", { name: "Save password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That password is too short.");
  });
});

describe("an administrator sees the page read-only", () => {
  const adminSnapshot = () =>
    liveSnapshot({ viewer: { role: "admin", can_edit: false } });

  it("disables every control and offers no way to save or test", async () => {
    const { user } = await mountSignIn(adminSnapshot());
    expect(screen.getByText("Only the owner can change sign-in.")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Email and password" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Keycloak" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Keep them out unless/ })).toBeDisabled();
    const rule = screen.getByRole("group", { name: "Rule 1" });
    expect(within(rule).getByRole("combobox", { name: "Rule applies to" })).toBeDisabled();
    expect(within(rule).getByRole("combobox", { name: "Choose groups" })).toBeDisabled();
    expect(within(rule).getByRole("checkbox", { name: "Make administrator" })).toBeDisabled();
    expect(within(rule).getByRole("combobox", { name: "Give plan" })).toBeDisabled();
    expect(within(rule).queryByRole("button", { name: "Remove rule" })).toBeNull();
    expect(within(rule).queryByRole("button", { name: "Remove /Trusted" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add a rule" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Test sign-in" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Save|Turn on|Discard/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Set a password" })).toBeNull();
    expect(document.querySelector('[data-slot="sign-in-caption"]')).toBeNull();
    // They can read the connection, but not change it.
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    expect(screen.getByLabelText("Provider address")).toBeDisabled();
    expect(screen.getByLabelText("Client ID")).toBeDisabled();
    expect(screen.getByLabelText("Client secret")).toBeDisabled();
    // Nothing is sent: no preview, no discovery.
    await vi.advanceTimersByTimeAsync(2000);
    expect(mocked.preview).not.toHaveBeenCalled();
    expect(mocked.discover).not.toHaveBeenCalled();
  });

  it("does not offer the one-time owner note to an administrator", async () => {
    await mountSignIn({ ...adminSnapshot(), notice: "removed_owner_rule" });
    expect(screen.queryByText(/Removed a rule that only named you/)).toBeNull();
  });
});

describe("the one-time note about a removed rule (P6)", () => {
  it("shows the note from the snapshot and dismisses it on the server", async () => {
    const { user } = await mountSignIn(
      liveSnapshot({ notice: "removed_owner_rule" }),
      { preview: emptyPreview() },
    );
    const note = screen.getByText("Removed a rule that only named you. You are always let in.");
    expect(note).toBeInTheDocument();
    // It is not unsaved work.
    expect(caption()).toBe("Nothing to save.");
    expect(screen.queryByRole("button", { name: "Discard changes" })).toBeNull();
    mocked.dismiss.mockResolvedValue(liveSnapshot({ notice: null }));
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(mocked.dismiss).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(note).not.toBeInTheDocument());
  });

  it("shows no note when the snapshot has none", async () => {
    await mountSignIn(liveSnapshot(), { preview: emptyPreview() });
    expect(screen.queryByText(/Removed a rule that only named you/)).toBeNull();
  });
});

describe("loading and failing to load", () => {
  const mount = () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <SignInSection />
      </QueryClientProvider>,
    );
  };

  it("announces the load as busy and keeps the heading", async () => {
    mocked.get.mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByRole("heading", { name: "Sign-in" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading sign-in" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("offers a retry when the load fails", async () => {
    mocked.get.mockRejectedValueOnce(new Error("boom"));
    mocked.preview.mockResolvedValue(emptyPreview());
    mocked.get.mockResolvedValue(liveSnapshot());
    mount();
    expect(await screen.findByText("Couldn’t load this")).toBeInTheDocument();
    expect(screen.getByText("Sign-in settings could not be loaded.")).toBeInTheDocument();
  });

  it("tells someone who may not read it that this is the owner's", async () => {
    mocked.get.mockRejectedValue(new ApiError("owner_required", "no", 403));
    mount();
    expect(await screen.findByText("Only the owner can change this.")).toBeInTheDocument();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, within } from "@testing-library/react";
import { toast } from "@/lib/toast";

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

import type { Preview, SignInProposal, TestVerdict } from "@/lib/signin-api";
import {
  emptyPreview,
  fillConnection,
  firstRunSnapshot,
  liveSnapshot,
  mocked,
  mountSignIn,
  ownerUnlinked,
  passedStatus,
  runTest,
  setPhone,
  clearPhone,
  text,
} from "./test-support";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  clearPhone();
});

const caption = () =>
  document.querySelector('[data-slot="sign-in-caption"]')!.textContent;

/** A server stub for a provider that is not on yet: it asks for a test first. */
const newProvider =
  (verdict: TestVerdict | null = ownerUnlinked()) =>
  (_p: unknown, testId: string | null): Preview =>
    testId
      ? emptyPreview({ test_verdict: verdict })
      : emptyPreview({
          blockers: [{ code: "test_required", message: "Test first." }],
        });

describe("state a: live and configured", () => {
  it("shows the saved sign-in, the connection and nothing to save", async () => {
    await mountSignIn(liveSnapshot());
    expect(
      screen.getByText(
        "People sign in with a password or with Keycloak. Keycloak lets in /Trusted, and people you invite. Everyone else is kept out.",
      ),
    ).toBeInTheDocument();
    // Ways to sign in
    expect(screen.getByRole("switch", { name: "Email and password" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Keycloak" })).toBeChecked();
    expect(screen.getByText(/^On, tested /)).toBeInTheDocument();
    expect(
      screen.getByText("Your password always works, even when Keycloak is down"),
    ).toBeInTheDocument();
    expect(
      screen.getByDisplayValue(
        "./venv/bin/python proxy.py generate-admin-key owner-cli",
      ),
    ).toHaveAttribute("readonly");
    // The connection is closed and summarised; the test row is there.
    expect(
      screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish · client azizam · tested / }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Provider address")).toBeNull();
    expect(screen.getByText(/^Passed .*\. Opens Keycloak in a new window\.$/)).toBeInTheDocument();
    // The rule, with who it matches
    const rule = screen.getByRole("group", { name: "Rule 1" });
    expect(within(rule).getByText("/Trusted")).toBeInTheDocument();
    expect(within(rule).getByRole("checkbox", { name: "Make administrator" })).toBeChecked();
    expect(within(rule).getByText(/Matches 2 people who have signed in/)).toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: /Keep them out unless I invite them/ }),
    ).toBeChecked();
    // Nothing to save, nothing to discard, no What changes
    expect(caption()).toBe("Nothing to save.");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Discard changes" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save for later" })).toBeNull();
    expect(screen.queryByRole("region", { name: "What changes" })).toBeNull();
  });

  it("sticks the footer to the bottom, above the phone tab bar", async () => {
    await mountSignIn(liveSnapshot());
    const footer = document.querySelector('[data-slot="sign-in-caption"]')!
      .closest(".sticky")!;
    expect(footer).toHaveClass("bottom-16", "sm:bottom-3", "sticky");
    expect(footer).toContainElement(
      screen.getByRole("button", { name: "Save changes" }),
    );
  });

  it("reveals the connection form with a read-only redirect address and tags", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    const redirect = screen.getByLabelText("Copy this into your provider");
    expect(redirect).toHaveValue("https://ai.example.test/admin/oidc/callback");
    expect(redirect).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Copy redirect address" })).toBeEnabled();
    expect(screen.getByLabelText("Provider address")).toHaveValue(
      "https://id.vivid.fish/realms/vivid-fish",
    );
    expect(screen.getByText("Found a sign-in provider at id.vivid.fish.")).toBeInTheDocument();
    expect(screen.getByLabelText("Client secret")).toHaveAttribute("placeholder", "Saved");
    expect(screen.getByText("Leave blank to keep the saved secret.")).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("Keycloak");
    // Advanced: tags say where each value comes from
    await user.click(screen.getByRole("button", { name: "Advanced" }));
    expect(screen.getByLabelText("Scopes")).toHaveValue("openid email profile");
    expect(screen.getAllByText("from discovery").length).toBeGreaterThan(0);
    expect(screen.getByText("default")).toBeInTheDocument();
    // Overriding one shows the tag and a way back to the provider's value
    const scopes = screen.getByLabelText("Scopes");
    await user.clear(scopes);
    await user.type(scopes, "openid");
    expect(screen.getByText("overridden")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Use the provider’s value" }));
    expect(screen.getByLabelText("Scopes")).toHaveValue("openid email profile");
    expect(screen.queryByText("overridden")).toBeNull();
  });

  it("marks the provider 'Changed, not tested' and blocks Save until it is tested again", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: (_p, testId) =>
        testId
          ? emptyPreview({ settings: ["connection_changed"] })
          : emptyPreview({
              settings: ["connection_changed"],
              blockers: [{ code: "test_required", message: "Test." }],
            }),
    });
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    const id = screen.getByLabelText("Client ID");
    await user.clear(id);
    await user.paste("other-client");
    await screen.findByText("Changed, not tested");
    expect(
      await screen.findByText(
        "Test sign-in again before you save. Keycloak keeps working with the old settings until then.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByText(/^Changed since the last test\. Opens Keycloak/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeInTheDocument();
  });
});

describe("state b: after a passed test", () => {
  it("shows the identity, the server's verdict and the time", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: (_p, testId) =>
        emptyPreview({ test_verdict: testId ? { kind: "owner_linked" } : null }),
    });
    await runTest(user);
    expect(await screen.findByText("Keycloak works.")).toBeInTheDocument();
    expect(screen.getByText("just now", { selector: "span" })).toBeInTheDocument();
    const dl = screen.getByText("Groups received").closest("dl")!;
    expect(within(dl).getByText("Tim Nunamaker")).toBeInTheDocument();
    expect(within(dl).getByText("tim.nunamaker@gmail.com")).toBeInTheDocument();
    expect(within(dl).getByText("verified")).toBeInTheDocument();
    expect(within(dl).getByText("/Trusted")).toBeInTheDocument();
    expect(within(dl).getByText("None")).toBeInTheDocument();
    expect(
      await screen.findByText("You are the owner. You can always sign in."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Link / })).toBeNull();
    expect(screen.getByText("Passed just now. Opens Keycloak in a new window.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Azizam remembered the groups it saw: /Trusted. Pick them in the rules below.",
      ),
    ).toBeInTheDocument();
    // Show details reveals the claims
    await user.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText(/Claims received: sub, email, name, groups/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Keycloak works.")).toBeNull();
  });
});

describe("state c: a new rule", () => {
  const family = {
    id: "odl",
    name: "Tim ODL",
    sign_in: { before: true, after: true, via: "provider" as const },
    admin: { before: false, after: false },
    plan: { before: null, after: { id: "plan_assistant", name: "Assistant" } },
    admin_keys: 0,
  };
  const server = (proposal: SignInProposal): Preview => {
    const rules = proposal.rules.rules;
    if (rules.some((r) => r.values.length === 0))
      return emptyPreview({
        blockers: [{ code: "incomplete_rule", message: "Finish the rule." }],
      });
    return rules.length > 1
      ? emptyPreview({ people: [family], unaffected: ["Simon Agent", "Tim Nunamaker"] })
      : emptyPreview();
  };

  async function addFamilyRule(user: Awaited<ReturnType<typeof mountSignIn>>["user"]) {
    await user.click(screen.getByRole("button", { name: "Add a rule" }));
    const rule2 = screen.getByRole("group", { name: "Rule 2" });
    // The picker opens for the new rule; pick the seen group with its count.
    await user.click(within(rule2).getByRole("option", { name: /\/Family/ }));
    await user.click(within(rule2).getByRole("button", { name: "Done" }));
    await user.click(within(rule2).getByRole("combobox", { name: "Give plan" }));
    await user.click(await screen.findByRole("option", { name: "Give plan: Assistant" }));
  }

  it("lists what changes, from the server's preview, and counts it in the footer", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: server,
    });
    await addFamilyRule(user);
    const region = await screen.findByRole("region", { name: "What changes" });
    expect(
      await within(region).findByText(/gets plan Assistant/),
    ).toBeInTheDocument();
    expect(region).toHaveTextContent("Tim ODL: gets plan Assistant (now: no plan).");
    expect(region).toHaveTextContent("Not affected: Simon Agent, Tim Nunamaker.");
    expect(region).toHaveTextContent("You stay the owner.");
    expect(region).toHaveTextContent("People who have not signed in yet are not shown here.");
    expect(caption()).toBe("Changes apply to the next sign-in. You stay signed in.");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeInTheDocument();
    expect(screen.getByText("New, not saved")).toBeInTheDocument();
  });

  it("sums up the changes on a phone as 'N changes · Review'", async () => {
    setPhone(true);
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: server,
    });
    await addFamilyRule(user);
    await screen.findByRole("region", { name: "What changes" });
    await vi.waitFor(() => expect(caption()).toBe("1 change · Review"));
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute(
      "href",
      "#sign-in-changes",
    );
    expect(document.getElementById("sign-in-changes")).toBe(
      screen.getByRole("region", { name: "What changes" }),
    );
  });

  it("counts lines and settings sentences together", async () => {
    setPhone(true);
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: emptyPreview({
        people: [family],
        settings: ["fallback_open", "name_changed"],
      }),
    });
    await user.click(
      screen.getByRole("radio", { name: /Let them in, but with no access/ }),
    );
    await vi.waitFor(() => expect(caption()).toBe("3 changes · Review"));
    expect(screen.getByRole("region", { name: "What changes" })).toHaveTextContent(
      "Everyone else is now let in with no access. The sign-in button reads “Sign in with Keycloak”.",
    );
  });

  it("says no one is affected when the server lists no one", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: emptyPreview({ settings: ["fallback_open"] }),
    });
    await user.click(
      screen.getByRole("radio", { name: /Let them in, but with no access/ }),
    );
    expect(
      await screen.findByText("No one who has signed in before is affected."),
    ).toBeInTheDocument();
  });
});

describe("state d: first-time setup", () => {
  it("turns the provider on only after a passed test", async () => {
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: newProvider(),
    });
    await fillConnection(user, {
      address: "https://id.example.com/realms/home",
      clientId: "azizam",
      secret: "s3cret",
      name: "Keycloak",
    });
    // Before the test: Turn on is off and says why; Test is ready.
    expect(await screen.findByText("Test sign-in first")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn on Keycloak" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Keycloak" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByText("Needs setup")).toBeInTheDocument();
    expect(
      screen.getByText("Sign in once at Keycloak to check that it works. Opens a new window."),
    ).toBeInTheDocument();
    // After the test it can be turned on, and says what that does.
    await runTest(user);
    await screen.findByText("Keycloak works.");
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Turn on Keycloak" })).toBeEnabled(),
    );
    expect(screen.getByText("Passed, not on yet")).toBeInTheDocument();
    expect(caption()).toContain(
      "The test passed. Turning it on adds a Keycloak button to the sign-in page.",
    );
    // The page still says the provider is off until Save.
    expect(
      screen.getByText("People sign in with a password. To add another way to sign in, connect a provider below."),
    ).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Keycloak" })).not.toBeChecked();
  });

  it("asks the owner to link before turning on, without hiding Save for later", async () => {
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: newProvider(),
    });
    await fillConnection(user, {
      address: "https://id.example.com/realms/home",
      clientId: "azizam",
      secret: "s3cret",
      name: "Keycloak",
    });
    await runTest(user);
    expect(
      await screen.findByText("tim.nunamaker@gmail.com at Keycloak is not linked to you yet."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Link it so you can sign in with Keycloak as the owner."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Link tim.nunamaker@gmail.com to my account" }),
    ).toBeEnabled();
    await vi.waitFor(() =>
      expect(caption()).toBe(
        "The test passed. Turning it on adds a Keycloak button to the sign-in page. You are not linked yet, so your own Keycloak sign-in would be turned away. Link first.",
      ),
    );
    expect(screen.getByRole("button", { name: "Save for later" })).toBeEnabled();
  });

  it("shortens the unlinked sentence on a phone (P5)", async () => {
    setPhone(true);
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: newProvider(),
    });
    await fillConnection(user, {
      address: "https://id.example.com/realms/home",
      clientId: "azizam",
      secret: "s3cret",
      name: "Keycloak",
    });
    await runTest(user);
    await vi.waitFor(() =>
      expect(caption()).toBe(
        "The test passed. Turning it on adds a Keycloak button to the sign-in page. Not linked yet. Link first.",
      ),
    );
  });
});

describe("state e: empty first run", () => {
  it("says 'Not set up' and uses neutral wording until an address exists", async () => {
    await mountSignIn(firstRunSnapshot(), { preview: newProvider() });
    expect(screen.getByText("Not set up")).toBeInTheDocument();
    expect(
      screen.getByText(
        "People sign in with a password. To add another way to sign in, connect a provider below.",
      ),
    ).toBeInTheDocument();
    // The form is open, because there is nothing to hide.
    expect(screen.getByLabelText("Provider address")).toHaveValue("");
    expect(
      screen.getByText("Copy the first address into your provider. Then paste its address below."),
    ).toBeInTheDocument();
    expect(screen.getByText("Finish the steps below, then test the sign-in.")).toBeInTheDocument();
    expect(screen.getByText("Needs setup")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Your provider" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    // P1: the reason is a state, not an instruction.
    expect(
      screen.getByText("Stays on until your provider is on, so you can still sign in."),
    ).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Email and password" })).toHaveAttribute(
      "aria-describedby",
      "sign-in-password-why",
    );
    expect(screen.getByText("Fill in the fields above first.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test sign-in" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Turn on your provider" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save for later" })).toBeDisabled();
    expect(caption()).toBe("Test sign-in first");
    expect(screen.getByText("No rules yet.")).toBeInTheDocument();
    expect(text()).not.toMatch(/\ba your provider\b|your own your/i);
  });

  it("shows 'Checking the address...' and then the unreachable line for a bad address", async () => {
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: newProvider(),
      discovery: null,
    });
    const address = screen.getByLabelText("Provider address");
    await user.click(address);
    await user.paste("https://nothing.invalid/realms/x");
    expect(screen.getByText("Checking the address...")).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Could not reach a sign-in provider at that address. Check it and try again.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByLabelText("Client ID"));
    await user.paste("azizam");
    await user.click(screen.getByLabelText("Client secret"));
    await user.paste("s3cret");
    expect(screen.getByText("Fix the provider address first.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test sign-in" })).toBeDisabled();
  });
});

describe("state f: the test failed", () => {
  const failures = [
    {
      cause: "redirect_not_allowed",
      title: "Redirect address refused",
      message:
        "Keycloak refused the redirect address. Add the address at the top of this form to the client’s Valid redirect URIs, then test again.",
    },
    {
      cause: "wrong_secret",
      title: "Client secret not accepted",
      message:
        "Keycloak did not accept the client secret. Copy it again from the client’s Credentials tab.",
    },
    {
      cause: "unverified_email",
      title: "Email not verified",
      message:
        "Keycloak signed you in, but says your email is not verified. Azizam turns away unverified emails.",
    },
    {
      cause: "unreachable",
      title: "Provider not reachable",
      message:
        "Azizam could not reach id.vivid.fish. Check the address and that this host can reach it.",
    },
    {
      cause: "refused",
      title: "Sign-in refused",
      message: "Keycloak refused the sign-in.",
    },
  ] as const;

  it.each(failures)("names the cause, the fix and the details: $cause", async (f) => {
    const { user } = await mountSignIn(liveSnapshot());
    await runTest(user, {
      status: "failed",
      cause: f.cause,
      message: f.message,
      details: `raw: ${f.cause}`,
    });
    expect(await screen.findByText(`Test failed: ${f.title}`)).toBeInTheDocument();
    expect(screen.getByText(f.message, { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(`raw: ${f.cause}`)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText(`raw: ${f.cause}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test again" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeEnabled();
    // A failure does not flip anything on.
    expect(screen.queryByText(/ works\.$/)).toBeNull();
  });

  it("offers Edit connection when the form is closed, and opens it", async () => {
    const { user } = await mountSignIn(liveSnapshot());
    await runTest(user, {
      status: "failed",
      cause: "redirect_not_allowed",
      message: failures[0].message,
      details: "x",
    });
    await screen.findByText("Test failed: Redirect address refused");
    expect(screen.queryByLabelText("Provider address")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Edit connection" }));
    expect(screen.getByLabelText("Provider address")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit connection" })).toBeNull();
  });

  it("shows a missing groups claim as a warning that still counts as a pass", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: (_p, testId) =>
        emptyPreview({ test_verdict: testId ? { kind: "owner_linked" } : null }),
    });
    await runTest(
      user,
      passedStatus({
        warnings: ["no_groups"],
        identity: {
          name: "Tim Nunamaker",
          email: "tim.nunamaker@gmail.com",
          email_verified: true,
          groups: [],
          roles: [],
          claims: ["sub", "email"],
          groups_claim: "groups",
        },
        details: 'Claims received: sub, email\nNo claim named "groups"',
      }),
    );
    expect(
      await screen.findByText("Sign-in worked, but no groups arrived"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Keycloak sent no groups. Rules that use groups will match nobody. Add a groups mapper to the client in Keycloak.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Test failed/)).toBeNull();
    // It passed: the connection counts as tested, and the group rules say they match nobody.
    expect(screen.getByText(/^Passed just now\./)).toBeInTheDocument();
    expect(
      screen.getByText("Keycloak has not sent groups since today. Group rules match nobody."),
    ).toBeInTheDocument();
  });
});

describe("N3: the provider's name is the typed Name, else the host", () => {
  // Fixed help text from the mockup that talks about providers in general. It
  // never names the provider, so it is not part of the name rule.
  const generic = [
    "Connect your provider",
    "Copy this into your provider",
    "Your provider may call it Redirect URI or callback URL.",
    "Paste the secret from your provider.",
    "Filled in from your provider. Change one only if your provider tells you to.",
  ];
  const withoutGenericPhrases = () =>
    generic.reduce((t, g) => t.replaceAll(g, ""), text());

  async function withHostOnly() {
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: newProvider(),
    });
    await fillConnection(user, {
      address: "https://id.example.com/realms/home",
      clientId: "azizam",
      secret: "s3cret",
    });
    await runTest(user);
    await screen.findByText("Sign-in with id.example.com works.");
    await vi.waitFor(() =>
      expect(caption()).toContain("Link first."),
    );
    return user;
  }

  it("uses the host in the row label, button, captions, sentence and sign-in button text", async () => {
    await withHostOnly();
    expect(screen.getByRole("switch", { name: "id.example.com" })).toBeInTheDocument();
    expect(screen.getByText("id.example.com", { selector: "p" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn on id.example.com" })).toBeEnabled();
    expect(
      screen.getByText("Passed just now. Opens id.example.com in a new window."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Copy the first address into id.example.com. Then paste id.example.com’s address below."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Shown on the sign-in page as “Sign in with id.example.com”. Type a friendlier name if you like."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("tim.nunamaker@gmail.com at id.example.com is not linked to you yet."),
    ).toBeInTheDocument();
    expect(caption()).toBe(
      "The test passed. Turning it on adds a “Sign in with id.example.com” button to the sign-in page. You are not linked yet, so your own id.example.com sign-in would be turned away. Link first.",
    );
    expect(
      screen.getByText("Stays on until id.example.com is on, so you can still sign in."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveAttribute("placeholder", "id.example.com");
  });

  it("never reads 'your provider' or 'a your' once an address exists", async () => {
    await withHostOnly();
    const body = withoutGenericPhrases();
    expect(body).not.toMatch(/your provider/i);
    expect(body).not.toMatch(/\ba your\b/i);
    expect(body).not.toMatch(/your own your/i);
  });

  it("lets a typed Name win everywhere", async () => {
    const user = await withHostOnly();
    const name = screen.getByLabelText("Name");
    await user.click(name);
    await user.paste("Family account");
    expect(await screen.findByRole("switch", { name: "Family account" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn on Family account" })).toBeInTheDocument();
    expect(screen.getByText("Family account works.")).toBeInTheDocument();
    expect(
      screen.getByText("Passed just now. Opens Family account in a new window."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Shown on the sign-in page as “Sign in with Family account”."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "id.example.com" })).toBeNull();
    const body = withoutGenericPhrases();
    expect(body).not.toMatch(/your provider/i);
    expect(body).not.toMatch(/Sign-in with id\.example\.com works/);
  });
});

// Keeps the toast mock from being flagged as unused by a stricter linter.
void toast;
void mocked;

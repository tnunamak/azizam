import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, within } from "@testing-library/react";
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

import type {
  Preview,
  PreviewPerson,
  SaveResult,
  SignInProposal,
  TestVerdict,
} from "@/lib/signin-api";
import {
  discovery,
  emptyPreview,
  fillConnection,
  firstRunSnapshot,
  liveSnapshot,
  mocked,
  mountSignIn,
  ownerUnlinked,
  passedStatus,
  runTest,
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

const simon = (over: Partial<PreviewPerson> = {}): PreviewPerson => ({
  id: "simon",
  name: "Simon Agent",
  sign_in: { before: true, after: false, via: "provider" },
  admin: { before: true, after: false },
  plan: { before: null, after: null },
  admin_keys: 2,
  ...over,
});

const saved = (over: Partial<SaveResult["applied"]> = {}): SaveResult => ({
  snapshot: liveSnapshot({
    revision: "rev-2",
    rules: { fallback: "closed", rules: [] },
    rule_matches: {},
  }),
  applied: { provider_turned_on: false, admin_removed: [], ...over },
});

const request = () => mocked.save.mock.calls[0];

describe("Save is off, with the reason, while the server says it cannot run", () => {
  it.each([
    [
      "incomplete_rule",
      "Finish or remove the new rule first.",
    ],
    [
      "address_invalid",
      "Fix the provider address first. Keycloak keeps working with the old settings until then.",
    ],
    [
      "test_required",
      "Test sign-in again before you save. Keycloak keeps working with the old settings until then.",
    ],
    [
      "owner_not_linked",
      "You are not linked yet, so your own Keycloak sign-in would be turned away. Link first.",
    ],
    ["both_methods_off", "Keep one way to sign in on."],
  ] as const)("%s", async (code, expected) => {
    await mountSignIn(liveSnapshot(), {
      preview: emptyPreview({
        blockers: [{ code, message: "Keep one way to sign in on." }],
      }),
    });
    expect(caption()).toBe(expected);
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(document.querySelector('[data-slot="sign-in-caption"]')).toHaveClass(
      "text-warning",
    );
  });

  it("says so when the preview cannot be had", async () => {
    await mountSignIn(liveSnapshot(), {
      preview: () => {
        throw new Error("down");
      },
    });
    expect(caption()).toBe("Could not check what would change. Try again.");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });
});

describe("Discard changes", () => {
  it("puts back the name, the rules, the choice and the footer", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: emptyPreview({ settings: ["name_changed"] }),
    });
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    const name = screen.getByLabelText("Name");
    await user.clear(name);
    await user.paste("Home login");
    await user.click(screen.getByRole("button", { name: "Add a rule" }));
    await user.click(screen.getByRole("radio", { name: /Let them in, but with no access/ }));
    await user.click(screen.getByRole("switch", { name: "Email and password" }));
    expect(screen.getByRole("switch", { name: "Email and password" })).not.toBeChecked();
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Discard changes" })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Keycloak");
    expect(screen.queryByRole("group", { name: "Rule 2" })).toBeNull();
    expect(
      screen.getByRole("radio", { name: /Keep them out unless I invite them/ }),
    ).toBeChecked();
    expect(screen.getByRole("switch", { name: "Email and password" })).toBeChecked();
    expect(screen.queryByRole("button", { name: "Discard changes" })).toBeNull();
    expect(screen.queryByRole("region", { name: "What changes" })).toBeNull();
    await vi.waitFor(() => expect(caption()).toBe("Nothing to save."));
    expect(toast.success).toHaveBeenCalledWith("Changes discarded.");
  });

  it("still lets the owner save an unrelated change while a connection edit is pending", async () => {
    // Round 2, polish 3: the owner can turn the provider off after an untested edit.
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: (p: SignInProposal) =>
        p.methods.provider
          ? emptyPreview({
              blockers: [{ code: "test_required", message: "Test." }],
            })
          : emptyPreview({ settings: ["provider_off"] }),
    });
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    await user.click(screen.getByLabelText("Client ID"));
    await user.paste("x");
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled(),
    );
    await user.click(screen.getByRole("switch", { name: "Keycloak" }));
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    );
  });
});

describe("Save", () => {
  const trustedGone = (p: SignInProposal): Preview =>
    p.rules.rules.length === 0
      ? emptyPreview({
          people: [simon()],
          unaffected: ["Tim Nunamaker"],
        })
      : emptyPreview();

  async function deleteTrusted() {
    const mount = await mountSignIn(liveSnapshot(), { preview: trustedGone });
    await mount.user.click(
      within(screen.getByRole("group", { name: "Rule 1" })).getByRole("button", {
        name: "Remove rule",
      }),
    );
    const region = await screen.findByRole("region", { name: "What changes" });
    await within(region).findByText(/Simon Agent:/);
    return mount;
  }

  it("says who loses what, and offers to keep their admin keys working", async () => {
    const { user } = await deleteTrusted();
    const region = screen.getByRole("region", { name: "What changes" });
    expect(region).toHaveTextContent(
      "Simon Agent: can no longer sign in (uses Keycloak), loses administrator now and is signed out.",
    );
    expect(region).toHaveTextContent(
      "2 admin keys Simon Agent made. They are revoked when you save.",
    );
    expect(region).toHaveTextContent("Not affected: Tim Nunamaker.");
    const keep = within(region).getByRole("checkbox", { name: "Keep them working" });
    expect(keep).not.toBeChecked();
    await user.click(keep);
    expect(keep).toBeChecked();
    expect(region).toHaveTextContent("2 admin keys Simon Agent made. They keep working.");
    await user.click(keep);
    expect(region).toHaveTextContent("They are revoked when you save.");
  });

  it("sends the revision, the kept keys and no test for an unchanged connection", async () => {
    const { user } = await deleteTrusted();
    await user.click(screen.getByRole("checkbox", { name: "Keep them working" }));
    mocked.save.mockResolvedValue(
      saved({
        admin_removed: [{ name: "Simon Agent", keys_revoked: 0, keys_kept: 2 }],
      }),
    );
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() => expect(mocked.save).toHaveBeenCalledTimes(1));
    const [revision, body] = request();
    expect(revision).toBe("rev-1");
    expect(body).toMatchObject({
      keep_admin_keys: ["simon"],
      test_id: null,
      save_for_later: false,
      proposal: {
        methods: { password: true, provider: true },
        rules: { fallback: "closed", rules: [] },
      },
    });
    expect(toast.success).toHaveBeenCalledWith(
      "Saved. Simon Agent lost administrator and was signed out. 2 admin keys Simon Agent made still work.",
    );
    // The saved snapshot is the new base.
    expect(await screen.findByText("No rules yet.")).toBeInTheDocument();
    await vi.waitFor(() => expect(caption()).toBe("Nothing to save."));
    expect(screen.queryByRole("region", { name: "What changes" })).toBeNull();
  });

  it("revokes the keys unless the owner opts out", async () => {
    const { user } = await deleteTrusted();
    mocked.save.mockResolvedValue(
      saved({
        admin_removed: [{ name: "Simon Agent", keys_revoked: 2, keys_kept: 0 }],
      }),
    );
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() => expect(mocked.save).toHaveBeenCalled());
    expect(request()[1].keep_admin_keys).toEqual([]);
    expect(toast.success).toHaveBeenCalledWith(
      "Saved. Simon Agent lost administrator and was signed out. 2 admin keys Simon Agent made were revoked.",
    );
  });

  it("sends the passed test with a changed connection", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: (_p, testId) =>
        testId
          ? emptyPreview({
              settings: ["connection_changed"],
              test_verdict: { kind: "owner_linked" },
            })
          : emptyPreview({
              settings: ["connection_changed"],
              blockers: [{ code: "test_required", message: "Test." }],
            }),
    });
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    const id = screen.getByLabelText("Client ID");
    await user.clear(id);
    await user.paste("azizam-2");
    await runTest(user, passedStatus(), "t9");
    await screen.findByText("Keycloak works.");
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    );
    mocked.save.mockResolvedValue(saved());
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() => expect(mocked.save).toHaveBeenCalled());
    expect(request()[0]).toBe("rev-1");
    expect(request()[1]).toMatchObject({
      test_id: "t9",
      proposal: { provider: { client_id: "azizam-2", client_secret: "" } },
    });
    // The preview was asked with the same test.
    expect(mocked.preview.mock.calls.some(([, id]) => id === "t9")).toBe(true);
  });

  it("turns a tested provider on", async () => {
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: (_p, testId) =>
        testId
          ? emptyPreview({ test_verdict: { kind: "owner_linked" } })
          : emptyPreview({
              blockers: [{ code: "test_required", message: "Test." }],
            }),
    });
    await fillConnection(user, {
      address: "https://id.example.com/realms/home",
      clientId: "azizam",
      secret: "s3cret",
      name: "Keycloak",
    });
    await runTest(user);
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Turn on Keycloak" })).toBeEnabled(),
    );
    mocked.save.mockResolvedValue({
      snapshot: liveSnapshot({ revision: "rev-2" }),
      applied: { provider_turned_on: true, admin_removed: [] },
    });
    await user.click(screen.getByRole("button", { name: "Turn on Keycloak" }));
    await vi.waitFor(() => expect(mocked.save).toHaveBeenCalled());
    expect(request()[0]).toBe("rev-1");
    expect(request()[1]).toMatchObject({
      test_id: "t1",
      save_for_later: false,
      keep_admin_keys: [],
      proposal: {
        methods: { password: true, provider: true },
        provider: {
          name: "Keycloak",
          address: "https://id.example.com/realms/home",
          client_id: "azizam",
          client_secret: "s3cret",
          scopes: "openid email profile",
          groups_claim: "groups",
        },
      },
    });
    expect(toast.success).toHaveBeenCalledWith(
      "Saved. Keycloak is on. A sign-in button now shows on the sign-in page.",
    );
    // The page now shows the saved, live provider.
    expect(await screen.findByText(/^On, tested /)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Keycloak" })).toBeChecked();
  });

  it("Save for later saves the connection with the provider off and no test", async () => {
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: emptyPreview({
        blockers: [{ code: "test_required", message: "Test." }],
      }),
    });
    expect(screen.getByRole("button", { name: "Save for later" })).toBeDisabled();
    await fillConnection(user, {
      address: "https://id.example.com/realms/home",
      clientId: "azizam",
      secret: "s3cret",
      name: "Keycloak",
    });
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save for later" })).toBeEnabled(),
    );
    expect(screen.getByRole("button", { name: "Turn on Keycloak" })).toBeDisabled();
    mocked.save.mockResolvedValue({
      snapshot: firstRunSnapshot({ revision: "rev-2" }),
      applied: { provider_turned_on: false, admin_removed: [] },
    });
    await user.click(screen.getByRole("button", { name: "Save for later" }));
    await vi.waitFor(() => expect(mocked.save).toHaveBeenCalled());
    expect(request()[0]).toBe("rev-1");
    expect(request()[1]).toMatchObject({
      test_id: null,
      save_for_later: true,
      keep_admin_keys: [],
      proposal: {
        methods: { provider: false },
        provider: { address: "https://id.example.com/realms/home", client_secret: "s3cret" },
      },
    });
    expect(toast.success).toHaveBeenCalledWith(
      "Saved. Keycloak stays off until a test passes.",
    );
  });

  it("asks the owner to reload when the settings changed elsewhere", async () => {
    const { user } = await deleteTrusted();
    mocked.save.mockRejectedValue(
      new ApiError("settings_conflict", "Settings changed.", 409),
    );
    mocked.get.mockResolvedValue(
      liveSnapshot({
        revision: "rev-9",
        rules: {
          fallback: "open",
          rules: [],
        },
        rule_matches: {},
      }),
    );
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Sign-in settings changed elsewhere. Reload to see the latest.",
    );
    await user.click(within(alert).getByRole("button", { name: "Reload" }));
    await vi.waitFor(() =>
      expect(
        screen.getByRole("radio", { name: /Let them in, but with no access/ }),
      ).toBeChecked(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("tells a non-owner that only the owner can save", async () => {
    const { user } = await deleteTrusted();
    mocked.save.mockRejectedValue(
      new ApiError("owner_required", "Owner required.", 403),
    );
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only the owner can change sign-in.",
    );
  });
});

describe("turning the provider off (round 3, item 8)", () => {
  const server = (p: SignInProposal): Preview =>
    p.methods.provider
      ? emptyPreview()
      : emptyPreview({
          people: [simon()],
          unaffected: ["Tim Nunamaker"],
          settings: ["provider_off"],
        });

  it("removes administrator from those it granted, with the keys line, like a removed rule", async () => {
    const { user } = await mountSignIn(liveSnapshot(), { preview: server });
    await user.click(screen.getByRole("switch", { name: "Keycloak" }));
    const region = await screen.findByRole("region", { name: "What changes" });
    await vi.waitFor(() =>
      expect(region).toHaveTextContent(
        "Simon Agent: can no longer sign in (uses Keycloak), loses administrator now and is signed out.",
      ),
    );
    expect(region).toHaveTextContent("Keycloak sign-in turns off.");
    expect(region).toHaveTextContent(
      "2 admin keys Simon Agent made. They are revoked when you save.",
    );
    await user.click(within(region).getByRole("checkbox", { name: "Keep them working" }));
    mocked.save.mockResolvedValue({
      snapshot: liveSnapshot({
        revision: "rev-2",
        methods: { password: true, provider: false },
      }),
      applied: {
        provider_turned_on: false,
        admin_removed: [{ name: "Simon Agent", keys_revoked: 0, keys_kept: 2 }],
      },
    });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() => expect(mocked.save).toHaveBeenCalled());
    expect(request()[1]).toMatchObject({
      keep_admin_keys: ["simon"],
      proposal: { methods: { password: true, provider: false } },
    });
    expect(toast.success).toHaveBeenCalledWith(
      "Saved. Simon Agent lost administrator and was signed out. 2 admin keys Simon Agent made still work.",
    );
  });

  it("shows only the 'turns off' line when a new address goes with it (P4)", async () => {
    const { user } = await mountSignIn(liveSnapshot(), {
      preview: emptyPreview({
        issuer_change: null,
        settings: ["provider_off", "connection_changed"],
      }),
    });
    await user.click(screen.getByRole("switch", { name: "Keycloak" }));
    const region = await screen.findByRole("region", { name: "What changes" });
    expect(region).toHaveTextContent("Keycloak sign-in turns off.");
    expect(region).not.toHaveTextContent("become new people");
  });
});

describe("linking the owner", () => {
  const unlinked: TestVerdict = ownerUnlinked();

  it("links the tested account, then reads the new state from the server (P3)", async () => {
    let linked = false;
    const { user } = await mountSignIn(firstRunSnapshot(), {
      preview: (_p, testId) =>
        testId
          ? emptyPreview({
              test_verdict: linked ? { kind: "owner_linked" } : unlinked,
            })
          : emptyPreview({
              blockers: [{ code: "test_required", message: "Test." }],
            }),
    });
    await fillConnection(user, {
      address: "https://id.example.com/realms/home",
      clientId: "azizam",
      secret: "s3cret",
      name: "Keycloak",
    });
    await runTest(user);
    const button = await screen.findByRole("button", {
      name: "Link tim.nunamaker@gmail.com to my account",
    });
    await vi.waitFor(() => expect(caption()).toContain("Link first."));
    const getsBefore = mocked.get.mock.calls.length;
    mocked.link.mockImplementation(async () => {
      linked = true;
      return firstRunSnapshot();
    });
    await user.click(button);
    expect(mocked.link).toHaveBeenCalledWith("t1");
    await vi.waitFor(() =>
      expect(mocked.get.mock.calls.length).toBeGreaterThan(getsBefore),
    );
    expect(
      await screen.findByText("Linked. You can sign in with Keycloak as the owner."),
    ).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith(
      "Linked. You can sign in with Keycloak as the owner.",
    );
    expect(screen.queryByRole("button", { name: /^Link / })).toBeNull();
    await vi.waitFor(() => expect(caption()).not.toContain("Link first."));
    expect(caption()).toBe(
      "The test passed. Turning it on adds a Keycloak button to the sign-in page.",
    );
  });

  it("warns that a new address needs a new link, and stops once linked (P3, P7)", async () => {
    let linked = false;
    const { user } = await mountSignIn(liveSnapshot(), {
      discovery: discovery("id2.vivid.fish"),
      preview: (_p, testId) =>
        emptyPreview({
          issuer_change: { old_host: "id.vivid.fish", new_host: "id2.vivid.fish", people: 3 },
          settings: [],
          test_verdict: testId
            ? linked
              ? { kind: "owner_linked" }
              : { kind: "owner_unlinked", email: "tim.nunamaker@gmail.com", old_link_stale: true }
            : null,
        }),
    });
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    const address = screen.getByLabelText("Provider address");
    await user.clear(address);
    await user.paste("https://id2.vivid.fish/realms/vivid-fish");
    await screen.findByText("Found a sign-in provider at id2.vivid.fish.");
    const region = await screen.findByRole("region", { name: "What changes" });
    expect(region).toHaveTextContent(
      "Everyone who signed in at id.vivid.fish (3 people): become new people at id2.vivid.fish.",
    );
    expect(region).toHaveTextContent(
      "link yourself again after you save. They are checked by your rules again when they sign in.",
    );
    await vi.waitFor(() =>
      expect(caption()).toBe(
        "Changes apply to the next sign-in. You stay signed in. You will need to link your account again.",
      ),
    );
    await runTest(user);
    expect(
      await screen.findByText("Your old link belonged to the old provider address.", {
        exact: false,
      }),
    ).toBeInTheDocument();
    mocked.link.mockImplementation(async () => {
      linked = true;
      return liveSnapshot();
    });
    await user.click(
      screen.getByRole("button", { name: "Link tim.nunamaker@gmail.com to my account" }),
    );
    await screen.findByText("Linked. You can sign in with Keycloak as the owner.");
    await vi.waitFor(() =>
      expect(caption()).toBe("Changes apply to the next sign-in. You stay signed in."),
    );
  });
});

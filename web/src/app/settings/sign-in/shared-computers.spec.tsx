import { afterEach, describe, expect, it, vi } from "vitest";
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

import {
  clearPhone,
  liveSnapshot,
  mocked,
  mountSignIn,
  text,
  until,
} from "./test-support";

afterEach(() => {
  cleanup();
  clearPhone();
});

describe("On shared computers (S12)", () => {
  it("sits after Who can sign in and what they get, and starts on Remember who signed in", async () => {
    await mountSignIn(liveSnapshot());
    const rules = screen.getByRole("region", { name: "Who can sign in and what they get" });
    const block = screen.getByRole("region", { name: "On shared computers" });
    expect(rules.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(block).getByRole("radio", { name: "Remember who signed in" })).toBeChecked();
    expect(within(block).getByRole("radio", { name: "Remember nobody" })).not.toBeChecked();
    expect(within(block).queryByText("Saving forgets every name on every browser.")).toBeNull();
  });

  it("says that saving forgets every name, under Remember nobody, and sends the choice on Save", async () => {
    mocked.save.mockResolvedValue({
      snapshot: liveSnapshot({ memory: "nobody" }),
      applied: { provider_turned_on: false, admin_removed: [] },
    });
    const { user } = await mountSignIn(liveSnapshot());
    const block = screen.getByRole("region", { name: "On shared computers" });
    await user.click(within(block).getByRole("radio", { name: "Remember nobody" }));
    expect(within(block).getByText("Saving forgets every name on every browser.")).toBeInTheDocument();
    await until(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(mocked.save).toHaveBeenCalledTimes(1);
    expect(mocked.save.mock.calls[0][1].proposal.memory).toBe("nobody");
    expect(toast.success).toHaveBeenCalled();
  });
});

describe("the provider's test result on the Ways to sign in row (W5)", () => {
  it("reads On · asks for a password every time when the last test found a fresh sign-in", async () => {
    const snapshot = liveSnapshot();
    await mountSignIn({ ...snapshot, provider: { ...snapshot.provider, fresh_login_supported: true } });
    expect(document.querySelector("#sign-in-provider-status")).toHaveTextContent(
      "On · asks for a password every time",
    );
    expect(text()).not.toContain("doesn’t ask for a password every time");
  });

  it("warns under the provider row when the last test did not find one", async () => {
    const snapshot = liveSnapshot();
    await mountSignIn({ ...snapshot, provider: { ...snapshot.provider, fresh_login_supported: false } });
    expect(document.querySelector("#sign-in-provider-status")).toHaveTextContent(/^On, tested /);
    expect(
      screen.getByText(
        "Keycloak doesn’t ask for a password every time. People on shared computers will need to sign out of Keycloak between people.",
      ),
    ).toBeInTheDocument();
  });
});

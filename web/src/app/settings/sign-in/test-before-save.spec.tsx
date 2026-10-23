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

import type { Preview, SignInProposal, SignInSnapshot } from "@/lib/signin-api";
import {
  clearPhone,
  emptyPreview,
  firstRunSnapshot,
  liveSnapshot,
  mocked,
  mountSignIn,
  until,
} from "./test-support";

afterEach(() => {
  cleanup();
  clearPhone();
});

/**
 * The server's rule (gateway/signin_api.py), as the page meets it: a provider test is needed only when single sign-on
 * is on and the connection is not the saved one, and no passed test covers it.
 */
function serverRule(saved: SignInSnapshot) {
  return (proposal: SignInProposal, testId: string | null): Preview => {
    const unchanged = saved.methods.provider && proposal.provider.client_id === saved.provider.client_id;
    return proposal.methods.provider && !unchanged && !testId
      ? emptyPreview({ blockers: [{ code: "test_required", message: "Test sign-in first." }] })
      : emptyPreview();
  };
}

describe("the test before Save is asked only when single sign-on is on and its connection changed", () => {
  it("with single sign-on off, a change saves directly, with no test and single sign-on still off", async () => {
    mocked.save.mockResolvedValue({
      snapshot: firstRunSnapshot({ memory: "nobody" }),
      applied: { provider_turned_on: false, admin_removed: [] },
    });
    const snapshot = firstRunSnapshot();
    const { user } = await mountSignIn(snapshot, { preview: serverRule(snapshot) });
    const block = screen.getByRole("region", { name: "On shared computers" });
    await user.click(within(block).getByRole("radio", { name: "Remember nobody" }));
    await until(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(mocked.save).toHaveBeenCalledTimes(1);
    const [, request] = mocked.save.mock.calls[0];
    expect(request.proposal.methods.provider).toBe(false);
    expect(request.test_id).toBeNull();
    expect(toast.success).toHaveBeenCalled();
  });

  it("with single sign-on on and its connection unchanged, a save needs no test", async () => {
    mocked.save.mockResolvedValue({
      snapshot: liveSnapshot({ memory: "nobody" }),
      applied: { provider_turned_on: false, admin_removed: [] },
    });
    const snapshot = liveSnapshot();
    const { user } = await mountSignIn(snapshot, { preview: serverRule(snapshot) });
    const block = screen.getByRole("region", { name: "On shared computers" });
    await user.click(within(block).getByRole("radio", { name: "Remember nobody" }));
    await until(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(mocked.save).toHaveBeenCalledTimes(1);
    const [, request] = mocked.save.mock.calls[0];
    expect(request.proposal.methods.provider).toBe(true);
    expect(request.test_id).toBeNull();
  });

  it("with single sign-on on and its connection changed, the save asks for a test first", async () => {
    const snapshot = liveSnapshot();
    const { user } = await mountSignIn(snapshot, { preview: serverRule(snapshot) });
    await user.click(screen.getByRole("button", { name: /Keycloak · id\.vivid\.fish/ }));
    const id = screen.getByLabelText("Client ID");
    await user.clear(id);
    await user.paste("other-client");
    expect(
      await screen.findByText(
        "Test sign-in again before you save. Keycloak keeps working with the old settings until then.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(mocked.save).not.toHaveBeenCalled();
  });
});

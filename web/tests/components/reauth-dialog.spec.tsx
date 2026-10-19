import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ navigate: vi.fn(), confirm: vi.fn() }));
vi.mock("@/lib/navigate", () => ({ navigate: { to: mocks.navigate } }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  confirmWithPassword: mocks.confirm,
}));

import { ReauthGate } from "@/components/shell/reauth-gate";
import { ApiError } from "@/lib/api";
import { requestReauth, settleReauth, type ReauthInfo } from "@/lib/reauth";

const info = (methods: ReauthInfo["methods"]): ReauthInfo => ({
  methods,
  windowSeconds: 900,
  oidcUrl: "/login?reauth=1",
});

/** Open the prompt the way adminFetch does, and wait for the lazy dialog. */
async function open(methods: ReauthInfo["methods"]) {
  render(<ReauthGate />);
  let answer!: Promise<boolean>;
  act(() => {
    answer = requestReauth(info(methods));
  });
  const dialog = await screen.findByRole("dialog", { name: "Confirm it’s you" });
  return { dialog, answer };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/people?tab=plans");
});
afterEach(() => {
  act(() => settleReauth(false));
  cleanup();
});

describe("password", () => {
  it("asks in plain words and focuses the password field", async () => {
    await open(["password"]);
    expect(screen.getByText("This action needs a recent sign-in.")).toBeVisible();
    const field = screen.getByLabelText("Password");
    expect(field).toHaveFocus();
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByRole("button", { name: "Confirm" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /identity provider/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in again" })).not.toBeInTheDocument();
  });

  it("confirms on Enter and lets the waiting request go on", async () => {
    mocks.confirm.mockResolvedValue(undefined);
    const user = userEvent.setup();
    const { answer } = await open(["password"]);
    await user.type(screen.getByLabelText("Password"), "correct horse{Enter}");
    expect(mocks.confirm).toHaveBeenCalledWith("correct horse");
    await expect(answer).resolves.toBe(true);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps the prompt open on a wrong password, shows the message, clears and refocuses", async () => {
    mocks.confirm.mockRejectedValueOnce(
      new ApiError("invalid_password", "That password is not right.", 401),
    );
    const user = userEvent.setup();
    const { dialog, answer } = await open(["password"]);
    const settled = vi.fn();
    void answer.then(settled);
    const field = screen.getByLabelText("Password");
    await user.type(field, "wrong{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("That password is not right.");
    expect(dialog).toBeVisible();
    expect(field).toHaveValue("");
    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(settled).not.toHaveBeenCalled();

    // A second try with the right one goes through.
    mocks.confirm.mockResolvedValueOnce(undefined);
    await user.type(field, "right{Enter}");
    await expect(answer).resolves.toBe(true);
  });

  it("Cancel closes the prompt and answers no", async () => {
    const user = userEvent.setup();
    const { answer } = await open(["password"]);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await expect(answer).resolves.toBe(false);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.confirm).not.toHaveBeenCalled();
  });

  it("Escape closes the prompt and answers no", async () => {
    const user = userEvent.setup();
    const { answer } = await open(["password"]);
    await user.keyboard("{Escape}");
    await expect(answer).resolves.toBe(false);
  });

  it("gives focus back to the control that started the action", async () => {
    const user = userEvent.setup();
    render(<button type="button">Export</button>);
    const starter = screen.getByRole("button", { name: "Export" });
    starter.focus();
    const { answer } = await open(["password"]);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await answer;
    await waitFor(() => expect(starter).toHaveFocus());
  });

  it("opens fresh the next time: an old password or message does not return", async () => {
    mocks.confirm.mockRejectedValueOnce(new ApiError("invalid_password", "Not right.", 401));
    const user = userEvent.setup();
    const first = await open(["password"]);
    await user.type(screen.getByLabelText("Password"), "x{Enter}");
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await first.answer;
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    act(() => {
      void requestReauth(info(["password"]));
    });
    await screen.findByRole("dialog", { name: "Confirm it’s you" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });
});

describe("password and identity provider", () => {
  it("offers the provider beside the password and sends the person back to this page", async () => {
    const user = userEvent.setup();
    await open(["password", "oidc"]);
    expect(screen.getByLabelText("Password")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Continue with your identity provider" }));
    expect(mocks.navigate).toHaveBeenCalledWith(
      `/login?reauth=1&return_to=${encodeURIComponent("/people?tab=plans")}`,
    );
  });
});

describe("identity provider only", () => {
  it("has no password field; the provider button is the way on", async () => {
    const user = userEvent.setup();
    await open(["oidc"]);
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    const go = screen.getByRole("button", { name: "Continue with your identity provider" });
    expect(go).toHaveFocus();
    await user.click(go);
    expect(mocks.navigate).toHaveBeenCalledWith(
      `/login?reauth=1&return_to=${encodeURIComponent("/people?tab=plans")}`,
    );
  });
});

describe("sign in again", () => {
  it("is the only way on when the session can use neither", async () => {
    const user = userEvent.setup();
    await open(["sign_in"]);
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /identity provider/ })).not.toBeInTheDocument();
    const go = screen.getByRole("button", { name: "Sign in again" });
    expect(go).toHaveFocus();
    await user.click(go);
    expect(mocks.navigate).toHaveBeenCalledWith(
      `/sign-in/?return_to=${encodeURIComponent("/people?tab=plans")}`,
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
  });
});

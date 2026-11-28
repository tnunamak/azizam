import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { adminFetch, ApiError, type ConnectionStatus } from "@/lib/api";
import { isLoopbackHost, SubscriptionConnectDialog } from "./subscription-connect-dialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  adminFetch: vi.fn(),
}));

const mocked = vi.mocked(adminFetch);
const kind = { kind: "chatgpt", display_name: "ChatGPT" };
const started = {
  subscription: {
    attempt_id: "attempt-1",
    authorize_url: "https://auth.example.test/authorize?state=fake",
    redirect_uri: "http://127.0.0.1:1455/auth/callback",
    expires_in: 600,
    listening: false,
  },
};
const connected: ConnectionStatus = {
  kind: "chatgpt",
  display_name: "ChatGPT",
  status: "connected",
  connection_id: "chatgpt-owner",
  generation: 2,
  account_email: "owner@example.com",
  connected_at: 1_700_000_100,
  models: ["gpt-a"],
};

function renderDialog(props: Partial<React.ComponentProps<typeof SubscriptionConnectDialog>> = {}) {
  const onConnected = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <SubscriptionConnectDialog
      open
      onOpenChange={onOpenChange}
      kind={kind}
      onConnected={onConnected}
      {...props}
    />,
  );
  return { onConnected, onOpenChange };
}

let open: ReturnType<typeof vi.fn>;
beforeEach(() => {
  mocked.mockReset();
  open = vi.fn();
  vi.stubGlobal("open", open);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

it("opens the sign-in address in a new tab, then finishes with the pasted address", async () => {
  mocked.mockResolvedValueOnce(started);
  const { onConnected } = renderDialog();
  expect(screen.getByRole("heading", { name: "Connect ChatGPT subscription" })).toBeInTheDocument();
  expect(screen.getByText("Connecting signs in to your subscription. Only the people and apps in the access plans you choose can use it.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Open ChatGPT sign-in/ }));
  const field = await screen.findByLabelText(/Address from the browser bar/);
  expect(open).toHaveBeenCalledWith(started.subscription.authorize_url, "_blank", "noopener,noreferrer");
  expect(mocked).toHaveBeenCalledWith("/admin/api/subscriptions/chatgpt/connect", { method: "POST", body: { listen: true } });

  mocked.mockResolvedValueOnce({ subscription: connected });
  fireEvent.change(field, { target: { value: "http://127.0.0.1:1455/auth/callback?code=fake&state=fake" } });
  fireEvent.click(screen.getByRole("button", { name: "Finish" }));
  expect(await screen.findByText(/Connected as owner@example.com/)).toBeInTheDocument();
  expect(mocked).toHaveBeenLastCalledWith("/admin/api/subscriptions/chatgpt/finish", {
    method: "POST",
    body: { attempt_id: "attempt-1", callback: "http://127.0.0.1:1455/auth/callback?code=fake&state=fake" },
  });
  expect(onConnected).toHaveBeenCalledTimes(1);
  // The pasted address is gone from the page and from storage.
  expect(document.body.textContent).not.toContain("code=fake");
  expect(JSON.stringify({ ...window.localStorage })).not.toContain("code=fake");
});

it("shows the API's own message and the next step when the address is wrong", async () => {
  mocked.mockResolvedValueOnce(started);
  renderDialog();
  fireEvent.click(screen.getByRole("button", { name: /Open ChatGPT sign-in/ }));
  const field = await screen.findByLabelText(/Address from the browser bar/);
  mocked.mockRejectedValueOnce(new ApiError("callback_invalid", "That address has no sign-in code in it. Paste the whole address.", 400));
  fireEvent.change(field, { target: { value: "http://127.0.0.1:1455/auth/callback?code=fake" } });
  fireEvent.click(screen.getByRole("button", { name: "Finish" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("That address has no sign-in code in it. Paste the whole address.");
  // The attempt is still open, so the same field stays for another try, emptied.
  expect(screen.getByLabelText(/Address from the browser bar/)).toHaveValue("");
  expect(document.body.textContent).not.toContain("code=fake");
});

it("offers Start again when the attempt is unknown", async () => {
  mocked.mockResolvedValueOnce(started);
  renderDialog();
  fireEvent.click(screen.getByRole("button", { name: /Open ChatGPT sign-in/ }));
  const field = await screen.findByLabelText(/Address from the browser bar/);
  mocked.mockRejectedValueOnce(new ApiError("attempt_not_found", "That sign-in attempt is unknown or expired. Start again.", 404));
  fireEvent.change(field, { target: { value: "http://127.0.0.1:1455/auth/callback?code=fake&state=x" } });
  fireEvent.click(screen.getByRole("button", { name: "Finish" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("unknown or expired");
  fireEvent.click(screen.getByRole("button", { name: "Start again" }));
  expect(await screen.findByRole("button", { name: /Open ChatGPT sign-in/ })).toBeInTheDocument();
});

it("listens on a local page and skips the paste step when the list shows the new sign-in", async () => {
  vi.useFakeTimers();
  // jsdom serves http://localhost:3000 by default.
  expect(["localhost", "127.0.0.1"]).toContain(window.location.hostname);
  mocked.mockResolvedValueOnce({ subscription: { ...started.subscription, listening: true } });
  const before: ConnectionStatus = { ...connected, status: "needs_reconnect", generation: 1 };
  const { onConnected } = renderDialog({ current: before, reconnect: true });
  expect(screen.getByRole("heading", { name: "Reconnect ChatGPT subscription" })).toBeInTheDocument();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Open ChatGPT sign-in/ })); });
  expect(mocked).toHaveBeenCalledWith("/admin/api/subscriptions/chatgpt/connect", { method: "POST", body: { listen: true } });
  mocked.mockResolvedValue({ subscriptions: [before] });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument();
  mocked.mockResolvedValue({ subscriptions: [connected] });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByText(/Connected as owner@example.com/)).toBeInTheDocument();
  expect(onConnected).toHaveBeenCalledTimes(1);
});

it("falls back to the paste step after two minutes of listening", async () => {
  vi.useFakeTimers();
  mocked.mockResolvedValueOnce({ subscription: { ...started.subscription, listening: true } });
  renderDialog();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Open ChatGPT sign-in/ })); });
  mocked.mockResolvedValue({ subscriptions: [{ kind: "chatgpt", status: "not_connected" }] });
  await act(async () => { await vi.advanceTimersByTimeAsync(121_000); });
  const calls = mocked.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(mocked.mock.calls.length).toBe(calls); // polling stopped
  expect(screen.getByLabelText(/Address from the browser bar/)).toBeInTheDocument();
});

it("clears the pasted address when the dialog closes", async () => {
  mocked.mockResolvedValueOnce(started);
  const view = render(
    <SubscriptionConnectDialog open onOpenChange={() => {}} kind={kind} onConnected={() => {}} />,
  );
  fireEvent.click(screen.getByRole("button", { name: /Open ChatGPT sign-in/ }));
  const field = await screen.findByLabelText(/Address from the browser bar/);
  fireEvent.change(field, { target: { value: "http://127.0.0.1:1455/auth/callback?code=fake" } });
  view.rerender(<SubscriptionConnectDialog open={false} onOpenChange={() => {}} kind={kind} onConnected={() => {}} />);
  expect(document.body.textContent).not.toContain("code=fake");
  mocked.mockResolvedValueOnce(started);
  view.rerender(<SubscriptionConnectDialog open onOpenChange={() => {}} kind={kind} onConnected={() => {}} />);
  expect(screen.getByRole("button", { name: /Open ChatGPT sign-in/ })).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByLabelText(/Address from the browser bar/)).not.toBeInTheDocument());
});

it("asks the gateway to listen only for a page on the gateway computer", () => {
  expect(isLoopbackHost("127.0.0.1")).toBe(true);
  expect(isLoopbackHost("localhost")).toBe(true);
  expect(isLoopbackHost("gateway.example.test")).toBe(false);
  expect(isLoopbackHost("example.test")).toBe(false);
});

const claude = { kind: "claude", display_name: "Claude", paste_kind: "code" as const, sign_in: "browser" as const };

it("opens Claude sign-in and sends the pasted code", async () => {
  mocked.mockResolvedValueOnce(started);
  renderDialog({ kind: claude });
  expect(document.body.textContent).not.toMatch(/CLAUDE_CONFIG_DIR|auth login|terminal|command/);
  expect(screen.queryByText(/Import that sign-in instead/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Open Claude sign-in" }));
  const field = await screen.findByLabelText("Code from Claude");
  expect(screen.getByText("Claude shows a code after you sign in. Paste it here.")).toBeInTheDocument();
  mocked.mockResolvedValueOnce({ subscription: { ...connected, ...claude } });
  fireEvent.change(field, { target: { value: " good#state " } });
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(/Connected as/);
  expect(mocked).toHaveBeenLastCalledWith("/admin/api/subscriptions/claude/finish", {
    method: "POST", body: { attempt_id: "attempt-1", callback: "good#state" },
  });
  expect(document.body.textContent).not.toMatch(/CLAUDE_CONFIG_DIR|auth login|terminal|command/);
});

it("shows Claude's 409 refusal in an alert", async () => {
  mocked.mockResolvedValueOnce(started);
  renderDialog({ kind: claude });
  fireEvent.click(screen.getByRole("button", { name: "Open Claude sign-in" }));
  const field = await screen.findByLabelText("Code from Claude");
  mocked.mockRejectedValueOnce(new ApiError("plan_sign_in_refused", "Claude didn't accept that code. Start the sign-in again.", 409));
  fireEvent.change(field, { target: { value: "bad" } });
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Claude didn't accept that code. Start the sign-in again.");
  expect(document.body.textContent).not.toMatch(/CLAUDE_CONFIG_DIR|auth login|terminal|command/);
});

it("connects an already signed-in Claude plan immediately", async () => {
  mocked.mockResolvedValueOnce({ subscription: { ...connected, ...claude } });
  renderDialog({ kind: claude });
  fireEvent.click(screen.getByRole("button", { name: "Open Claude sign-in" }));
  await screen.findByText(/Connected as/);
  expect(open).not.toHaveBeenCalled();
});

it("polls Claude without spending a pending code attempt", async () => {
  vi.useFakeTimers();
  mocked.mockResolvedValueOnce(started);
  renderDialog({ kind: claude });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Open Claude sign-in" })); });
  mocked.mockResolvedValueOnce({ subscription: { status: "pending" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByLabelText("Code from Claude")).toBeInTheDocument();
  mocked.mockResolvedValueOnce({ subscription: { ...connected, ...claude } });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByText(/Connected as/)).toBeInTheDocument();
  expect(mocked).toHaveBeenLastCalledWith("/admin/api/subscriptions/claude/finish", {
    method: "POST", body: { attempt_id: "attempt-1", callback: "" },
  });
});

it("offers a download for a missing runner without a command", () => {
  renderDialog({ kind: claude, current: { ...claude, status: "not_connected", runner: { state: "missing" } } });
  expect(screen.getByRole("button", { name: "Install Claude Code" })).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/run:|\$ |curl |\| bash/i);
});

it("has no file import in the ChatGPT dialog", () => {
  renderDialog();
  expect(screen.queryByText(/Import that sign-in instead/)).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

it("shows progress then moves to sign-in without a reload", async () => {
  vi.useFakeTimers();
  mocked.mockResolvedValueOnce({ subscription: { ...claude, status: "not_connected", runner: { state: "installing" } } });
  renderDialog({ kind: claude, current: { ...claude, status: "not_connected", runner: { state: "missing" } } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Install Claude Code" })); });
  expect(screen.getByRole("status")).toHaveTextContent("Installing Claude Code");
  expect(screen.getByRole("button", { name: "Installing…" })).toBeDisabled();
  expect(mocked).toHaveBeenCalledWith("/admin/api/subscriptions/claude/install-runner", { method: "POST", body: {} });
  mocked.mockResolvedValueOnce({ subscriptions: [{ ...claude, status: "not_connected", runner: { state: "ready", version: "1.2.3" } }] });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByRole("button", { name: "Open Claude sign-in" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Open Claude sign-in" })).toHaveFocus();
  expect(document.body.textContent).not.toMatch(/run:|\$ |curl |\| bash/i);
});

it("says how much of the download has arrived", () => {
  renderDialog({ kind: claude, current: { ...claude, status: "not_connected", runner: { state: "installing", percent: 42 } } });
  expect(screen.getByRole("status")).toHaveTextContent("Installing Claude Code… 42%");
});

it("shows a failed download with retry and help", async () => {
  mocked.mockResolvedValueOnce({ subscription: { ...claude, status: "not_connected", runner: { state: "installing" } } });
  renderDialog({ kind: { ...claude, install_page_url: "https://example.test/help" }, current: {
    ...claude, status: "not_connected", runner: { state: "failed", error: { code: "runner_install_download", message: "Check the internet connection, then try again." } },
  } });
  expect(screen.getByRole("alert")).toHaveTextContent("Check the internet connection");
  expect(screen.getByRole("link", { name: "Installation help" })).toHaveAttribute("href", "https://example.test/help");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Installing Claude Code");
  expect(document.body.textContent).not.toMatch(/run:|\$ |curl |\| bash/i);
});

it("keeps waiting, and says so, when the progress check fails", async () => {
  vi.useFakeTimers();
  renderDialog({ kind: claude, current: { ...claude, status: "not_connected", runner: { state: "installing" } } });
  mocked.mockRejectedValueOnce(new Error("offline"));
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByText(/download is still running/)).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Installing Claude Code");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  mocked.mockResolvedValueOnce({ subscriptions: [{ ...claude, status: "not_connected", runner: { state: "ready" } }] });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.queryByText(/download is still running/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open Claude sign-in" })).toHaveFocus();
});

it("moves focus to the code field on the paste step", async () => {
  mocked.mockResolvedValueOnce(started);
  renderDialog({ kind: claude });
  fireEvent.click(screen.getByRole("button", { name: /Open Claude sign-in/ }));
  const field = await screen.findByLabelText(/Code from Claude/);
  expect(field).toHaveFocus();
});

const failedRunner: ConnectionStatus["runner"] = {
  state: "failed",
  error: { code: "runner_install_download", message: "Example download could not finish." },
};

it("offers sign-in after a previously failed download reports ready", async () => {
  mocked.mockResolvedValueOnce({ subscription: { ...claude, status: "not_connected", runner: { state: "ready" } } });
  renderDialog({ kind: claude, current: { ...claude, status: "not_connected", runner: failedRunner } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Try again" })); });
  expect(screen.getByRole("button", { name: "Open Claude sign-in" })).toBeEnabled();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("uses the latest reported download percent even when lower", async () => {
  vi.useFakeTimers();
  renderDialog({ kind: claude, current: { ...claude, status: "not_connected", runner: { state: "installing", percent: 80 } } });
  expect(screen.getByRole("status")).toHaveTextContent("80%");
  mocked.mockResolvedValueOnce({ subscriptions: [{ ...claude, status: "not_connected", runner: { state: "installing", percent: 42 } }] });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByRole("status")).toHaveTextContent("42%");
  expect(screen.getByRole("status")).not.toHaveTextContent("80%");
});

it("shows retry without an installation-help link when no help address is set", () => {
  renderDialog({ kind: { ...claude, install_page_url: undefined }, current: { ...claude, status: "not_connected", runner: failedRunner } });
  expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  expect(screen.queryByRole("link", { name: "Installation help" })).not.toBeInTheDocument();
});

it("returns to the failed state when the retried download fails again", async () => {
  const retryFailure = { ...failedRunner, error: { code: "runner_install_download", message: "Example retry also failed." } };
  mocked.mockResolvedValueOnce({ subscription: { ...claude, status: "not_connected", runner: retryFailure } });
  renderDialog({ kind: claude, current: { ...claude, status: "not_connected", runner: failedRunner } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Try again" })); });
  expect(mocked).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("alert")).toHaveTextContent("Example retry also failed.");
  expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "Installing…" })).not.toBeInTheDocument();
});

it("still shows the latest supplied percent after closing and reopening", () => {
  const onOpenChange = vi.fn();
  const onConnected = vi.fn();
  const dialog = (open: boolean, percent: number) => (
    <SubscriptionConnectDialog open={open} onOpenChange={onOpenChange} kind={claude} onConnected={onConnected}
      current={{ ...claude, status: "not_connected", runner: { state: "installing", percent } }} />
  );
  const { rerender } = render(dialog(true, 25));
  expect(screen.getByRole("status")).toHaveTextContent("25%");
  rerender(dialog(false, 42));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  rerender(dialog(true, 42));
  expect(screen.getByRole("status")).toHaveTextContent("42%");
});

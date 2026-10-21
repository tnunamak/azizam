import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi, type CheckResult } from "@/lib/engines-api";
import { check, definitions, renderWithClient } from "../__fixtures__/test-utils";
import { AddEngineDialog } from "./add-engine-dialog";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Tim", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

type User = ReturnType<typeof userEvent.setup>;

function mount(checkRevealMs = 0) {
  return renderWithClient(
    <AddEngineDialog open onOpenChange={() => undefined} onAdded={vi.fn()} onOpenEngine={vi.fn()} checkRevealMs={checkRevealMs} />,
  );
}

async function pickKind(user: User, title: RegExp = /Any OpenAI-compatible server/) {
  await user.click(await screen.findByRole("button", { name: title }));
  return screen.getByRole("dialog", { name: "Add engine" });
}

async function typeAddress(user: User, address: string) {
  const field = screen.getByLabelText(/^Address/);
  await user.clear(field);
  await user.type(field, address);
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Tim", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const ENV_CONTEXT =
  "Azizam could not use the key from this variable. Check the variable name and its value on the computer running Azizam, then check again.";

describe("a key finding sits under the credential control that is in use", () => {
  it("environment-variable branch: the finding, its context line and the server's own reason show; the control is invalid and described by them", async () => {
    const user = userEvent.setup();
    const refused = check("oai_auth");
    vi.spyOn(enginesApi, "check").mockResolvedValue(refused);
    mount();
    const dialog = await pickKind(user);
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Read the key from a server environment variable instead" }));
    await user.type(within(dialog).getByLabelText("Environment variable name"), "LAN_SERVER_KEY");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));

    const card = await within(dialog).findByRole("alert", { name: "Key refused" });
    const finding = refused.findings[0];
    expect(within(card).getByText(finding.why)).toBeVisible();
    expect(within(card).getByText(finding.fix)).toBeVisible();
    expect(within(dialog).getByText(ENV_CONTEXT)).toBeVisible();
    const env = within(dialog).getByLabelText("Environment variable name");
    expect(env).toHaveAttribute("aria-invalid", "true");
    expect(env).toHaveAccessibleDescription(expect.stringContaining(finding.what));
    expect(env).toHaveAccessibleDescription(expect.stringContaining(finding.fix));
    expect(env).toHaveAccessibleDescription(expect.stringContaining(ENV_CONTEXT));
    // The card sits below the variable field, not below the address.
    const field = env.closest("div.flex.flex-col") as HTMLElement;
    expect(field).toContainElement(card);
    // The action buttons stay out of the description.
    expect(env).not.toHaveAccessibleDescription(expect.stringContaining("Paste the key here instead"));
  });

  it("pasted-key branch: the control is invalid and described by the finding, and the variable context line is not shown", async () => {
    const user = userEvent.setup();
    const refused = check("oai_auth");
    vi.spyOn(enginesApi, "check").mockResolvedValue(refused);
    mount();
    const dialog = await pickKind(user);
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.type(within(dialog).getByLabelText("API key"), "sk-bad");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByRole("alert", { name: "Key refused" });
    const key = within(dialog).getByLabelText("API key");
    expect(key).toHaveAttribute("aria-invalid", "true");
    expect(key).toHaveAccessibleDescription(expect.stringContaining(refused.findings[0].what));
    expect(within(dialog).queryByText(ENV_CONTEXT)).toBeNull();
  });
});

describe("a finding is described on its input, and required fields say so", () => {
  it("an address finding joins the address description; the fix button does not", async () => {
    const user = userEvent.setup();
    const missing = check("oai_path");
    vi.spyOn(enginesApi, "check").mockResolvedValue(missing);
    mount();
    const dialog = await pickKind(user);
    await typeAddress(user, "http://192.168.1.50:8080");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByTestId("finding");
    const address = within(dialog).getByLabelText(/^Address/);
    expect(address).toHaveAttribute("aria-invalid", "true");
    expect(address).toHaveAccessibleDescription(expect.stringContaining(missing.findings[0].why));
    expect(address).toHaveAccessibleDescription(expect.stringContaining("Where the server listens"));
    expect(address).not.toHaveAccessibleDescription(expect.stringContaining("Add /v1 and check again"));
  });

  it("the required star is matched by aria-required on the input", async () => {
    const user = userEvent.setup();
    mount();
    const dialog = await pickKind(user);
    expect(within(dialog).getByLabelText(/^Address/)).toHaveAttribute("aria-required", "true");
    expect(within(dialog).getByLabelText("API key")).not.toHaveAttribute("aria-required");
  });
});

describe("no fake progress while the check runs", () => {
  it("rows stay neutral and the wait text shows until the server answers", async () => {
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockImplementation(() => new Promise<CheckResult>(() => undefined));
    mount();
    const dialog = await pickKind(user);
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    const rows = within(dialog).getByRole("list", { name: "Check progress" });
    expect(within(dialog).getByText("Checking the server. This may take a few seconds.")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    expect(rows.querySelector(".lucide-circle-check")).toBeNull();
    expect(within(rows).queryByText("Reached the server")).toBeNull();
    expect(within(dialog).queryByText(/Reached the server/)).toBeNull();
  });

  it("the real result ticks the rows", async () => {
    const user = userEvent.setup();
    const ok = check("oai_ok");
    let answer!: (result: CheckResult) => void;
    vi.spyOn(enginesApi, "check").mockImplementation(() => new Promise<CheckResult>((resolve) => (answer = resolve)));
    mount(400);
    const dialog = await pickKind(user);
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const rows = await within(dialog).findByRole("list", { name: "Check progress" });
    expect(rows.querySelector(".lucide-circle-check")).toBeNull();
    await act(async () => answer(ok));
    await waitFor(() => expect(rows.querySelectorAll(".lucide-circle-check")).toHaveLength(within(rows).getAllByRole("listitem").length));
    await within(dialog).findByText("Step 3 of 5 · What it can do");
  });
});

describe("focus follows the step", () => {
  it("each step heading takes focus: kind, address, abilities, models, name, and Back", async () => {
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_ok"));
    mount();
    const dialog = await pickKind(user);
    const address = within(dialog).getByRole("heading", { name: "Where is the server?" });
    expect(address).toHaveAttribute("tabindex", "-1");
    expect(address).toHaveFocus();
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByText("Step 3 of 5 · What it can do");
    expect(within(dialog).getByRole("heading", { name: "What this server can do" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByRole("heading", { name: "Models and limits" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByRole("heading", { name: "Name and create" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Back" }));
    expect(within(dialog).getByRole("heading", { name: "Models and limits" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Back" }));
    expect(within(dialog).getByRole("heading", { name: "What this server can do" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Back" }));
    expect(within(dialog).getByRole("heading", { name: "Where is the server?" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Change kind" }));
    const kind = within(dialog).getByRole("heading", { name: "What kind of server is it?" });
    expect(kind).toHaveAttribute("tabindex", "-1");
    expect(kind).toHaveFocus();
  });

  it("a local validation failure focuses the address field", async () => {
    const user = userEvent.setup();
    mount();
    const dialog = await pickKind(user);
    await typeAddress(user, "192.168.1.50:8080");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const field = within(dialog).getByLabelText(/^Address/);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveFocus();
  });

  it("a refused key from the server puts focus on the key", async () => {
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_auth"));
    mount();
    const dialog = await pickKind(user);
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByRole("alert", { name: "Key refused" });
    expect(within(dialog).getByLabelText("API key")).toHaveFocus();
  });
});

describe("one live region across the check", () => {
  it("the same polite node survives from the address step to step 3 and says how the check ended", async () => {
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_ok"));
    mount();
    const dialog = await pickKind(user);
    const live = within(dialog).getByTestId("wizard-announcement");
    expect(live).toHaveAttribute("role", "status");
    expect(live).toHaveAttribute("aria-live", "polite");
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByText("Step 3 of 5 · What it can do");
    expect(within(dialog).getByTestId("wizard-announcement")).toBe(live);
    expect(live).toHaveTextContent("Check finished: reached the server, 3 models found.");
    expect(live).toBeInTheDocument();
  });

  it("the same node carries the finding's title when the check fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_auth"));
    mount();
    const dialog = await pickKind(user);
    const live = within(dialog).getByTestId("wizard-announcement");
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByRole("alert", { name: "Key refused" });
    expect(within(dialog).getByTestId("wizard-announcement")).toBe(live);
    expect(live).toHaveTextContent("Key refused");
  });
});

describe("copy from the walkthrough", () => {
  it("step 2, step 3, the shortcut and the recap say the new things", async () => {
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_ok"));
    mount();
    const dialog = await pickKind(user);
    expect(within(dialog).getByText(/^Enter the server address and a key if it needs one\./)).toBeVisible();
    expect(within(dialog).queryByText(/Two fields/)).toBeNull();
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByText("Step 3 of 5 · What it can do");
    expect(within(dialog).getByText(/Review the selected abilities\. They become available when you add the engine\./)).toBeVisible();
    expect(within(dialog).queryByText(/Nothing turns on until you do/)).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Add engine now" })).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: "Add now with these settings" })).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByTestId("recap")).toHaveTextContent("KeyNo key supplied.");
  });
});

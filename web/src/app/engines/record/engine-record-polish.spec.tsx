import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/api";
import { enginesApi, type CheckResult, type EngineView } from "@/lib/engines-api";
import { accessAnswer, check, definitions, engineView, renderWithClient } from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Tim", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const onOpenEngine = vi.fn();
let view: EngineView;

function mount(over: Partial<EngineView> = {}) {
  view = engineView(over);
  vi.spyOn(enginesApi, "view").mockImplementation(async () => structuredClone(view));
  return renderWithClient(
    <EngineRecordPage
      serviceId={view.service_id}
      onBack={() => undefined}
      onRemove={() => undefined}
      onOpenEngine={onOpenEngine}
    />,
  );
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Tim", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("nobody"));
});
afterEach(() => {
  cleanup();
  onOpenEngine.mockReset();
  vi.restoreAllMocks();
});

function refusal(status: number, code: string, message: string, field?: string, checkResult?: CheckResult) {
  return new ApiError(code, message, status, { error: { code, message, ...(field ? { field } : {}) }, ...(checkResult ? { check: checkResult } : {}) });
}

describe("a warning from the check on the record page", () => {
  it("is amber, says to review it, and shows the warning; it is never the green reached-the-server line", async () => {
    const user = userEvent.setup();
    const warning = check("oai_nocap");
    mount();
    vi.spyOn(enginesApi, "check").mockResolvedValue(warning);
    await user.click(await screen.findByRole("button", { name: "Check again" }));
    const line = await screen.findByTestId("check-line");
    await waitFor(() =>
      expect(line).toHaveTextContent(/^Reached the server, but the check found a warning\. Review it below before saving\.$/),
    );
    expect(line.querySelector(".text-success")).toBeNull();
    expect(line.querySelector(".text-warning")).not.toBeNull();
    const card = screen.getByRole("status", { name: warning.findings[0].title });
    expect(within(card).getByText(warning.findings[0].why)).toBeVisible();
    expect(within(card).getByRole("button", { name: "Check again" })).toBeVisible();
  });
});

describe("a failed Save is shown at its field", () => {
  /** A changed address cannot be saved until it is checked; "Save and keep off" is the way that sends it anyway. */
  async function failSave(error: ApiError, edit: (user: ReturnType<typeof userEvent.setup>) => Promise<void>, button = "Save changes") {
    const user = userEvent.setup();
    mount();
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    await edit(user);
    vi.spyOn(enginesApi, "update").mockRejectedValue(error);
    await user.click(screen.getByRole("button", { name: button }));
    await screen.findByText("The changes could not be saved");
    return user;
  }

  it("name: the message is the Name field's error, and Name takes focus", async () => {
    await failSave(refusal(400, "invalid_engine", "Give the engine a name.", "name"), async (user) => {
      await user.type(await screen.findByLabelText("Name"), "!");
    });
    const name = screen.getByLabelText("Name");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveAccessibleDescription("Give the engine a name.");
    expect(name).toHaveFocus();
  });

  it("address with a fix: the finding sits under Address with its action, and the action applies and checks again", async () => {
    const missing = check("oai_path");
    const run = vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_ok"));
    const user = await failSave(refusal(422, "engine_check_failed", missing.findings[0].what, "address", missing), async (u) => {
      const address = await screen.findByLabelText("Address");
      await u.clear(address);
      await u.type(address, "http://192.168.1.50:8080");
    }, "Save and keep off");
    const address = screen.getByLabelText("Address");
    expect(address).toHaveAttribute("aria-invalid", "true");
    expect(address).toHaveAccessibleDescription(expect.stringContaining(missing.findings[0].why));
    expect(address).toHaveFocus();
    const field = address.closest("div.flex.flex-col") as HTMLElement;
    const card = within(field).getByRole("alert", { name: missing.findings[0].title });
    await user.click(within(card).getByRole("button", { name: "Add /v1 and check again" }));
    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(run.mock.calls[0][0]).toMatchObject({ service_id: "garage-gpu", values: { address: "http://192.168.1.50:8080/v1" } });
    expect(screen.getByLabelText("Address")).toHaveValue("http://192.168.1.50:8080/v1");
  });

  it("a duplicate address offers to open the engine that has it", async () => {
    const dup = check("oai_dup");
    const user = await failSave(refusal(409, "engine_duplicate", dup.findings[0].what, "address", dup), async (u) => {
      const address = await screen.findByLabelText("Address");
      await u.clear(address);
      await u.type(address, "http://192.168.1.20:8080/v1");
    }, "Save and keep off");
    const field = (screen.getByLabelText("Address").closest("div.flex.flex-col")) as HTMLElement;
    await user.click(within(field).getByRole("button", { name: "Open Kitchen mini PC" }));
    expect(onOpenEngine).toHaveBeenCalledWith("kitchen");
  });

  it("key: the key box opens, is invalid, is described by the finding and takes focus", async () => {
    const refused = check("oai_auth");
    await failSave(refusal(422, "engine_check_failed", refused.findings[0].what, "key", refused), async (user) => {
      await user.type(await screen.findByLabelText("Name"), "!");
    });
    const key = await screen.findByPlaceholderText("Paste the key");
    expect(key).toHaveAttribute("aria-invalid", "true");
    expect(key).toHaveAccessibleDescription(expect.stringContaining(refused.findings[0].fix));
    expect(key).toHaveFocus();
  });

  it("an option field: More options opens, the field shows the message, and it takes focus", async () => {
    await failSave(
      refusal(400, "invalid_engine", "Time to wait for a reply must be between 1 and 3600.", "reply_timeout_s"),
      async (user) => {
        await user.type(await screen.findByLabelText("Name"), "!");
      },
    );
    const more = screen.getByText("More options", { selector: "summary span" }).closest("details") as HTMLElement;
    expect(more).toHaveAttribute("open");
    const timeout = within(more).getByLabelText("Time to wait for a reply");
    expect(timeout).toHaveAttribute("aria-invalid", "true");
    expect(timeout).toHaveAccessibleDescription("Time to wait for a reply must be between 1 and 3600.");
    expect(timeout).toHaveFocus();
  });

  it("headers: More options opens and the message sits with the headers", async () => {
    await failSave(
      refusal(400, "invalid_engine", "Header 1 needs a name and a value.", "headers"),
      async (user) => {
        await user.type(await screen.findByLabelText("Name"), "!");
      },
    );
    const more = screen.getByText("More options", { selector: "summary span" }).closest("details") as HTMLElement;
    expect(more).toHaveAttribute("open");
    const group = within(more).getByRole("group", { name: "Request headers" });
    expect(group).toHaveAccessibleDescription("Header 1 needs a name and a value.");
    expect(group).toHaveFocus();
  });

  it("the general message stays as a summary", async () => {
    await failSave(refusal(400, "invalid_engine", "Give the engine a name.", "name"), async (user) => {
      await user.type(await screen.findByLabelText("Name"), "!");
    });
    expect(screen.getByText("The changes could not be saved")).toBeVisible();
  });
});

describe("Retry on the record page", () => {
  it("refetches the definitions when they are what failed, and says the settings could not load", async () => {
    const user = userEvent.setup();
    vi.mocked(enginesApi.definitions).mockRejectedValueOnce(new Error("down"));
    mount();
    expect(await screen.findByText("Azizam couldn’t load this engine’s settings. Try again.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { level: 1, name: "garage-gpu" })).toBeVisible();
    expect(enginesApi.definitions).toHaveBeenCalledTimes(2);
  });

  it("refetches the view when it is what failed", async () => {
    const user = userEvent.setup();
    view = engineView();
    const load = vi
      .spyOn(enginesApi, "view")
      .mockRejectedValueOnce(new Error("down"))
      .mockImplementation(async () => structuredClone(view));
    renderWithClient(<EngineRecordPage serviceId="garage-gpu" onBack={() => undefined} onRemove={() => undefined} />);
    expect(await screen.findByText("Azizam couldn’t load this engine’s settings. Try again.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { level: 1, name: "garage-gpu" })).toBeVisible();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("a missing engine keeps its own message", async () => {
    vi.spyOn(enginesApi, "view").mockRejectedValue(new ApiError("not_found", "No such engine.", 404));
    renderWithClient(<EngineRecordPage serviceId="gone" onBack={() => undefined} onRemove={() => undefined} />);
    expect(await screen.findByText("The engine is no longer on this gateway. Go back to Engines.")).toBeVisible();
  });
});

describe("stored record", () => {
  it("the stored record and Other settings sit in a collapsed Stored record disclosure", async () => {
    const user = userEvent.setup();
    mount();
    const summary = (await screen.findByRole("heading", { name: "Stored record" })).closest("summary") as HTMLElement;
    const details = summary.closest("details") as HTMLElement;
    expect(details).not.toHaveAttribute("open");
    expect(within(details).getByText("Saved configuration for troubleshooting. Secrets are hidden.")).toBeInTheDocument();
    expect(within(details).getByLabelText("Stored record, read only")).toBeInTheDocument();
    expect(within(details).getByRole("heading", { name: "Other settings" })).toBeInTheDocument();
    expect(screen.getByLabelText("Stored record, read only")).not.toBeVisible();
    await user.click(summary);
    expect(screen.getByLabelText("Stored record, read only")).toBeVisible();
    expect(screen.getByText("probe_interval_s")).toHaveClass("[overflow-wrap:anywhere]");
  });
});

describe("phone targets on the record page", () => {
  it("the section links are inline-flex with a 44 px phone height and padding", async () => {
    mount();
    const jump = await screen.findByRole("navigation", { name: "On this page" });
    for (const link of within(jump).getAllByRole("link")) {
      expect(link).toHaveClass("inline-flex", "max-sm:min-h-11");
      expect(link.className).toMatch(/\bpx-\d/);
    }
  });
});

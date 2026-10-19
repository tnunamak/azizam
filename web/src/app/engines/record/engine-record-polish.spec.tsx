import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api, ApiError } from "@/lib/api";
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
    vi.spyOn(api.services, "edit").mockRejectedValue(error);
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

  it("key: a refused command keeps the replacement draft and offers an explicit retry", async () => {
    const user = userEvent.setup();
    mount({ revision: '"os-4"' });
    const key = await screen.findByLabelText("API key");
    const error = refusal(400, "invalid_value", "The replacement key is not valid.");
    const write = vi.spyOn(enginesApi, "secretEntries").mockRejectedValueOnce(error).mockResolvedValueOnce({ schema_version: 1, services: {} } as never);
    const update = vi.spyOn(enginesApi, "update");
    await user.type(key, "replacement-key");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("The changes could not be saved")).toBeVisible();
    expect(screen.getByText(error.message)).toBeVisible();
    expect(key).toHaveValue("replacement-key");
    expect(write).toHaveBeenCalledTimes(1);
    expect(update).not.toHaveBeenCalled();
    await user.clear(key);
    await user.type(key, "corrected-key");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    expect(write.mock.calls[1][1]).toEqual([{ carrier: "config.api_key", operation: "replace", entry_id: "ent_fixture_key", value: "corrected-key" }]);
    expect(await screen.findByText("Saved.")).toBeVisible();
    expect(screen.getByLabelText("API key")).toHaveValue("");
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

  it("headers: a refused command retains the row so the owner can fix and retry it", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "Add a header" }));
    await user.type(screen.getByLabelText("Header name"), "X-Old");
    await user.type(screen.getByLabelText("Value for X-Old"), "private-header");
    const write = vi.spyOn(enginesApi, "secretEntries").mockRejectedValueOnce(refusal(400, "invalid_name", "This header name is not allowed.")).mockResolvedValueOnce({ schema_version: 1, services: {} } as never);
    const update = vi.spyOn(enginesApi, "update");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("The changes could not be saved")).toBeVisible();
    expect(screen.getByText("This header name is not allowed.")).toBeVisible();
    const group = screen.getByRole("group", { name: "Request headers" });
    expect(group).toBeVisible();
    expect(within(group).getByLabelText("Value for X-Old")).toHaveValue("private-header");
    await user.clear(within(group).getByLabelText("Header name"));
    await user.type(within(group).getByLabelText("Header name"), "X-New");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    expect(write.mock.calls[1][1]).toEqual([{ carrier: "config.headers", operation: "add", name: "X-New", value: "private-header" }]);
    expect(update).not.toHaveBeenCalled();
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

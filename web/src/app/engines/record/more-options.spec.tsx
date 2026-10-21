import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi, type EngineView } from "@/lib/engines-api";
import { definitions, engineView, renderWithClient } from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Tim", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const registry = { schema_version: 1, services: {} } as never;
type User = ReturnType<typeof userEvent.setup>;

async function mount(over: Partial<EngineView> = {}) {
  const view = engineView(over);
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  renderWithClient(<EngineRecordPage serviceId={view.service_id} onBack={() => undefined} onRemove={() => undefined} />);
  return (await screen.findByText("More options", { selector: "summary span" })).closest("details") as HTMLDetailsElement;
}

/** A page section by its heading: What it offers holds the adjustments, How it runs holds Start and stop. */
async function section(over: Partial<EngineView>, heading: string) {
  await mount(over);
  return (await screen.findByRole("heading", { level: 2, name: heading })).closest("section") as HTMLElement;
}

async function openMore(user: User, more: HTMLDetailsElement) {
  if (!more.open) await user.click(within(more).getByText("More options", { selector: "summary span" }));
}

async function addAdjustment(user: User, more: HTMLElement, name: string) {
  await user.click(within(more).getByText("Add an adjustment"));
  await user.click(within(more).getByRole("button", { name: new RegExp(`^${name}`) }));
  const cards = within(more).getAllByTestId("adjustment");
  return cards[cards.length - 1];
}

async function saveAndGetTransforms(user: User) {
  const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(update).toHaveBeenCalled());
  return update.mock.calls[0][1].values?.transforms;
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Tim", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
});
afterEach(() => cleanup());

describe("More options", () => {
  it("stays closed with All defaults when nothing differs", async () => {
    const more = await mount();
    expect(more.open).toBe(false);
    expect(within(more).getByText("All defaults")).toBeInTheDocument();
  });

  it("opens by itself and counts what differs from the defaults", async () => {
    const more = await mount({
      values: {
        ...engineView().values,
        reply_timeout_s: 300,
        transforms: ["reasoning-effort-final-chat", { kind: "drop-params", drop: ["user"] }],
        lifecycle_mode: "managed",
        lifecycle_start: undefined,
        lifecycle_stop: "",
        lifecycle_ready_s: 180,
      },
    });
    expect(more.open).toBe(true);
    // More options holds the connection settings only; adjustments and start and stop have their own sections.
    expect(within(more).getByText("1 changed from default")).toBeVisible();
    expect(within(more).getByText("Changed · default 600 s")).toBeVisible();
    for (const heading of ["Timeouts", "Extra path"]) {
      expect(within(more).getByRole("heading", { name: new RegExp(`^${heading}`) })).toBeVisible();
    }
    expect(screen.getByRole("group", { name: "Request headers" })).toBeVisible();
    const offers = screen.getByRole("heading", { level: 2, name: "What it offers" }).closest("section") as HTMLElement;
    expect(within(offers).getByRole("heading", { name: /^Request adjustments/ })).toBeVisible();
    const runs = screen.getByRole("heading", { level: 2, name: "How it runs" }).closest("section") as HTMLElement;
    expect(within(runs).getByRole("heading", { name: /^Start and stop/ })).toBeVisible();
  });

  it("offers no setting the gateway ignores: no sign-in header, connect time or idle stop", async () => {
    const more = await mount();
    for (const gone of [/Key header name/, /Time to connect/, /Stop when idle/, /sign-in header/i]) {
      expect(within(more).queryByText(gone)).toBeNull();
      expect(within(more).queryByLabelText(gone)).toBeNull();
    }
    expect(within(more).queryByRole("checkbox")).toBeNull();
  });

  it("shows only the groups whose fields the definition lists", async () => {
    const more = await mount({ definition: "own-model-list" });
    expect(within(more).queryByRole("heading", { name: "Extra path" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Request adjustments" })).toBeInTheDocument();
    expect(within(more).getByRole("heading", { name: "Timeouts" })).toBeInTheDocument();
  });

  it("warns when the extra path repeats the end of the address", async () => {
    const user = userEvent.setup();
    const more = await mount();
    await openMore(user, more);
    await user.type(within(more).getByLabelText("Extra path before every request"), "/v1");
    expect(within(more).getByTestId("call-line")).toHaveTextContent(
      "Connection check: GET http://192.168.1.50:8080/v1/v1/models. The address already ends in /v1, so that part is sent twice. Clear the extra path or shorten the address.",
    );
  });
});

describe("Request adjustments: the six typed forms", () => {
  it("Replace values", async () => {
    const user = userEvent.setup();
    const more = await section({}, "What it offers");
    const card = await addAdjustment(user, more, "Replace values");
    await user.type(within(card).getByLabelText("Field"), "reasoning_effort");
    await user.type(within(card).getByLabelText("When the value is (row 1)"), "xhigh");
    await user.type(within(card).getByLabelText("Send instead (row 1)"), "high");
    expect(within(card).getByRole("status")).toHaveTextContent(
      "In requests, Azizam changes the value of “reasoning_effort”: “xhigh” becomes “high”.",
    );
    expect(await saveAndGetTransforms(user)).toEqual(["reasoning-effort-final-chat", { kind: "value-map", param: "reasoning_effort", map: { xhigh: "high" } }]);
  });

  it("Rename fields", async () => {
    const user = userEvent.setup();
    const more = await section({}, "What it offers");
    const card = await addAdjustment(user, more, "Rename fields");
    await user.type(within(card).getByLabelText("Field name (row 1)"), "max_completion_tokens");
    await user.type(within(card).getByLabelText("Rename to (row 1)"), "max_tokens");
    await user.click(within(card).getByRole("button", { name: "Add a row" }));
    await user.type(within(card).getByLabelText("Field name (row 2)"), "stop_sequences");
    await user.type(within(card).getByLabelText("Rename to (row 2)"), "stop");
    await user.selectOptions(within(card).getByLabelText("Applies to, Rename fields"), "response");
    expect(within(card).getByRole("status")).toHaveTextContent(
      "In replies, Azizam renames “max_completion_tokens” to “max_tokens” and “stop_sequences” to “stop”.",
    );
    expect(await saveAndGetTransforms(user)).toEqual([
      "reasoning-effort-final-chat",
      { kind: "param-map", from: "max_completion_tokens", to: "max_tokens", phase: "response" },
      { kind: "param-map", from: "stop_sequences", to: "stop", phase: "response" },
    ]);
  });

  it("Remove fields", async () => {
    const user = userEvent.setup();
    const more = await section({}, "What it offers");
    const card = await addAdjustment(user, more, "Remove fields");
    await user.type(within(card).getByLabelText("Field to remove, item 1"), "logit_bias");
    await user.click(within(card).getByRole("button", { name: "Add a field" }));
    await user.type(within(card).getByLabelText("Field to remove, item 2"), "user");
    expect(within(card).getByRole("status")).toHaveTextContent("Azizam leaves “logit_bias”, “user” out of requests.");
    expect(await saveAndGetTransforms(user)).toEqual(["reasoning-effort-final-chat", { kind: "drop-params", drop: ["logit_bias", "user"] }]);
  });

  it("Set defaults", async () => {
    const user = userEvent.setup();
    const more = await section({}, "What it offers");
    const card = await addAdjustment(user, more, "Set defaults");
    await user.type(within(card).getByLabelText("Field (row 1)"), "top_p");
    await user.type(within(card).getByLabelText("Value when missing (row 1)"), "0.9");
    expect(within(card).getByRole("status")).toHaveTextContent("Azizam adds “top_p” = “0.9” to requests that do not set it.");
    expect(within(card).queryByLabelText(/Applies to/)).toBeNull();
    expect(await saveAndGetTransforms(user)).toEqual(["reasoning-effort-final-chat", { kind: "default-params", set: { top_p: 0.9 } }]);
  });

  it("Add a message", async () => {
    const user = userEvent.setup();
    const more = await section({}, "What it offers");
    const card = await addAdjustment(user, more, "Add a message");
    await user.selectOptions(within(card).getByLabelText("Where"), "end");
    await user.type(within(card).getByLabelText("Message text"), "Answer in plain language.");
    expect(within(card).getByRole("status")).toHaveTextContent(
      "Azizam adds instructions at the end of matching chat requests: “Answer in plain language.”.",
    );
    expect(await saveAndGetTransforms(user)).toEqual([
      "reasoning-effort-final-chat",
      { kind: "message-inject", role: "system", text: "Answer in plain language.", mode: "append" },
    ]);
  });

  it("Keep a number in range", async () => {
    const user = userEvent.setup();
    const more = await section({}, "What it offers");
    const card = await addAdjustment(user, more, "Keep a number in range");
    await user.type(within(card).getByLabelText("Field"), "temperature");
    await user.type(within(card).getByLabelText("Minimum"), "0");
    await user.type(within(card).getByLabelText("Maximum"), "1");
    expect(within(card).getByRole("status")).toHaveTextContent(
      "In requests, Azizam keeps “temperature” between 0 and 1. A value outside moves to the nearest limit.",
    );
    expect(await saveAndGetTransforms(user)).toEqual(["reasoning-effort-final-chat", { kind: "clamp", param: "temperature", min: 0, max: 1 }]);
  });

  it("a built-in shows in words and can be removed", async () => {
    const user = userEvent.setup();
    const more = await section({}, "What it offers");
    expect(within(more).getByText("Built-in adjustment: reasoning-effort-final-chat")).toBeInTheDocument();
    await user.click(within(more).getByRole("button", { name: "Remove adjustment: Built-in adjustment" }));
    expect(await saveAndGetTransforms(user)).toEqual([]);
  });
});

describe("Start and stop", () => {
  it("does not offer process creation when no process command is configured", async () => {
    const more = await section({}, "How it runs");
    expect(within(more).queryByLabelText("Who starts and stops this server")).toBeNull();
    expect(within(more).getByText("No process is configured. This page can edit existing process settings; it cannot create process control.")).toBeVisible();
    expect(within(more).queryByRole("button", { name: /Add .*command argument/ })).toBeNull();
  });

  it("an administrator reads it with the lock text", async () => {
    viewer.current = { ...viewer.current, role: "admin" };
    const more = await section(
      {
        record: { ...engineView().record, lifecycle: { driver: "command", driver_config: { start: ["systemctl", "--user", "start", "garage-llm"], stop: ["systemctl", "--user", "stop", "garage-llm"] } } },
        values: {
          ...engineView().values,
          lifecycle_mode: "managed",
          lifecycle_start: undefined,
          lifecycle_stop: undefined,
          lifecycle_ready_s: 180,
        },
      },
      "How it runs",
    );
    expect(within(more).getByText(/Only the owner can change this\. Azizam starts and stops this server with the commands below\./)).toBeVisible();
    expect(within(more).queryByLabelText("Who starts and stops this server")).toBeNull();
    expect(within(more).getByLabelText("lifecycle.driver_config start command Program")).toHaveValue("systemctl");
    for (const input of within(more).getAllByRole("textbox")) expect(input).toBeDisabled();
  });
});

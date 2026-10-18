import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi } from "@/lib/engines-api";
import { accessAnswer, definitions, engineView, renderWithClient } from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";

vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ authenticated: true, name: "Owner", email: null, role: "owner", admin: true }),
}));

const start = ["/opt/engine/bin/serve", "--model", "/models/family, chat.bin", "--token", { id: "ent_token", configured: true }] as const;
const stop = ["/opt/engine/bin/stop", "family chat"];

beforeEach(() => {
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("nobody"));
});
afterEach(() => cleanup());

function mount() {
  const view = engineView({
    values: {
      ...engineView().values,
      lifecycle_mode: "managed",
      lifecycle_start: undefined,
      lifecycle_stop: [...stop],
      lifecycle_ready_s: 180,
    },
    lifecycle_editable: true,
    revision: '"os-7"',
    record: { id: "garage-gpu", config: {}, lifecycle: {
      driver: "command", driver_config: { start: [...start], stop: [...stop], ready_timeout_s: 180 },
    } },
  });
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  const update = vi.spyOn(enginesApi, "update").mockResolvedValue({ schema_version: 1, services: {} } as never);
  renderWithClient(<EngineRecordPage serviceId={view.service_id} onBack={() => undefined} onRemove={() => undefined} />);
  const secretEntries = vi.spyOn(enginesApi, "secretEntries").mockResolvedValue({ schema_version: 1, services: {} } as never);
  return { update, secretEntries };
}

it("edits one literal argument and retains the secret by id without splitting commas or spaces", async () => {
  const user = userEvent.setup();
  const { update, secretEntries } = mount();
  const argument = await screen.findByRole("textbox", { name: "lifecycle.driver_config start command Argument 2" });
  expect(argument).toHaveValue(start[2]);
  const token = screen.getByLabelText("lifecycle.driver_config start command Argument 4");
  expect(token).toHaveValue("");
  expect(token).toHaveAttribute("type", "password");
  expect(within(screen.getByRole("group", { name: "lifecycle.driver_config start command" })).getByText("Configured")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "lifecycle.driver_config stop command Argument 1" })).toHaveValue(stop[1]);
  await user.clear(argument);
  await user.paste("/models/new family, chat.bin");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(secretEntries).toHaveBeenCalled());
  expect(update).not.toHaveBeenCalled();
  expect(secretEntries).toHaveBeenCalledWith("garage-gpu", [{ carrier: "lifecycle.driver_config.start", operation: "set",
    items: [{ literal: start[0] }, { literal: start[1] }, { literal: "/models/new family, chat.bin" }, { literal: "--token" }, { retain: "ent_token" }], clear: [] }], '"os-7"');
  expect(JSON.stringify(secretEntries.mock.calls)).not.toContain("[redacted]");
});

it("keeps readiness editable and omits unchanged command carriers from a readiness-only save", async () => {
  const user = userEvent.setup();
  const { update, secretEntries } = mount();
  const ready = await screen.findByLabelText("Wait for it to be ready");
  expect(ready).toBeEnabled();
  await user.clear(ready);
  await user.type(ready, "240");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(update).toHaveBeenCalled());
  expect(update.mock.calls[0][1].values).toMatchObject({ lifecycle_ready_s: 240 });
  expect(update.mock.calls[0][1].values).not.toHaveProperty("lifecycle_start");
  expect(update.mock.calls[0][1].values).not.toHaveProperty("lifecycle_stop");
  expect(secretEntries).not.toHaveBeenCalled();
});

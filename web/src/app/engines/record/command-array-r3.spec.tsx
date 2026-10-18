import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi } from "@/lib/engines-api";
import { accessAnswer, definitions, engineView, renderWithClient } from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";

vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ authenticated: true, name: "Owner", email: null, role: "owner", admin: true }),
}));

const start = ["/opt/engine/bin/serve", "--model", "/models/family, chat.bin", "--token", "[redacted]"];
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
      lifecycle_start: [...start],
      lifecycle_stop: [...stop],
      lifecycle_ready_s: 180,
    },
    lifecycle_editable: true,
    record: { id: "garage-gpu", config: {}, lifecycle: {
      driver: "command", driver_config: { start: [...start], stop: [...stop], ready_timeout_s: 180 },
    } },
  });
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  const update = vi.spyOn(enginesApi, "update").mockResolvedValue({ schema_version: 1, services: {} } as never);
  renderWithClient(<EngineRecordPage serviceId={view.service_id} onBack={() => undefined} onRemove={() => undefined} />);
  return update;
}

it("edits one start argument and sends start and stop as arrays without splitting commas or spaces", async () => {
  const user = userEvent.setup();
  const update = mount();
  const argument = await screen.findByRole("textbox", { name: "Start command argument 3" });
  expect(argument).toHaveValue(start[2]);
  expect(screen.getByRole("textbox", { name: "Start command argument 5" })).toHaveValue("[redacted]");
  expect(screen.getByRole("textbox", { name: "Stop command argument 2" })).toHaveValue(stop[1]);
  await user.clear(argument);
  await user.type(argument, "/models/new family, chat.bin");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(update).toHaveBeenCalled());
  expect(update.mock.calls[0][1].values).toMatchObject({
    lifecycle_start: [start[0], start[1], "/models/new family, chat.bin", ...start.slice(3)], lifecycle_stop: stop,
  });
});

it("keeps readiness editable and round-trips both command arrays when only readiness changes", async () => {
  const user = userEvent.setup();
  const update = mount();
  const ready = await screen.findByLabelText("Wait for it to be ready");
  expect(ready).toBeEnabled();
  await user.clear(ready);
  await user.type(ready, "240");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(update).toHaveBeenCalled());
  expect(update.mock.calls[0][1].values).toMatchObject({
    lifecycle_start: start, lifecycle_stop: stop, lifecycle_ready_s: 240,
  });
});

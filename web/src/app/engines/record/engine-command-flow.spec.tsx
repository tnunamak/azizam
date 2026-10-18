import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi } from "@/lib/engines-api";
import {
  accessAnswer,
  definitions,
  engineView,
  renderWithClient,
} from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: "owner" }),
}));
beforeEach(() => {
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(
    structuredClone(definitions),
  );
  vi.spyOn(enginesApi, "access").mockResolvedValue(
    accessAnswer("you_and_family"),
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
it("replaces a configured header using its id and never submits it as engine values", async () => {
  const view = engineView({
    record: {
      config: {
        headers: [{ id: "ent_header", name: "X-Tenant", configured: true }],
      },
    },
  });
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  const commands = vi
    .spyOn(enginesApi, "secretEntries")
    .mockResolvedValue({ services: {} } as never);
  const update = vi
    .spyOn(enginesApi, "update")
    .mockResolvedValue({ services: {} } as never);
  renderWithClient(
    <EngineRecordPage
      serviceId={view.service_id}
      onBack={() => {}}
      onRemove={() => {}}
    />,
  );
  const user = userEvent.setup();
  const input = await screen.findByLabelText("Value for X-Tenant");
  expect(input).toHaveValue("");
  await user.type(input, "new-header");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    expect(commands).toHaveBeenCalledWith(
      view.service_id,
      [
        {
          carrier: "config.headers",
          operation: "replace",
          entry_id: "ent_header",
          value: "new-header",
        },
      ],
      undefined,
    ),
  );
  expect(update).not.toHaveBeenCalled();
});
it("uses the latest failed check in the health label", async () => {
  const view = engineView();
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  vi.spyOn(enginesApi, "check").mockResolvedValue({
    outcome: "error",
    findings: [],
    capabilities: [],
    models: [],
    key_sent: false,
  } as never);
  renderWithClient(
    <EngineRecordPage
      serviceId={view.service_id}
      onBack={() => {}}
      onRemove={() => {}}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "Check again" }),
  );
  await waitFor(() =>
    expect(screen.getByTestId("engine-health")).toHaveTextContent(
      "Latest check failed",
    ),
  );
});
it.each(["unknown", "not_needed"])(
  "describes key transmission independently when auth is %s",
  async (auth) => {
    const view = engineView();
    vi.spyOn(enginesApi, "view").mockResolvedValue(view);
    vi.spyOn(enginesApi, "check").mockResolvedValue({
      outcome: "ok",
      findings: [],
      capabilities: [],
      models: [],
      key_sent: true,
      auth,
    } as never);
    renderWithClient(
      <EngineRecordPage
        serviceId={view.service_id}
        onBack={() => {}}
        onRemove={() => {}}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Check again" }),
    );
    expect(
      await screen.findByText(/A key was sent. Acceptance was not established/),
    ).toBeVisible();
  },
);
it("replaces a configured inference key only through its command carrier", async () => {
  const view = engineView({
    key: {
      state: "configured",
      source: "env",
      env_name: "ENGINE_TOKEN",
      origin: "http://192.168.1.50:8080",
      entry_id: "ent_key",
    } as never,
  });
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  const commands = vi
    .spyOn(enginesApi, "secretEntries")
    .mockResolvedValue({ services: {} } as never);
  const update = vi
    .spyOn(enginesApi, "update")
    .mockResolvedValue({ services: {} } as never);
  renderWithClient(
    <EngineRecordPage
      serviceId={view.service_id}
      onBack={() => {}}
      onRemove={() => {}}
    />,
  );
  const input = await screen.findByLabelText("API key");
  expect(input).toHaveValue("");
  await userEvent.type(input, "new-key");
  await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    expect(commands).toHaveBeenCalledWith(
      view.service_id,
      [
        {
          carrier: "config.api_key",
          operation: "replace",
          entry_id: "ent_key",
          value: "new-key",
        },
      ],
      undefined,
    ),
  );
  expect(update).not.toHaveBeenCalled();
});

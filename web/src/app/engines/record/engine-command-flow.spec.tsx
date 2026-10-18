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
it("keeps the revision of the entries loaded into the draft after a background refresh", async () => {
  const { act } = await import("@testing-library/react");
  const { engineViewKey } = await import("./engine-record-page");
  const view = engineView({
    revision: '"os-1"',
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
  const mounted = renderWithClient(
    <EngineRecordPage
      serviceId={view.service_id}
      onBack={() => {}}
      onRemove={() => {}}
    />,
  );
  await userEvent.type(
    await screen.findByLabelText("Value for X-Tenant"),
    "draft",
  );
  await act(async () =>
    mounted.client.setQueryData(engineViewKey(view.service_id), {
      ...view,
      revision: '"os-2"',
    }),
  );
  await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    expect(commands).toHaveBeenCalledWith(
      view.service_id,
      expect.any(Array),
      '"os-1"',
    ),
  );
});
it("requires separate saves for public settings and a secret batch", async () => {
  const view = engineView({
    record: {
      config: {
        headers: [{ id: "ent_header", name: "X-Tenant", configured: true }],
      },
    },
  });
  vi.spyOn(enginesApi, "view").mockResolvedValue(view);
  renderWithClient(
    <EngineRecordPage
      serviceId={view.service_id}
      onBack={() => {}}
      onRemove={() => {}}
    />,
  );
  const name = await screen.findByLabelText("Name");
  await userEvent.type(name, " changed");
  expect(screen.getByLabelText("Value for X-Tenant")).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Discard" }));
  await userEvent.type(
    await screen.findByLabelText("Value for X-Tenant"),
    "replacement",
  );
  expect(screen.getByLabelText("Name")).toBeDisabled();
});
it("explains which parts of an owned process can be edited", async () => {
 const view = engineView({ lifecycle_editable: false, record: { process: { driver: "process", start: ["runner"] } } });
 vi.spyOn(enginesApi, "view").mockResolvedValue(view);
 renderWithClient(<EngineRecordPage serviceId={view.service_id} onBack={() => {}} onRemove={() => {}} />);
 expect(await screen.findByText("Command arguments and environment values can change below. Other process settings are read-only here.")).toBeVisible();
});

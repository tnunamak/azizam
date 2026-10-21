import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi } from "@/lib/engines-api";
import { protocolDescriptionsApi } from "@/lib/protocol-descriptions-api";
import {
  definitions,
  renderWithClient,
} from "../__fixtures__/test-utils";
import { AddEngineDialog } from "./add-engine-dialog";

vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: "owner" }),
}));

beforeEach(() => {
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(
    structuredClone(definitions),
  );
  vi.spyOn(protocolDescriptionsApi, "list").mockResolvedValue({
    protocols: [
      {
        id: "tiny",
        title: "Tiny protocol",
        executor: "http-v1",
        operations: ["chat"],
        source: "user",
        dispatch_supported: true,
        validation: { valid: true, message: "Valid description. 1 operation." },
      },
    ],
    problems: [],
  });
});

describe("Add engine protocol dispatch", () => {
  it("validates the installed description and creates it off without probing the definition", async () => {
    const user = userEvent.setup();
    const added = vi.fn();
    const runCheck = vi.spyOn(enginesApi, "check");
    const runTest = vi.spyOn(enginesApi, "test");
    const create = vi
      .spyOn(enginesApi, "create")
      .mockResolvedValue({
        registry: { schema_version: 1, services: {} },
        serviceId: "tiny",
      } as never);
    renderWithClient(
      <AddEngineDialog
        open
        onOpenChange={() => undefined}
        onAdded={added}
        onOpenEngine={() => undefined}
        checkRevealMs={0}
      />,
    );
    await user.click(
      await screen.findByRole("button", {
        name: /Any OpenAI-compatible server/,
      }),
    );
    await user.type(
      screen.getByLabelText(/^Address/),
      "http://127.0.0.1:18301",
    );
    await user.selectOptions(
      screen.getByLabelText("Request dispatch"),
      "protocol",
    );
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Protocol description" }),
      "tiny",
    );
    expect(screen.getByText("Valid description. 1 operation.")).toBeVisible();
    expect(screen.getByText(/Configure its offerings on the engine page before turning it on/)).toBeVisible();
    expect(screen.queryByTestId("call-line")).toBeNull();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add protocol engine (off)" }));
    expect(runCheck).not.toHaveBeenCalled();
    expect(runTest).not.toHaveBeenCalled();
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        disabled: true,
        values: expect.objectContaining({
          dispatch: "protocol",
          protocol_description: "tiny",
        }),
      }),
    );
    expect(added).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining("validated without contacting the engine") }));
    expect(added.mock.calls[0][0].message).not.toMatch(/Check now|checks it when/);
  });

  it("requires a valid installed description and address before adding an opted-in engine", async () => {
    const user = userEvent.setup();
    const runCheck = vi.spyOn(enginesApi, "check");
    renderWithClient(
      <AddEngineDialog
        open
        onOpenChange={() => undefined}
        onAdded={() => undefined}
        onOpenEngine={() => undefined}
        checkRevealMs={0}
      />,
    );
    await user.click(
      await screen.findByRole("button", {
        name: /Any OpenAI-compatible server/,
      }),
    );
    await user.type(
      screen.getByLabelText(/^Address/),
      "http://127.0.0.1:18301",
    );
    await user.selectOptions(
      screen.getByLabelText("Request dispatch"),
      "protocol",
    );
    expect(screen.getByRole("button", { name: "Add protocol engine (off)" })).toBeDisabled();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Protocol description" }), "tiny");
    expect(screen.getByRole("button", { name: "Add protocol engine (off)" })).toBeEnabled();
    await user.clear(screen.getByLabelText(/^Address/));
    expect(screen.getByRole("button", { name: "Add protocol engine (off)" })).toBeDisabled();
    expect(runCheck).not.toHaveBeenCalled();
  });
});

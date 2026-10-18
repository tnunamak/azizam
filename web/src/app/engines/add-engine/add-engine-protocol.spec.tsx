import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi } from "@/lib/engines-api";
import { protocolDescriptionsApi } from "@/lib/protocol-descriptions-api";
import {
  check,
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
  it("checks and creates with the selected installed protocol", async () => {
    const user = userEvent.setup();
    const result = check("oai_ok");
    result.outcome = "error";
    result.findings = [
      {
        code: "unreachable",
        severity: "error",
        field: "address",
        title: "Could not reach the server",
        what: "No reply.",
        why: "Server is off.",
        fix: "Start it or keep it off.",
        keep_off: true,
      },
    ];
    const runCheck = vi.spyOn(enginesApi, "check").mockResolvedValue(result);
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
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Protocol description" }),
      "tiny",
    );
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(runCheck).toHaveBeenCalledWith(
      expect.objectContaining({
        values: expect.objectContaining({
          dispatch: "protocol",
          protocol_description: "tiny",
        }),
      }),
      expect.any(AbortSignal),
    );
    await user.click(
      await screen.findByRole("button", { name: "Add but keep off" }),
    );
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        disabled: true,
        values: expect.objectContaining({
          dispatch: "protocol",
          protocol_description: "tiny",
        }),
      }),
    );
  });

  it("requires a description before checking an opted-in engine", async () => {
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
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(runCheck).not.toHaveBeenCalled();
    expect(
      screen.getByText(
        "Choose an installed protocol description before continuing.",
      ),
    ).toBeVisible();
  });
});

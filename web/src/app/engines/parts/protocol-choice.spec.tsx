import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { renderWithClient } from "../__fixtures__/test-utils";
import { protocolDescriptionsApi } from "@/lib/protocol-descriptions-api";
import { ProtocolChoice, type ProtocolDraft } from "./protocol-choice";

const catalog = {
  protocols: [
    {
      id: "tiny",
      title: "Tiny protocol",
      executor: "http-v1",
      operations: ["chat", "models"],
      source: "user" as const,
      dispatch_supported: true,
      validation: {
        valid: true as const,
        message: "Valid description. 2 operations.",
      },
    },
  ],
  problems: [
    {
      file: "broken.yml",
      reason:
        "The description is invalid or unreadable. Validate the file before enabling it.",
    },
  ],
};

function mount(
  initial: ProtocolDraft = { dispatch: "", protocol: "" },
  disabled = false,
) {
  const changed = vi.fn();
  function Harness() {
    const [value, setValue] = useState(initial);
    return (
      <ProtocolChoice
        value={value}
        disabled={disabled}
        onChange={(next) => {
          changed(next);
          setValue(next);
        }}
      />
    );
  }
  renderWithClient(<Harness />);
  return changed;
}

describe("installed protocol choice", () => {
  it("offers only installed descriptions and shows their validation before opting in", async () => {
    vi.spyOn(protocolDescriptionsApi, "list").mockResolvedValue(catalog);
    const user = userEvent.setup();
    const changed = mount();
    await user.selectOptions(
      screen.getByLabelText("Request dispatch"),
      "protocol",
    );
    const select = await screen.findByRole("combobox", {
      name: "Protocol description",
    });
    expect(select.tagName).toBe("SELECT");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(
      screen.getByRole("option", { name: "Tiny protocol (tiny)" }),
    ).toBeVisible();
    await user.selectOptions(select, "tiny");
    expect(changed).toHaveBeenLastCalledWith({
      dispatch: "protocol",
      protocol: "tiny",
    });
    expect(screen.getByText("Valid description. 2 operations.")).toBeVisible();
    expect(
      screen.getByText("Installed by you · http-v1 · chat, models"),
    ).toBeVisible();
    expect(screen.getByText(/broken.yml/)).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Request dispatch"), "");
    expect(changed).toHaveBeenLastCalledWith({
      dispatch: "",
      protocol: "tiny",
    });
  });

  it("keeps an unavailable saved id visible and requires a valid installed replacement", async () => {
    vi.spyOn(protocolDescriptionsApi, "list").mockResolvedValue(catalog);
    mount({ dispatch: "protocol", protocol: "gone" });
    expect(
      await screen.findByText(/The saved description is unavailable/),
    ).toBeVisible();
    expect(
      screen.getByRole("combobox", { name: "Protocol description" }),
    ).toHaveAttribute("aria-invalid", "true");
    expect(
      screen.getByRole("option", { name: "gone (unavailable)" }),
    ).toBeDisabled();
  });

  it("shows catalog failure with retry and keeps locked engines read-only", async () => {
    vi.spyOn(protocolDescriptionsApi, "list").mockRejectedValue(
      new Error("unavailable"),
    );
    mount({ dispatch: "protocol", protocol: "tiny" }, true);
    expect(
      await screen.findByText("Protocol descriptions could not be loaded."),
    ).toBeVisible();
    expect(screen.getByLabelText("Request dispatch")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Retry protocol descriptions" }),
    ).toBeVisible();
  });
});

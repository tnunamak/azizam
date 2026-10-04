import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyField, ErrorState, SecretReveal } from "@/components/product";
import { ApiError } from "@/lib/api";

afterEach(cleanup);

describe("shared error recovery", () => {
  it("retries a transient load and discloses only safe technical identifiers", async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    const rawMessage = "Private upstream diagnostic: do-not-display-this-value";
    const { container } = render(
      <ErrorState
        heading="Couldn’t load requests"
        error={new ApiError("service_unavailable", rawMessage, 503)}
        onRetry={retry}
      />,
    );

    expect(screen.getByText("Couldn’t load requests")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(container).not.toHaveTextContent(rawMessage);
    await user.click(screen.getByText(/technical details/i));
    expect(container).toHaveTextContent("service_unavailable");
    expect(container).toHaveTextContent("503");
    expect(container).not.toHaveTextContent(rawMessage);
  });

  it.each([
    ["invite_expired", 410],
    ["link_unavailable", 404],
  ] as const)(
    "gives owner-help recovery for terminal %s without offering a futile retry",
    (code, status) => {
      render(
        <ErrorState
          error={new ApiError(code, "Unsafe invite diagnostic", status)}
          onRetry={vi.fn()}
          viewerRole="member"
        />,
      );
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(/invite|link/i);
      expect(alert).toHaveTextContent(/owner|new invite|new link/i);
      expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
      expect(alert).not.toHaveTextContent("Unsafe invite diagnostic");
    },
  );

  it("offers sign-in for expired authentication instead of retry", () => {
    render(
      <ErrorState
        error={
          new ApiError("authentication_required", "Private session detail", 401)
        }
        onRetry={vi.fn()}
        viewerRole="member"
      />,
    );
    expect(screen.getByRole("link", { name: /sign in/i })).toHaveAttribute(
      "href",
      expect.stringMatching(/sign-in/),
    );
    expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
  });

  it("directs members with denied access to the owner instead of admin controls", () => {
    render(
      <ErrorState
        error={new ApiError("forbidden", "Private permission detail", 403)}
        onRetry={vi.fn()}
        viewerRole="member"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/ask.*owner/i);
    expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it.each(["owner", "admin"] as const)(
    "offers %s sign-in with an authorized account after denied access",
    (role) => {
      render(
        <ErrorState
          error={new ApiError("forbidden", "Private permission detail", 403)}
          onRetry={vi.fn()}
          viewerRole={role}
        />,
      );
      expect(screen.getByRole("link", { name: /sign in/i })).toHaveAttribute(
        "href",
        expect.stringMatching(/sign-in/),
      );
      expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
    },
  );

  it("names a failed mutation and runs its caller-supplied next action", async () => {
    const user = userEvent.setup();
    const save = vi.fn();
    render(
      <ErrorState
        heading="Couldn’t save the access plan"
        error={
          new ApiError("service_unavailable", "Private save diagnostic", 503)
        }
        action={{ label: "Save again", onClick: save }}
      />,
    );
    expect(screen.getByText("Couldn’t save the access plan")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save again" }));
    expect(save).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("uses a safe fallback for an unknown error without rendering its message", () => {
    render(<ErrorState error={new Error("Private unknown diagnostic")} />);
    expect(screen.getByRole("alert")).not.toHaveTextContent(
      "Private unknown diagnostic",
    );
    expect(screen.getByRole("alert").textContent?.trim()).not.toBe("");
  });

  it.each(["opk_fixture_only", "oak_fixture_only", "secret value with spaces"])(
    "suppresses unsafe technical identifier %s",
    async (code) => {
      const user = userEvent.setup();
      const { container } = render(
        <ErrorState error={new ApiError(code, "Private diagnostic", 503)} />,
      );
      await user.click(screen.getByText(/technical details/i));
      expect(container).toHaveTextContent("503");
      expect(container).not.toHaveTextContent(code);
      expect(container).not.toHaveTextContent("Private diagnostic");
    },
  );

  it("refreshes stale configuration instead of blindly retrying a save", async () => {
    const user = userEvent.setup();
    const refresh = vi.fn();
    render(
      <ErrorState
        heading="Couldn’t save the engine configuration"
        error={new ApiError("generation_mismatch", "Private stale detail", 412)}
        onRetry={refresh}
        viewerRole="owner"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      /refresh.*before saving/i,
    );
    await user.click(screen.getByRole("button", { name: /^Refresh$/ }));
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});

describe("clipboard and reveal-once copy", () => {
  it("announces the copied field without disclosing its value", async () => {
    const user = userEvent.setup();
    const write = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    render(<CopyField value="fixture-reveal-value" label="Your key" />);
    await user.click(screen.getByRole("button", { name: "Copy your key" }));
    expect(write).toHaveBeenCalledWith("fixture-reveal-value");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your key copied to clipboard.",
    );
    expect(screen.getByRole("status")).not.toHaveTextContent(
      "fixture-reveal-value",
    );
  });

  it("defaults to Gateway address and clears old success when the field changes", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const { rerender } = render(<CopyField value="https://first.example/v1" />);
    await user.click(
      screen.getByRole("button", { name: "Copy gateway address" }),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Gateway address copied to clipboard.",
    );
    rerender(<CopyField value="https://second.example/v1" />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(
      screen.getByRole("textbox", { name: "Gateway address" }),
    ).toHaveValue("https://second.example/v1");
  });

  it("keeps a denied clipboard value selectable and announces failure without success", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
      new Error("Clipboard denied"),
    );
    const value = "https://gateway.example/openai/v1?exact=1";
    render(<CopyField value={value} />);
    await user.click(
      screen.getByRole("button", { name: "Copy gateway address" }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      /select.*copy.*manually/i,
    );
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    const input = screen.getByRole("textbox", {
      name: "Gateway address",
    }) as HTMLInputElement;
    expect(input).toHaveValue(value);
    expect(input).toBeEnabled();
    expect(input).toHaveAttribute("readonly");
    input.focus();
    input.select();
    expect(input.value.slice(input.selectionStart!, input.selectionEnd!)).toBe(
      value,
    );
  });

  it.each(["resolve", "reject"] as const)(
    "discards an old pending clipboard %s after the value changes",
    async (outcome) => {
      const user = userEvent.setup();
      let resolve!: () => void;
      let reject!: (reason: Error) => void;
      const pending = new Promise<void>((yes, no) => {
        resolve = yes;
        reject = no;
      });
      vi.spyOn(navigator.clipboard, "writeText").mockReturnValue(pending);
      const { rerender } = render(<CopyField value="first-fixture-value" />);
      await user.click(
        screen.getByRole("button", { name: "Copy gateway address" }),
      );
      rerender(<CopyField value="second-fixture-value" />);
      await act(async () => {
        if (outcome === "resolve") resolve();
        else reject(new Error("Old clipboard denial"));
        await pending.catch(() => undefined);
      });
      expect(screen.getByRole("status")).toBeEmptyDOMElement();
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it("hides the exact key after confirmation without claiming it was verified", async () => {
    const user = userEvent.setup();
    const saved = vi.fn();
    const { container, rerender } = render(
      <SecretReveal secret="fixture-only-key" onSaved={saved} />,
    );
    expect(screen.getByRole("textbox", { name: "Your key" })).toHaveValue(
      "fixture-only-key",
    );
    await user.click(screen.getByRole("button", { name: "I’ve saved it" }));
    expect(saved).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Key hidden. It won’t be shown again.",
    );
    expect(container.innerHTML).not.toContain("fixture-only-key");
    expect(within(container).queryByRole("textbox")).toBeNull();
    rerender(<SecretReveal secret="fixture-only-key" onSaved={saved} />);
    expect(container.innerHTML).not.toContain("fixture-only-key");
    expect(screen.getByRole("status")).not.toHaveTextContent(
      /verified|key saved/i,
    );
  });

  it("does not revive old success when the field changes away and back", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const { rerender } = render(<CopyField value="fixture-A" />);
    await user.click(
      screen.getByRole("button", { name: "Copy gateway address" }),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Gateway address copied to clipboard.",
    );
    rerender(<CopyField value="fixture-B" />);
    rerender(<CopyField value="fixture-A" />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("discards pending success when the field changes away and back", async () => {
    const user = userEvent.setup();
    let resolve!: () => void;
    const pending = new Promise<void>((yes) => {
      resolve = yes;
    });
    vi.spyOn(navigator.clipboard, "writeText").mockReturnValue(pending);
    const { rerender } = render(<CopyField value="fixture-A" />);
    await user.click(
      screen.getByRole("button", { name: "Copy gateway address" }),
    );
    rerender(<CopyField value="fixture-B" />);
    rerender(<CopyField value="fixture-A" />);
    await act(async () => {
      resolve();
      await pending;
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps a newer successful copy after an older attempt is denied", async () => {
    const user = userEvent.setup();
    let reject!: (reason: Error) => void;
    const old = new Promise<void>((_resolve, no) => {
      reject = no;
    });
    const write = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockReturnValueOnce(old)
      .mockResolvedValueOnce();
    render(<CopyField value="same-fixture-value" />);
    const copy = screen.getByRole("button", { name: "Copy gateway address" });
    await user.click(copy);
    await user.click(copy);
    expect(write).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Gateway address copied to clipboard.",
    );
    await act(async () => {
      reject(new Error("Older attempt denied"));
      await old.catch(() => undefined);
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Gateway address copied to clipboard.",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

import {
  cleanup,
  render,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CopyField } from "@/components/product/copy-field";
import { SecretReveal } from "@/components/product/secret-reveal";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Progress } from "@/components/ui/progress";
import { Money } from "@/components/product/money";
import { StatusPill } from "@/components/product/status-pill";
import { CommandDialog, CommandInput } from "@/components/ui/command";

afterEach(cleanup);
describe("foundation product contracts", () => {
  it("makes a named scroll region reachable with the keyboard", () => {
    render(
      <ScrollArea aria-label="Request details">
        <p>Scrollable content</p>
      </ScrollArea>,
    );
    expect(
      screen.getByRole("region", { name: "Request details" }),
    ).toHaveAttribute("tabindex", "0");
  });
  it("reports measured progress to assistive technology", () => {
    render(<Progress value={64} aria-label="Loading model" />);
    expect(
      screen.getByRole("progressbar", { name: "Loading model" }),
    ).toHaveAttribute("aria-valuenow", "64");
  });
  it("copies the exact endpoint and exposes clipboard success", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<CopyField value="https://gateway.example/v1" />);
    fireEvent.click(
      screen.getByRole("button", { name: "Copy gateway address" }),
    );
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("https://gateway.example/v1"),
    );
    // The message renders after writeText resolves, so wait for it, not once.
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Gateway address copied to clipboard.",
      ),
    );
  });
  it("keeps a failed copy recoverable instead of claiming success", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    render(<CopyField value="https://gateway.example/v1" />);
    fireEvent.click(
      screen.getByRole("button", { name: "Copy gateway address" }),
    );
    expect(
      await screen.findByText(
        "Copy failed. Select the value and copy it manually.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Gateway address")).toHaveValue(
      "https://gateway.example/v1",
    );
  });
  it("permanently removes a newly issued secret on confirmation", () => {
    const saved = vi.fn();
    const view = render(<SecretReveal secret="example-only" onSaved={saved} />);
    expect(screen.getByLabelText("Your key")).toHaveValue("example-only");
    fireEvent.click(screen.getByRole("button", { name: "I’ve saved it" }));
    expect(screen.queryByLabelText("Your key")).toBeNull();
    expect(saved).toHaveBeenCalledOnce();
    view.rerender(<SecretReveal secret="example-only" onSaved={saved} />);
    expect(screen.queryByLabelText("Your key")).toBeNull();
  });
  it("distinguishes unknown cost, zero cost and local hardware", () => {
    render(
      <>
        <Money amount={null} />
        <Money amount={0} />
        <Money amount={0.00004} />
        <Money amount={null} local />
      </>,
    );
    expect(screen.getByText("Not measured yet")).toBeVisible();
    expect(screen.getByText("$0.00")).toBeVisible();
    expect(screen.getByText("$0.00004")).toBeVisible();
    expect(screen.getByText("Local · no provider charge")).toBeVisible();
  });
  it("renders truthful availability labels for unknown and startable idle states", () => {
    render(
      <>
        <StatusPill
          status="unknown"
          reason="No health endpoint is configured."
        />
        <StatusPill
          status="standby_stopped"
          reason="Starts when you send a request."
        />
      </>,
    );
    expect(screen.getByText("Unknown")).toBeVisible();
    expect(screen.getByText("No health endpoint is configured.")).toBeVisible();
    expect(screen.getByText("Ready to start")).toBeVisible();
    expect(screen.getByText("Starts when you send a request.")).toBeVisible();
    expect(screen.queryByText("Unavailable")).toBeNull();
  });
  it.each([
    ["ready", "Ready"],
    ["idle", "Ready to start"],
    ["starting", "Starting"],
    ["busy", "Busy"],
    ["degraded", "Degraded"],
    ["unavailable", "Unavailable"],
    ["unknown", "Unknown"],
  ])("preserves the measured %s state and its reason", (status, label) => {
    render(<StatusPill status={status} reason={`Observation for ${status}`} />);
    expect(screen.getByText(label, { exact: true })).toBeVisible();
    expect(screen.getByText(`Observation for ${status}`)).toBeVisible();
    if (status === "unknown") {
      expect(screen.queryByText("Ready", { exact: true })).toBeNull();
      expect(screen.queryByText("Unavailable", { exact: true })).toBeNull();
    }
  });
  it("owns command dialog title and description inside its accessible dialog", () => {
    render(
      <CommandDialog
        open
        title="Find a page"
        description="Navigate your gateway"
      >
        <CommandInput placeholder="Search" />
      </CommandDialog>,
    );
    const dialog = screen.getByRole("dialog", { name: "Find a page" });
    expect(dialog).toHaveAccessibleDescription("Navigate your gateway");
    expect(dialog).toContainElement(
      screen.getByRole("heading", { name: "Find a page" }),
    );
  });
});

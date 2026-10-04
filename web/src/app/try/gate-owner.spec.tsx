import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { costFrom } from "./result-cost";
import { ReceiptLink } from "./receipt-link";

afterEach(cleanup);

describe("Try result honesty", () => {
  it("reports no provider charge for a local decision without a measured cost", () => {
    expect(costFrom({ decision: "yes" }, undefined, "Local")).toBe(
      "Local · no provider charge",
    );
  });
  it("keeps the local label for a measured zero provider charge", () => {
    expect(
      costFrom(
        {},
        new Response(null, { headers: { "x-litellm-response-cost": "0" } }),
        "Local",
      ),
    ).toBe("Local · no provider charge");
  });
  it("keeps missing locality and cost unknown", () => {
    expect(costFrom({ decision: "yes" })).toBe("Not measured yet");
  });
  it("retains the measured remote charge", () => {
    expect(
      costFrom(
        {},
        new Response(null, { headers: { "x-litellm-response-cost": "0.12" } }),
        "Remote",
      ),
    ).toBe("$0.12");
  });
  it("shows a short receipt but links and copies the full id", async () => {
    const id = "request-0123456789-abcdef0123456789";
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<ReceiptLink id={id} />);
    const link = screen.getByRole("link", { name: "Open receipt request-…" });
    expect(link).toHaveAttribute("href", `/r/${id}`);
    expect(link).toHaveTextContent("request-…");
    fireEvent.click(screen.getByRole("button", { name: "Copy receipt ID" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(id));
    expect(screen.getByText("Receipt ID copied")).toBeInTheDocument();
  });
});

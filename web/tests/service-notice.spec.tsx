import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import {
  ServiceNotice,
  serviceUnavailable,
} from "@/components/product/service-notice";
afterEach(cleanup);
test("keeps the recovery link in the same sentence block as a friendly name", () => {
  render(<ServiceNotice message={serviceUnavailable("quality_checks")} />);
  const message = screen.getByRole("status");
  expect(message).toHaveTextContent(
    "Quality checks can’t take requests right now. See Status",
  );
  expect(screen.getByRole("link", { name: "See Status" }).parentElement).toBe(
    message,
  );
  expect(screen.getByRole("link")).toHaveAttribute("href", "/status#engines");
});

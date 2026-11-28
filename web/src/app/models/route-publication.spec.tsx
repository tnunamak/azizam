import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
vi.mock("@/components/product/discovery-tabs", () => ({ DiscoveryTabs: () => null }));
vi.mock("./models-client", () => ({ default: () => <div>Model catalog</div> }));
import Page from "./page";
afterEach(cleanup);
it("explains that route pins are callable but absent from the list", () => {
  render(<Page />);
  expect(screen.getByText(/pins are accepted but not listed/i)).toBeInTheDocument();
});

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiscoveryTabs } from "@/components/product/discovery-tabs";

describe("DiscoveryTabs", () => {
  it("renders both discovery links with their page hrefs", () => {
    render(<DiscoveryTabs current="models" />);
    const navigation = screen.getByRole("navigation", { name: "Discovery" });
    expect(within(navigation).getByRole("link", { name: "Models" })).toHaveAttribute("href", "/models");
    expect(within(navigation).getByRole("link", { name: "Workflows" })).toHaveAttribute("href", "/workflows");
  });

  it("marks only Models as the current page when models is current", () => {
    render(<DiscoveryTabs current="models" />);
    expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Workflows" })).not.toHaveAttribute("aria-current");
  });

  it("names the navigation landmark Discovery", () => {
    render(<DiscoveryTabs current="models" />);
    expect(screen.getByRole("navigation", { name: "Discovery" })).toBeInTheDocument();
  });

  it("marks only Workflows as the current page when workflows is current", () => {
    render(<DiscoveryTabs current="workflows" />);
    expect(screen.getByRole("link", { name: "Workflows" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Models" })).not.toHaveAttribute("aria-current");
  });
});
